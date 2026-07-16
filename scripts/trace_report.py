"""End-to-end latency trace report.

One command to see where a voice turn's time goes — ASR / reasoning / web-tool /
first-audio (TTS) — plus the browser mouth-to-ear wait and the APIM gateway
overhead, all pulled from the telemetry the backend emits.

Two things it can do (default: both):

  1. GENERATE  a few turns through the running backend (browser-like client that
     feeds a spoken WAV and posts the same client.* marks the UI does), then
  2. REPORT    the `voice.turn` spans from Application Insights + the APIM gateway
     overhead from Log Analytics, as readable tables.

Usage:
    uv run python scripts/trace_report.py                 # generate 2 turns, then report
    uv run python scripts/trace_report.py --report-only   # just read recent traces
    uv run python scripts/trace_report.py --generate 4    # more turns
    uv run python scripts/trace_report.py --minutes 60    # widen the query window
    uv run python scripts/trace_report.py --wav my.wav    # your own question audio

Config (from .env):
    BACKEND_WS_URL             backend /realtime endpoint (default ws://127.0.0.1:8000/realtime)
    APPINSIGHTS_WORKSPACE_ID   Log Analytics workspace backing App Insights (voice.turn spans)
    LOG_ANALYTICS_WORKSPACE_ID workspace receiving APIM gateway logs (optional)

Auth: uses your Azure CLI login (az login) via DefaultAzureCredential.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import statistics
import sys
import time
import wave
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_WAV = ROOT / "scripts" / "assets" / "question-capital-australia.wav"
BACKEND_WS_URL = os.environ.get("BACKEND_WS_URL", "ws://127.0.0.1:8000/realtime")
# Model-mode route: derive from the agent route unless overridden.
BACKEND_WS_URL_MODEL = os.environ.get(
    "BACKEND_WS_URL_MODEL", BACKEND_WS_URL.replace("/realtime", "/realtime-model")
)
ROUTES = {"agent": BACKEND_WS_URL, "model": BACKEND_WS_URL_MODEL}


# --------------------------------------------------------------------------- #
# 1. Generate turns through the backend (browser-like client)
# --------------------------------------------------------------------------- #
def _wav_chunks(path: Path, chunk_ms: int = 100, rate: int = 24000) -> list[str]:
    with wave.open(str(path), "rb") as w:
        data = w.readframes(w.getnframes())
    n = int(rate * 2 * chunk_ms / 1000)
    return [base64.b64encode(data[i:i + n]).decode() for i in range(0, len(data), n)]


async def _run_turn(ws, chunks: list[str], silence: str, idx: int) -> bool:
    import json
    await ws.send(json.dumps({"type": "session.update", "session": {"modalities": ["text", "audio"]}}))
    got_audio = False

    async def sender():
        for c in chunks:
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": c}))
            await asyncio.sleep(0.03)
        # Mark end-of-speech exactly like the browser does (mouth-to-ear clock start).
        await ws.send(json.dumps({"type": "client.speech_stopped", "t": time.time() * 1000}))
        for _ in range(12):  # trailing silence so server VAD detects end-of-turn
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": silence}))
            await asyncio.sleep(0.05)

    st = asyncio.create_task(sender())
    t0 = time.time()
    transcript = ""
    try:
        while time.time() - t0 < 30:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "conversation.item.input_audio_transcription.completed":
                transcript = (m.get("transcript") or "").strip()
            elif t == "response.audio.delta" and not got_audio:
                got_audio = True
                await ws.send(json.dumps({"type": "client.first_audio_played", "t": time.time() * 1000}))
            elif t == "error":
                print(f"  turn {idx}: server error: {m.get('error')}")
                return False
            elif t == "response.done":
                print(f"  turn {idx}: OK  ({transcript!r})")
                return True
        print(f"  turn {idx}: incomplete")
        return False
    finally:
        st.cancel()


async def _generate(n: int, wav: Path, modes: list[str]) -> int:
    import websockets
    chunks = _wav_chunks(wav)
    silence = base64.b64encode(b"\x00" * int(24000 * 2 * 0.1)).decode()
    ok = 0
    for mode in modes:
        url = ROUTES[mode]
        print(f"Generating {n} turn(s) in {mode} mode through {url} ...")
        for i in range(n):
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    if await _run_turn(ws, chunks, silence, i + 1):
                        ok += 1
            except (OSError, ConnectionError) as exc:
                print(f"\n[!] Could not reach the backend at {url}: {exc}")
                print("    Start it first:  uv run python backend/app.py")
                if mode == "model":
                    print("    (model mode also needs GATEWAY_WS_URL_MODEL + APIM_SUBSCRIPTION_KEY_MODEL in .env)")
                break
            await asyncio.sleep(1)
    return ok


# --------------------------------------------------------------------------- #
# 2. Report — pull the traces back out
# --------------------------------------------------------------------------- #
def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.0f}"
    return str(v)


def _print_table(headers: list[str], rows: list[list], aligns: str = "") -> None:
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    aligns = (aligns + "l" * len(headers))[: len(headers)]

    def render(cells):
        out = []
        for cell, w, a in zip(cells, widths, aligns):
            s = str(cell)
            out.append(s.rjust(w) if a == "r" else s.ljust(w))
        return "  ".join(out)

    print(render(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(render([_fmt(c) for c in r]))


def _query(client, workspace_id: str, kql: str, minutes: int):
    from azure.monitor.query import LogsQueryStatus
    resp = client.query_workspace(workspace_id, kql, timespan=timedelta(minutes=minutes))
    if resp.status == LogsQueryStatus.SUCCESS:
        tables = resp.tables
    else:  # partial
        tables = resp.partial_data
    if not tables or not tables[0].rows:
        return [], []
    t = tables[0]
    return list(t.columns), [list(r) for r in t.rows]


TURN_KQL = """
AppDependencies
| where TimeGenerated > ago({m}m)
| where Name == 'voice.turn'
| extend d = Properties
| project TimeGenerated,
    mode      = tostring(d['voice.mode']),
    turn      = toint(d['turn.index']),
    asr       = todouble(d['turn.asr_ms']),
    reasoning = todouble(d['turn.reasoning_ms']),
    tool      = todouble(d['turn.tool_ms']),
    tts_first = todouble(d['turn.tts_first_ms']),
    total     = todouble(d['turn.total_ms']),
    m2e       = todouble(d['turn.client_wait_ms']),
    tokens    = toint(d['turn.usage.total_tokens']),
    question  = tostring(d['turn.transcript'])
| order by TimeGenerated desc
| take 50
"""

GATEWAY_KQL = """
ApiManagementGatewayLogs
| where TimeGenerated > ago({m}m)
| where ApiId in ('voice-agent', 'voice-agent-model')
| extend apim_overhead = TotalTime - BackendTime
| extend mode = iff(ApiId == 'voice-agent-model', 'model', 'agent')
| project TimeGenerated, mode, Method, ResponseCode, ClientProtocol,
          backend_ms = BackendTime, total_ms = TotalTime, apim_overhead
| order by TimeGenerated desc
| take 50
"""


def _report(minutes: int) -> None:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient

    ai_ws = os.environ.get("APPINSIGHTS_WORKSPACE_ID", "").strip()
    gw_ws = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "").strip()
    if not ai_ws:
        print("\n[!] APPINSIGHTS_WORKSPACE_ID not set in .env — can't read voice.turn spans.")
        print("    (workspace-based App Insights: the customerId of its backing LA workspace)")
        return

    client = LogsQueryClient(DefaultAzureCredential())

    print(f"\n=== Per-turn latency (voice.turn, last {minutes} min) — all values in ms ===")
    cols, rows = _query(client, ai_ws, TURN_KQL.format(m=minutes), minutes)
    if not rows:
        print("No turns found yet. Ingestion can lag 2-5 min after a conversation — retry,")
        print("or run with --generate to create some. Make sure the backend was running.")
    else:
        show = ["mode", "turn", "asr", "reasoning", "tool", "tts_first", "total", "m2e", "tokens", "question"]
        idx = {c: i for i, c in enumerate(cols)}
        table = [[r[idx[c]] for c in show] for r in rows]
        _print_table(show, table, aligns="lrrrrrrrrl")

        # Per-mode medians → side-by-side agent vs model comparison.
        num_cols = ("asr", "reasoning", "tool", "tts_first", "total", "m2e")

        def med(subset, col):
            vals = [r[idx[col]] for r in subset if r[idx[col]] is not None]
            return statistics.median(vals) if vals else None

        modes_present = []
        for r in rows:
            mv = r[idx["mode"]] or "agent"
            if mv not in modes_present:
                modes_present.append(mv)
        med_rows = []
        for mv in modes_present:
            subset = [r for r in rows if (r[idx["mode"]] or "agent") == mv]
            med_rows.append([f"{mv} (median, n={len(subset)})"] + [med(subset, c) for c in num_cols])
        print()
        _print_table(["mode", "asr", "reasoning", "tool", "tts_first", "total", "m2e"],
                     med_rows, aligns="lrrrrrr")

    if gw_ws:
        print(f"\n=== APIM gateway overhead (ApiManagementGatewayLogs, last {minutes} min) — ms ===")
        cols, rows = _query(client, gw_ws, GATEWAY_KQL.format(m=minutes), minutes)
        if not rows:
            print("No gateway rows yet (new workspace can take 10-30 min for first ingestion).")
        else:
            idx = {c: i for i, c in enumerate(cols)}
            show = ["mode", "Method", "ResponseCode", "ClientProtocol", "backend_ms", "total_ms", "apim_overhead"]
            table = [[r[idx[c]] for c in show] for r in rows]
            _print_table(show, table, aligns="llllrrr")
    else:
        print("\n(Tip: set LOG_ANALYTICS_WORKSPACE_ID in .env to also show APIM gateway overhead.)")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="End-to-end voice latency trace report.")
    ap.add_argument("--generate", type=int, default=2,
                    help="number of turns to drive through the backend first (default 2; 0 to skip)")
    ap.add_argument("--mode", choices=["both", "agent", "model"], default="both",
                    help="which mode(s) to generate turns for (default both, for the comparison)")
    ap.add_argument("--report-only", action="store_true", help="skip generation, just read traces")
    ap.add_argument("--minutes", type=int, default=30, help="query look-back window (default 30)")
    ap.add_argument("--wav", type=Path, default=DEFAULT_WAV, help="question audio to feed (WAV)")
    ap.add_argument("--wait", type=int, default=20,
                    help="seconds to wait for span export before querying (default 20)")
    args = ap.parse_args()

    modes = ["agent", "model"] if args.mode == "both" else [args.mode]
    n = 0 if args.report_only else args.generate
    if n > 0:
        if not args.wav.exists():
            print(f"[!] WAV not found: {args.wav}")
            sys.exit(1)
        ok = asyncio.run(_generate(n, args.wav, modes))
        if ok == 0:
            print("No turns generated; reporting whatever is already ingested.")
        else:
            print(f"\nWaiting {args.wait}s for telemetry export, then querying "
                  "(App Insights ingestion may add a few more minutes)...")
            time.sleep(args.wait)

    _report(args.minutes)


if __name__ == "__main__":
    main()
