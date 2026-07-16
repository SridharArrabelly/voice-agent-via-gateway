"""
Voice Agent backend — Python WebSocket proxy.

Architecture:
    Browser (JS)  --ws-->  this backend (/realtime)  --wss + subscription-key-->
    APIM gateway  --managed-identity Entra token-->  Azure Voice Live (Foundry agent)

The APIM subscription key stays here on the server and is never exposed to the browser.
The backend also serves the static frontend in ../web.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# Load configuration from the project-root .env before reading any settings.
load_dotenv(ROOT / ".env")

from telemetry import configure as configure_telemetry, get_tracer  # noqa: E402
from opentelemetry import trace  # noqa: E402

# --- config (all values come from .env / environment) ---
GATEWAY_WS_URL = os.environ.get(
    "GATEWAY_WS_URL", "wss://apim-ai-gateway-sweden.azure-api.net/voice-agent"
)


def _load_subscription_key() -> str:
    key = os.environ.get("APIM_SUBSCRIPTION_KEY")
    if key:
        return key.strip()
    raise RuntimeError(
        "No APIM subscription key. Set APIM_SUBSCRIPTION_KEY in .env (see .env.example)."
    )


SUBSCRIPTION_KEY = _load_subscription_key()
UPSTREAM_URL = f"{GATEWAY_WS_URL}?subscription-key={SUBSCRIPTION_KEY}"

app = FastAPI(title="voice-agent-backend")

# Enable Application Insights tracing (no-op if the connection string is unset).
configure_telemetry(app)
tracer = get_tracer()


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "gateway": GATEWAY_WS_URL}


@app.websocket("/realtime")
async def realtime(client: WebSocket):
    """Relay a browser WS session to the Voice Live agent through APIM.

    The whole session is recorded as a single `voice.session` span in
    Application Insights, with lifecycle events (upstream connect, server errors,
    close) and per-session message/audio counters.
    """
    await client.accept()
    peer = f"{client.client.host}:{client.client.port}" if client.client else "unknown"

    with tracer.start_as_current_span("voice.session") as span:
        span.set_attribute("net.peer", peer)
        span.set_attribute("gateway.url", GATEWAY_WS_URL)
        started = time.perf_counter()
        counters = {"client_to_upstream": 0, "audio_deltas": 0, "server_events": 0}

        connect_t0 = time.perf_counter()
        try:
            upstream = await websockets.connect(UPSTREAM_URL, max_size=None)
        except Exception as exc:  # upstream handshake failed
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "upstream connect failed"))
            await client.close(code=1011, reason=f"upstream connect failed: {exc}"[:120])
            return
        span.add_event(
            "upstream.connected",
            {"connect_ms": round((time.perf_counter() - connect_t0) * 1000, 1)},
        )

        async def client_to_upstream():
            try:
                while True:
                    msg = await client.receive_text()
                    counters["client_to_upstream"] += 1
                    await upstream.send(msg)
            except WebSocketDisconnect:
                pass

        async def upstream_to_client():
            try:
                async for msg in upstream:
                    if isinstance(msg, bytes):
                        counters["audio_deltas"] += 1
                        await client.send_bytes(msg)
                    else:
                        counters["server_events"] += 1
                        _record_server_event(span, msg, counters)
                        await client.send_text(msg)
            except websockets.ConnectionClosed:
                pass

        c2u = asyncio.create_task(client_to_upstream())
        u2c = asyncio.create_task(upstream_to_client())
        try:
            _, pending = await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            await upstream.close()
            try:
                await client.close()
            except RuntimeError:
                pass
            span.set_attribute("session.duration_ms", round((time.perf_counter() - started) * 1000, 1))
            span.set_attribute("session.client_messages", counters["client_to_upstream"])
            span.set_attribute("session.audio_deltas", counters["audio_deltas"])
            span.set_attribute("session.server_events", counters["server_events"])
            span.add_event("session.closed", dict(counters))


# Voice Live server event types worth recording as span events (deltas are noisy,
# so they are only counted, not logged individually).
_TRACE_EVENT_TYPES = {
    "session.created",
    "session.updated",
    "response.created",
    "response.done",
    "response.audio_transcript.done",
    "conversation.item.input_audio_transcription.completed",
    "error",
}


def _record_server_event(span, raw: str, counters: dict) -> None:
    """Parse an upstream text frame and record interesting events on the span."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    etype = obj.get("type")
    if not etype:
        return
    if etype == "error":
        err = obj.get("error", obj)
        span.add_event("voice.error", {"message": str(err)[:400]})
        span.set_status(trace.Status(trace.StatusCode.ERROR, str(err.get("message", "error"))[:200]))
        return
    if etype in _TRACE_EVENT_TYPES:
        attrs = {"type": etype}
        transcript = obj.get("transcript")
        if transcript:
            attrs["transcript"] = str(transcript)[:400]
        span.add_event(f"voice.{etype}", attrs)


# Static frontend mounted last so /realtime and /healthz win first.
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BACKEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("BACKEND_PORT", "8000")),
    )
