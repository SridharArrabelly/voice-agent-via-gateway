"""Agent QA harness — runs a fixed question set through the configured agent in
BOTH modalities and records the results.

Both paths go through the local Python backend proxy (so Application Insights also
records a `voice.session` span per run):

    Browser-facing WS  ->  backend  ->  APIM  ->  Voice Live (Foundry agent + web tool)

  VOICE : session modalities ["audio", "text"]  -> transcript + audio deltas
  TEXT  : session modalities ["text"]           -> text response, no audio

For each question we capture:
  - answer text (transcript)
  - audio_deltas (voice only)
  - ttfr_ms  : question sent -> first response token/audio  (responsiveness)
  - total_ms : question sent -> response.done

Results are written to results/qa-results.json and results/qa-results.md.

    uv run python scripts/test_qa.py            # needs the backend running
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

URL = os.environ.get("BACKEND_WS_URL", "ws://127.0.0.1:8000/realtime")
AGENT = os.environ.get("AGENT_NAME", "the configured agent")
RESULTS_DIR = ROOT / "results"
TURN_TIMEOUT = 90  # seconds; web-tool turns can take a while

QUESTIONS = [
    "What is the latest stable version of Python, and roughly when was it released?",
    "Who is the current CEO of OpenAI?",
    "What is today's date?",
    "Who won the most recent FIFA World Cup, and in what year?",
    "What is the latest iPhone model Apple has released?",
    "What is the current approximate price of Bitcoin in US dollars?",
    "Who is the current Secretary-General of the United Nations?",
    "Name one major AI model that was released recently.",
    "What is the capital city of Australia?",
    "What is the weather like in London today?",
]


async def _drain_until(ws, wanted_types, timeout):
    """Yield parsed messages until one of wanted_types is seen or timeout."""
    deadline = time.perf_counter() + timeout
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            yield {"type": "response.audio.delta"}
            continue
        msg = json.loads(raw)
        yield msg
        if msg.get("type") in wanted_types:
            return


def _text_from_done(msg):
    """Fallback: pull the assistant answer out of a response.done payload
    (used when we miss the streaming deltas due to throttling/races)."""
    out = []
    resp = (msg or {}).get("response") or {}
    for item in resp.get("output") or []:
        for part in item.get("content") or []:
            out.append(part.get("text") or part.get("transcript") or "")
    return "".join(out).strip()


async def run_turn(ws, question):
    t0 = time.perf_counter()
    ttfr = None
    text = ""
    audio = 0
    error = None
    done_msg = None
    await ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": question}]},
    }))
    await ws.send(json.dumps({"type": "response.create"}))
    try:
        async for m in _drain_until(ws, {"response.done", "error"}, TURN_TIMEOUT):
            t = m.get("type")
            if t in ("response.audio_transcript.delta", "response.text.delta"):
                text += m.get("delta", "")
                if ttfr is None:
                    ttfr = time.perf_counter()
            elif t == "response.audio.delta":
                audio += 1
                if ttfr is None:
                    ttfr = time.perf_counter()
            elif t == "response.done":
                done_msg = m
            elif t == "error":
                error = m.get("error", m)
                break
    except asyncio.TimeoutError:
        error = {"message": f"timeout after {TURN_TIMEOUT}s"}
    # If we streamed no deltas, recover the answer from the final response.done.
    if not text and done_msg is not None:
        text = _text_from_done(done_msg)
    total = time.perf_counter() - t0
    return {
        "question": question,
        "answer": text.strip(),
        "audio_deltas": audio,
        "ttfr_ms": round((ttfr - t0) * 1000, 1) if ttfr else None,
        "total_ms": round(total * 1000, 1),
        "error": error,
    }


async def run_session(modality, modalities):
    print(f"\n===== {modality.upper()} session (modalities={modalities}) =====")
    connect_t0 = time.perf_counter()
    async with websockets.connect(URL, max_size=None) as ws:
        # wait for session.created
        async for m in _drain_until(ws, {"session.created", "error"}, 30):
            if m.get("type") == "error":
                raise RuntimeError(f"session error: {m}")
        connect_ms = round((time.perf_counter() - connect_t0) * 1000, 1)
        await ws.send(json.dumps({"type": "session.update", "session": {"modalities": modalities}}))
        await asyncio.sleep(0.5)  # let the update apply

        rows = []
        for i, q in enumerate(QUESTIONS, 1):
            r = await run_turn(ws, q)
            # Retry with growing backoff if the turn came back empty without an
            # explicit error (transient throttling on back-to-back web-tool turns).
            for backoff in (6.0, 10.0):
                if r["answer"] or r["error"]:
                    break
                await asyncio.sleep(backoff)
                r = await run_turn(ws, q)
            r["modality"] = modality
            rows.append(r)
            status = "ERR" if r["error"] else ("ok " if r["answer"] else "---")
            snippet = (r["answer"][:70] + "…") if len(r["answer"]) > 70 else r["answer"]
            print(f"  [{status}] Q{i:2} ttfr={str(r['ttfr_ms']):>7} total={r['total_ms']:>7}  {snippet!r}")
            await asyncio.sleep(3.0)  # pace turns to avoid throttling
    return {"modality": modality, "connect_ms": connect_ms, "turns": rows}


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 1)


def write_reports(sessions):
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    payload = {"generated_utc": stamp, "gateway_ws": URL, "sessions": sessions}
    (RESULTS_DIR / "qa-results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Agent QA results — voice vs text",
        "",
        f"- Generated (UTC): `{stamp}`",
        f"- Path: browser WS → Python backend → APIM → Voice Live (agent `{AGENT}` + web tool)",
        "",
        "## Latency summary (median across questions)",
        "",
        "| Modality | connect (ms) | ttfr median (ms) | total median (ms) | errors |",
        "|----------|-------------:|-----------------:|------------------:|-------:|",
    ]
    for s in sessions:
        turns = s["turns"]
        errs = sum(1 for t in turns if t["error"])
        lines.append(
            f"| {s['modality']} | {s['connect_ms']} | "
            f"{_median([t['ttfr_ms'] for t in turns])} | "
            f"{_median([t['total_ms'] for t in turns])} | {errs} |"
        )

    for s in sessions:
        lines += ["", f"## {s['modality'].capitalize()} — per question", ""]
        for i, t in enumerate(s["turns"], 1):
            lines.append(f"### Q{i}. {t['question']}")
            if t["error"]:
                lines.append(f"- **ERROR:** `{t['error']}`")
            else:
                lines.append(f"- ttfr={t['ttfr_ms']} ms · total={t['total_ms']} ms · audio_deltas={t['audio_deltas']}")
                lines.append(f"- Answer: {t['answer'] or '(no text)'}")
            lines.append("")
    (RESULTS_DIR / "qa-results.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {RESULTS_DIR / 'qa-results.md'} and qa-results.json")


async def main():
    sessions = []
    sessions.append(await run_session("voice", ["audio", "text"]))
    await asyncio.sleep(5.0)  # let the service settle before the text session
    sessions.append(await run_session("text", ["text"]))
    write_reports(sessions)


if __name__ == "__main__":
    asyncio.run(main())
