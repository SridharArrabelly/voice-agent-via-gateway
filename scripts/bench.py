"""Voice Live latency benchmark — 2x2 matrix against the SAME Foundry agent.

Runs four combinations and records the results:

  1. APIM gateway   + voice   (raw WebSocket -> APIM -> Voice Live agent)
  2. APIM gateway   + text    (raw WebSocket -> APIM -> Voice Live agent)
  3. Direct SDK     + voice   (azure-ai-voicelive -> Voice Live agent, Entra)
  4. Direct SDK     + text    (azure-ai-voicelive -> Voice Live agent, Entra)

All four invoke the EXISTING agent (no agent is created, no model is sent). The
agent runs on its own configured model (gpt-5.4-mini). Modality is chosen per
session: voice = [audio, text], text = [text].

Per run we measure (ms):
  - connect : start                -> connection ready (TLS + upgrade + backend dial)
  - session : connected            -> session ready (session.created / session.updated)
  - first   : response.create sent -> first output token   (audio delta / text delta)
  - done    : response.create sent -> response.done
  - e2e     : start                -> response.done

Comparing APIM vs Direct for the same modality isolates the APIM hop overhead.

    uv run python scripts/bench.py [iterations]     # default 6
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_VERSION = os.environ.get("VOICELIVE_API_VERSION", "2026-04-10")
SDK_API_VERSION = os.environ.get("VOICELIVE_SDK_API_VERSION", "2026-01-01-preview")
FOUNDRY_WS_HOST = os.environ.get("FOUNDRY_WS_HOST", "wss://foundry-resource-sweden-01.services.ai.azure.com")
FOUNDRY_HTTP = FOUNDRY_WS_HOST.replace("wss://", "https://").replace("ws://", "http://")
AGENT = os.environ["AGENT_NAME"].strip()
PROJECT = os.environ["AGENT_PROJECT_NAME"].strip()
GATEWAY = os.environ.get("GATEWAY_WS_URL", "wss://apim-ai-gateway-sweden.azure-api.net/voice-agent")
SUBKEY = os.environ["APIM_SUBSCRIPTION_KEY"].strip()

DIRECT_WS_URL = f"{FOUNDRY_WS_HOST}/voice-live/realtime?api-version={API_VERSION}&agent-name={AGENT}&agent-project-name={PROJECT}"
APIM_URL = f"{GATEWAY}?subscription-key={SUBKEY}"

PROMPT = "Say hello in one short sentence."
RESULTS_DIR = ROOT / "results"
READ_TIMEOUT = 60  # per-turn ceiling (agent tool discovery can add a few seconds)

# modality -> (list sent in session.update, event type that marks the first output token)
MODALITIES = {
    "voice": (["audio", "text"], "response.audio.delta"),
    "text": (["text"], "response.text.delta"),
}


# --------------------------------------------------------------------------- #
# Path 1/2: raw WebSocket through APIM
# --------------------------------------------------------------------------- #
async def run_apim(modality: str) -> dict:
    modalities, first_type = MODALITIES[modality]
    t0 = time.perf_counter()
    async with websockets.connect(APIM_URL, max_size=None, open_timeout=20) as ws:
        t_open = time.perf_counter()
        t_created = t_req = t_first = None
        text, audio = "", 0
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT)
            if isinstance(raw, bytes):
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "session.created":
                t_created = time.perf_counter()
                await ws.send(json.dumps({"type": "session.update", "session": {"modalities": modalities}}))
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": PROMPT}]}}))
                await ws.send(json.dumps({"type": "response.create"}))
                t_req = time.perf_counter()
            elif t in ("response.audio_transcript.delta", "response.text.delta"):
                text += m.get("delta", "")
                if t == first_type and t_first is None:
                    t_first = time.perf_counter()
            elif t == "response.audio.delta":
                audio += 1
                if first_type == "response.audio.delta" and t_first is None:
                    t_first = time.perf_counter()
            elif t == "response.done":
                return _metrics(t0, t_open, t_created, t_req, t_first, text, audio)
            elif t == "error":
                raise RuntimeError(json.dumps(m.get("error")))


# --------------------------------------------------------------------------- #
# Path 3/4: direct via azure-ai-voicelive SDK
# --------------------------------------------------------------------------- #
# Foundry Voice Live expects an Entra token for this resource (same as the APIM
# managed-identity policy: resource="https://ai.azure.com").
FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def _make_caching_cli_credential():
    """AzureCliCredential subclass that caches the token in-process.

    The azure-ai-voicelive SDK calls ``get_token`` on every ``connect()``. The
    stock ``AzureCliCredential`` shells out to ``az`` each time — slow (1–3 s) and
    flaky ("Timed out waiting for Azure CLI"), which pollutes the ``connect``
    measurement and fails runs. We subclass the real credential (so the SDK still
    recognises it as an async credential) and only re-shell near expiry, so
    ``connect`` reflects the real TLS + WS handshake — an apples-to-apples
    APIM-vs-Direct comparison.
    """
    from azure.identity.aio import AzureCliCredential

    class CachingCliCredential(AzureCliCredential):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._cached = None

        async def get_token(self, *scopes, **kwargs):
            if self._cached is None or (self._cached.expires_on - time.time()) < 300:
                self._cached = await super().get_token(*scopes, **kwargs)
            return self._cached

    return CachingCliCredential()


async def run_sdk(modality: str, cred) -> dict:
    from azure.ai.voicelive.aio import connect
    from azure.ai.voicelive.models import (
        RequestSession, Modality, InputAudioFormat, OutputAudioFormat,
        UserMessageItem, InputTextContentPart,
    )

    mods, first_type = MODALITIES[modality]
    mod_enums = [Modality.AUDIO if m == "audio" else Modality.TEXT for m in mods]
    session_kwargs = {"modalities": mod_enums}
    if "audio" in mods:
        session_kwargs["input_audio_format"] = InputAudioFormat.PCM16
        session_kwargs["output_audio_format"] = OutputAudioFormat.PCM16

    t0 = time.perf_counter()
    async with connect(endpoint=FOUNDRY_HTTP, credential=cred, api_version=SDK_API_VERSION,
                       agent_name=AGENT, project_name=PROJECT) as conn:
        t_open = time.perf_counter()
        await conn.session.update(session=RequestSession(**session_kwargs))
        t_created = t_req = t_first = None
        text, audio = "", 0
        events = conn.__aiter__()
        while True:
            evt = await asyncio.wait_for(events.__anext__(), timeout=READ_TIMEOUT)
            ts = str(getattr(evt, "type", ""))
            if "session.updated" in ts and t_req is None:
                t_created = time.perf_counter()
                await conn.conversation.item.create(
                    item=UserMessageItem(content=[InputTextContentPart(text=PROMPT)]))
                await conn.response.create()
                t_req = time.perf_counter()
            elif "response.audio_transcript.delta" in ts or "response.text.delta" in ts:
                text += getattr(evt, "delta", "") or ""
                if first_type == "response.text.delta" and "response.text.delta" in ts and t_first is None:
                    t_first = time.perf_counter()
            elif "response.audio.delta" in ts:
                audio += 1
                if first_type == "response.audio.delta" and t_first is None:
                    t_first = time.perf_counter()
            elif "response.done" in ts:
                return _metrics(t0, t_open, t_created, t_req, t_first, text, audio)
            elif ts == "error" or ts.endswith(".error"):
                raise RuntimeError(str(getattr(evt, "error", evt)))


def _metrics(t0, t_open, t_created, t_req, t_first, text, audio) -> dict:
    t_done = time.perf_counter()
    return {
        "connect": (t_open - t0) * 1000,
        "session": (t_created - t_open) * 1000 if t_created else float("nan"),
        "first": (t_first - t_req) * 1000 if t_first and t_req else float("nan"),
        "done": (t_done - t_req) * 1000 if t_req else float("nan"),
        "e2e": (t_done - t0) * 1000,
        "audio_deltas": audio,
        "chars": len(text.strip()),
    }


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
METRIC_KEYS = ("connect", "session", "first", "done", "e2e")


async def bench(label: str, runner, iters: int) -> list[dict]:
    rows = []
    for i in range(iters):
        try:
            r = await runner()
            rows.append(r)
            print(f"  [{label}] {i+1}/{iters}: connect={r['connect']:.0f} session={r['session']:.0f} "
                  f"first={r['first']:.0f} done={r['done']:.0f} e2e={r['e2e']:.0f} (ms) chars={r['chars']}")
        except Exception as exc:
            print(f"  [{label}] {i+1} FAILED: {str(exc)[:160]}")
        await asyncio.sleep(1.0)
    return rows


def _median(vals):
    vals = sorted(v for v in vals if v == v)
    return round(statistics.median(vals), 1) if vals else None


def summarize(label: str, rows: list[dict]):
    print(f"\n=== {label}  (n={len(rows)}) ===")
    for key in METRIC_KEYS:
        vals = sorted(r[key] for r in rows if r[key] == r[key])
        if not vals:
            continue
        p95 = vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]
        print(f"  {key:8} median={statistics.median(vals):8.1f} ms   min={vals[0]:8.1f}   max={vals[-1]:8.1f}   p95={p95:8.1f}")


def write_reports(results: dict, iters: int):
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "generated_utc": stamp, "iterations": iters, "agent": AGENT, "project": PROJECT,
        "apim_api_version": API_VERSION, "sdk_api_version": SDK_API_VERSION,
        "runs": {k: {"rows": v} for k, v in results.items()},
    }
    (RESULTS_DIR / "bench-matrix.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Voice Live latency — APIM gateway vs direct SDK (same agent)",
        "",
        f"- Generated (UTC): `{stamp}`  ·  iterations per run: **{iters}**",
        f"- Agent: `{AGENT}` / project `{PROJECT}` (model gpt-5.4-mini, no model sent)",
        f"- APIM api-version `{API_VERSION}` · SDK api-version `{SDK_API_VERSION}`",
        "",
        "All times in ms (median across iterations).",
        "",
        "| # | Path | Modality | connect | session | first | done | e2e | n |",
        "|--:|------|----------|--------:|--------:|------:|-----:|----:|--:|",
    ]
    order = [
        ("1", "APIM gateway", "voice", "apim_voice"),
        ("2", "APIM gateway", "text", "apim_text"),
        ("3", "Direct SDK", "voice", "sdk_voice"),
        ("4", "Direct SDK", "text", "sdk_text"),
    ]
    for num, path, modality, key in order:
        rows = results.get(key, [])
        cells = [str(_median([r[m] for r in rows])) for m in METRIC_KEYS]
        lines.append(f"| {num} | {path} | {modality} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | {cells[4]} | {len(rows)} |")

    lines += ["", "## APIM overhead vs direct SDK (median APIM − median Direct)", ""]
    lines += ["| Modality | connect | session | first | done |", "|----------|--------:|--------:|------:|-----:|"]
    for modality, akey, skey in (("voice", "apim_voice", "sdk_voice"), ("text", "apim_text", "sdk_text")):
        a, s = results.get(akey, []), results.get(skey, [])
        row = [modality]
        for m in ("connect", "session", "first", "done"):
            am, sm = _median([r[m] for r in a]), _median([r[m] for r in s])
            row.append(f"{am - sm:+.1f}" if (am is not None and sm is not None) else "n/a")
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |")

    (RESULTS_DIR / "bench-matrix.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {RESULTS_DIR / 'bench-matrix.md'} and bench-matrix.json")


async def main():
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    print(f"agent={AGENT}  iterations={iters}  (APIM api={API_VERSION}, SDK api={SDK_API_VERSION})")

    from azure.identity.aio import AzureCliCredential  # noqa: F401 (kept for env probe)
    cred = _make_caching_cli_credential()
    try:
        results = {}
        print("\n########## 1. APIM gateway + VOICE ##########")
        results["apim_voice"] = await bench("APIM voice", lambda: run_apim("voice"), iters)
        print("\n########## 2. APIM gateway + TEXT ##########")
        results["apim_text"] = await bench("APIM text", lambda: run_apim("text"), iters)
        # Pre-warm the Entra token once so the first SDK connect isn't charged for
        # the `az` shell-out (kept out of the measured connect window).
        try:
            await cred.get_token(FOUNDRY_SCOPE)
        except Exception as exc:
            print(f"  [warn] could not pre-acquire Entra token: {str(exc)[:120]}")
        print("\n########## 3. Direct SDK + VOICE ##########")
        results["sdk_voice"] = await bench("SDK voice", lambda: run_sdk("voice", cred), iters)
        print("\n########## 4. Direct SDK + TEXT ##########")
        results["sdk_text"] = await bench("SDK text", lambda: run_sdk("text", cred), iters)
    finally:
        await cred.close()

    for label, key in (("1. APIM  + voice", "apim_voice"), ("2. APIM  + text", "apim_text"),
                       ("3. Direct+ voice", "sdk_voice"), ("4. Direct+ text", "sdk_text")):
        summarize(label, results[key])
    write_reports(results, iters)


if __name__ == "__main__":
    asyncio.run(main())
