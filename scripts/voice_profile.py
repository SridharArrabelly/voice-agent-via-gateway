"""Voice Live turn profiler — where does the latency actually go?

Feeds a spoken question (a WAV file) into the SAME Foundry Voice Live agent and
times every stage of the pipeline from the realtime event stream, so you can see
whether a slow turn is ASR, model reasoning, a tool call, or TTS — and how much
(if any) the APIM gateway contributes.

It reconstructs the pipeline from the event timeline (Voice Live does not expose
a single server-side "ASR=x, LLM=y, TTS=z" field):

  speaking     : length of the input audio (informational)
  asr          : audio committed          -> input_audio_transcription.completed
  model start  : audio committed          -> response.created
  reasoning    : response.created         -> first text/transcript token
    (tool)     : mcp_call added           -> mcp_call output_item.done  (subset of reasoning)
  tts first    : first text/transcript    -> first response.audio.delta
  tts playout  : first audio.delta        -> response.audio.done
  total        : audio committed          -> response.done

Two paths (choose which to profile, or run both to compare):
  apim   : raw WebSocket -> APIM gateway -> Voice Live agent   (subscription key)
  direct : raw WebSocket -> Voice Live agent                   (Entra bearer, az login)

Usage:
  uv run python scripts/voice_profile.py                       # apim, sample WAV
  uv run python scripts/voice_profile.py --path direct
  uv run python scripts/voice_profile.py --path both --iters 3
  uv run python scripts/voice_profile.py --wav path\\to\\your-question.wav

Bring your own audio: any mono 16-bit PCM WAV works (it is resampled to 24 kHz if
needed). Record a real question your users would ask to profile your own traffic.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path

import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_VERSION = os.environ.get("VOICELIVE_API_VERSION", "2026-04-10")
FOUNDRY_WS_HOST = os.environ["FOUNDRY_WS_HOST"].strip()
AGENT = os.environ["AGENT_NAME"].strip()
PROJECT = os.environ["AGENT_PROJECT_NAME"].strip()
GATEWAY = os.environ.get("GATEWAY_WS_URL", "wss://apim-ai-gateway-sweden.azure-api.net/voice-agent")
SUBKEY = os.environ.get("APIM_SUBSCRIPTION_KEY", "").strip()
TRANSCRIPTION_MODEL = os.environ.get("INPUT_TRANSCRIPTION_MODEL", "azure-speech").strip()
DEFAULT_WAV = ROOT / "scripts" / "assets" / "question-capital-australia.wav"

DIRECT_URL = f"{FOUNDRY_WS_HOST}/voice-live/realtime?api-version={API_VERSION}&agent-name={AGENT}&agent-project-name={PROJECT}"
APIM_URL = f"{GATEWAY}?subscription-key={SUBKEY}"
RESULTS_DIR = ROOT / "results"
TARGET_RATE = 24000
READ_TIMEOUT = 90


def load_pcm24k(path: Path) -> tuple[bytes, float]:
    """Return (raw PCM16 mono @24kHz, duration_seconds) for any mono/stereo 16-bit WAV.

    Stdlib-only (no audioop, which was removed in Python 3.13): averages stereo to
    mono and linear-resamples to 24 kHz — plenty for ASR profiling.
    """
    with wave.open(str(path), "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise SystemExit(f"{path}: need 16-bit PCM WAV (got {width * 8}-bit)")
    samples = array("h")
    samples.frombytes(raw)
    if ch == 2:  # average L/R to mono
        samples = array("h", [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)])
    if rate != TARGET_RATE:  # linear-interpolation resample
        ratio = rate / TARGET_RATE
        n_out = int(len(samples) / ratio)
        out = array("h", bytes(2 * n_out))
        last = len(samples) - 1
        for i in range(n_out):
            pos = i * ratio
            i0 = int(pos)
            frac = pos - i0
            s0 = samples[i0]
            s1 = samples[i0 + 1] if i0 < last else s0
            out[i] = int(s0 + (s1 - s0) * frac)
        samples = out
    pcm = samples.tobytes()
    duration = len(pcm) / (TARGET_RATE * 2)
    return pcm, duration


async def _connect(path: str):
    if path == "apim":
        if not SUBKEY:
            raise SystemExit("APIM_SUBSCRIPTION_KEY not set in .env (needed for --path apim)")
        return await websockets.connect(APIM_URL, max_size=None, open_timeout=20)
    # direct: Entra bearer via az login
    from azure.identity.aio import AzureCliCredential
    cred = AzureCliCredential()
    try:
        tok = (await cred.get_token("https://ai.azure.com/.default")).token
    finally:
        await cred.close()
    return await websockets.connect(DIRECT_URL, max_size=None, open_timeout=20,
                                    additional_headers={"Authorization": f"Bearer {tok}"})


async def profile_once(path: str, pcm: bytes, audio_s: float) -> dict:
    now = time.perf_counter
    marks: dict[str, float] = {}
    transcript, answer, audio_deltas = "", "", 0
    errors: list[str] = []
    mcp_seen = False

    ws = await _connect(path)
    try:
        # 1) wait for session.created, then configure the session
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT))
            if m.get("type") == "session.created":
                break
            if m.get("type") == "error":
                raise RuntimeError(json.dumps(m.get("error")))
        await ws.send(json.dumps({"type": "session.update", "session": {
            "modalities": ["audio", "text"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": TRANSCRIPTION_MODEL},
            "turn_detection": None,  # manual commit -> deterministic, no VAD tuning
        }}))

        # 2) stream the audio up, then commit + ask for a response
        marks["first_append"] = now()
        step = 32000  # ~0.66s of 24kHz PCM16 per frame
        for i in range(0, len(pcm), step):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(pcm[i:i + step]).decode()}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        marks["commit"] = now()
        await ws.send(json.dumps({"type": "response.create"}))

        # 3) time the event stream
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT)
            if isinstance(raw, bytes):
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "conversation.item.input_audio_transcription.completed":
                marks.setdefault("asr", now())
                transcript = (m.get("transcript") or "").strip()
            elif t == "response.created":
                marks.setdefault("created", now())
            elif t in ("response.output_item.added",) and (m.get("item") or {}).get("type") == "mcp_call":
                marks.setdefault("mcp_start", now()); mcp_seen = True
            elif t.startswith("response.mcp_call") and "mcp_start" not in marks:
                marks["mcp_start"] = now(); mcp_seen = True
            elif t == "response.output_item.done" and (m.get("item") or {}).get("type") == "mcp_call":
                marks["mcp_done"] = now()
            elif t in ("response.text.delta", "response.audio_transcript.delta"):
                answer += m.get("delta", "")
                marks.setdefault("first_token", now())
            elif t == "response.audio.delta":
                audio_deltas += 1
                marks.setdefault("first_audio", now())
            elif t == "response.audio.done":
                marks.setdefault("audio_done", now())
            elif t == "response.done":
                marks["done"] = now()
                break
            elif t == "error":
                errors.append(json.dumps(m.get("error"))[:200])
    finally:
        await ws.close()

    def span(a, b):
        return (marks[b] - marks[a]) * 1000 if a in marks and b in marks else float("nan")

    return {
        "speaking_ms": audio_s * 1000,
        "asr_ms": span("commit", "asr"),
        "model_start_ms": span("commit", "created"),
        "reasoning_ms": span("created", "first_token"),
        "tool_ms": span("mcp_start", "mcp_done"),
        "tts_first_ms": span("first_token", "first_audio"),
        "tts_playout_ms": span("first_audio", "audio_done"),
        "total_ms": span("commit", "done"),
        "tool_used": mcp_seen,
        "audio_deltas": audio_deltas,
        "transcript": transcript,
        "answer": answer.strip(),
        "errors": errors,
    }


STAGES = [
    ("speaking_ms", "user speaking (input audio length)"),
    ("asr_ms", "ASR / speech-to-text"),
    ("model_start_ms", "commit -> model start"),
    ("reasoning_ms", "reasoning -> first token"),
    ("tool_ms", "  (of which: web/MCP tool)"),
    ("tts_first_ms", "TTS to first audio"),
    ("tts_playout_ms", "TTS playout (speaking)"),
    ("total_ms", "TOTAL (commit -> done)"),
]


def _med(rows, key):
    vals = [r[key] for r in rows if r[key] == r[key]]
    return statistics.median(vals) if vals else float("nan")


def print_and_collect(path: str, rows: list[dict], lines: list[str]):
    hdr = f"===== {path.upper()}  (n={len(rows)}) — median ms ====="
    print("\n" + hdr)
    lines += ["", f"## {path.upper()} (median of {len(rows)} run(s))", "",
              "| stage | ms |", "|-------|---:|"]
    for key, label in STAGES:
        v = _med(rows, key)
        if v != v:
            continue
        print(f"  {label:38} {v:8.0f}")
        lines.append(f"| {label} | {v:.0f} |")
    ok = [r for r in rows if r["transcript"]]
    if ok:
        print(f"  transcript : {ok[0]['transcript']!r}")
        print(f"  answer     : {ok[0]['answer'][:80]!r}")
        lines += ["", f"- ASR transcript: `{ok[0]['transcript']}`",
                  f"- Agent answer: {ok[0]['answer'][:160]}",
                  f"- Web/MCP tool used: **{ok[0]['tool_used']}**"]
    errs = [e for r in rows for e in r["errors"]]
    if errs:
        print(f"  warnings   : {errs[:2]}")
        lines.append(f"- Warnings: {errs[:3]}")


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", choices=["apim", "direct", "both"], default="apim")
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--wav", default=str(DEFAULT_WAV))
    args = ap.parse_args()

    pcm, audio_s = load_pcm24k(Path(args.wav))
    print(f"agent={AGENT}  wav={Path(args.wav).name} ({audio_s:.1f}s)  iters={args.iters}  path={args.path}")

    paths = ["apim", "direct"] if args.path == "both" else [args.path]
    results: dict[str, list[dict]] = {}
    for p in paths:
        results[p] = []
        print(f"\n########## profiling: {p} ##########")
        for i in range(args.iters):
            try:
                r = await profile_once(p, pcm, audio_s)
                results[p].append(r)
                print(f"  [{p}] {i+1}/{args.iters}: total={r['total_ms']:.0f}ms "
                      f"(asr={r['asr_ms']:.0f} reason={r['reasoning_ms']:.0f} "
                      f"tts_first={r['tts_first_ms']:.0f} playout={r['tts_playout_ms']:.0f})")
            except Exception as exc:
                print(f"  [{p}] {i+1} FAILED: {str(exc)[:180]}")
            await asyncio.sleep(1.5)

    stamp = datetime.now(timezone.utc).isoformat()
    lines = ["# Voice Live turn profile — pipeline stage breakdown", "",
             f"- Generated (UTC): `{stamp}`",
             f"- Agent: `{AGENT}` / project `{PROJECT}` · input: `{Path(args.wav).name}` ({audio_s:.1f}s)",
             f"- Stages reconstructed from the Voice Live realtime event stream."]
    for p in paths:
        if results[p]:
            print_and_collect(p, results[p], lines)
    if args.path == "both" and results.get("apim") and results.get("direct"):
        lines += ["", "## APIM overhead (median apim − direct, ms)", "",
                  "| stage | Δ ms |", "|-------|----:|"]
        print("\n===== APIM overhead (apim − direct, median ms) =====")
        for key, label in STAGES:
            a, d = _med(results["apim"], key), _med(results["direct"], key)
            if a == a and d == d:
                print(f"  {label:38} {a-d:+8.0f}")
                lines.append(f"| {label} | {a-d:+.0f} |")

    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "voice-profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (RESULTS_DIR / "voice-profile.json").write_text(
        json.dumps({"generated_utc": stamp, "agent": AGENT, "wav": Path(args.wav).name,
                    "audio_seconds": audio_s, "runs": results}, indent=2), encoding="utf-8")
    print(f"\nWrote {RESULTS_DIR / 'voice-profile.md'} and voice-profile.json")


if __name__ == "__main__":
    asyncio.run(main())
