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
import uuid
from pathlib import Path

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# Load configuration from the project-root .env before reading any settings.
load_dotenv(ROOT / ".env")

from telemetry import configure as configure_telemetry, get_tracer, flush as flush_telemetry  # noqa: E402
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

# --- Model mode (native speech-to-speech, e.g. gpt-realtime) — OPTIONAL ---
# A second, fully isolated gateway API + subscription. When both are set, the
# backend exposes a parallel /realtime-model route. Agent mode is unaffected.
GATEWAY_WS_URL_MODEL = os.environ.get("GATEWAY_WS_URL_MODEL")
MODEL_SUBSCRIPTION_KEY = os.environ.get("APIM_SUBSCRIPTION_KEY_MODEL")
UPSTREAM_URL_MODEL = (
    f"{GATEWAY_WS_URL_MODEL}?subscription-key={MODEL_SUBSCRIPTION_KEY}"
    if GATEWAY_WS_URL_MODEL and MODEL_SUBSCRIPTION_KEY
    else None
)
MODEL_MODE = UPSTREAM_URL_MODEL is not None

# --- Voice / session config (all OPTIONAL) ---
# Agent mode: leave VOICE_TYPE unset to use the agent's own configured voice.
# Custom Neural Voice (CNV) plugs in here via VOICE_TYPE=azure-custom (agent mode only).
VOICE_TYPE = (os.environ.get("VOICE_TYPE") or "").strip()          # standard | azure-custom
VOICE_NAME = (os.environ.get("VOICE_NAME") or "").strip()
VOICE_CUSTOM_ENDPOINT_ID = (os.environ.get("VOICE_CUSTOM_ENDPOINT_ID") or "").strip()
# Model mode has no Foundry agent, so the backend supplies the system prompt + voice.
MODEL_MODE_INSTRUCTIONS = (
    os.environ.get("MODEL_MODE_INSTRUCTIONS")
    or "You are a helpful, concise voice assistant. Keep answers to one or two short sentences."
)
MODEL_MODE_VOICE = (os.environ.get("MODEL_MODE_VOICE") or "").strip()


def _build_voice(mode: str) -> dict | None:
    """Build the Voice Live `voice` object for a session, or None to keep the default.

    Custom Neural Voice (`azure-custom`) lives in the cascaded TTS stage, which only
    exists in AGENT mode. Native model mode (gpt-realtime) has no swappable TTS stage,
    so a CNV request there is ignored (built-in voices only).
    """
    if mode == "model":
        if VOICE_TYPE == "azure-custom":
            print("[voice] CNV (azure-custom) is not supported in model mode — ignoring; "
                  "use agent mode for Custom Neural Voice.")
        if MODEL_MODE_VOICE:
            return {"name": MODEL_MODE_VOICE, "type": "azure-standard"}
        return None
    # agent mode
    if not VOICE_TYPE:
        return None
    if VOICE_TYPE == "azure-custom":
        voice = {"name": VOICE_NAME, "type": "azure-custom"}
        if VOICE_CUSTOM_ENDPOINT_ID:
            voice["endpoint_id"] = VOICE_CUSTOM_ENDPOINT_ID
        return voice
    return {"name": VOICE_NAME, "type": "azure-standard"}


def _server_session_update(mode: str) -> dict | None:
    """Server-side `session.update` injected right after the upstream connects.

    Keeps voice + instructions on the server (out of the browser). Model mode always
    needs instructions + turn detection (no agent); agent mode only overrides the voice
    when VOICE_TYPE is configured.
    """
    session: dict = {}
    voice = _build_voice(mode)
    if voice:
        session["voice"] = voice
    if mode == "model":
        session["modalities"] = ["text", "audio"]
        session["instructions"] = MODEL_MODE_INSTRUCTIONS
        session["turn_detection"] = {"type": "server_vad"}
    if not session:
        return None
    return {"type": "session.update", "session": session}

app = FastAPI(title="voice-agent-backend")

# Enable Application Insights tracing (no-op if the connection string is unset).
configure_telemetry(app)
tracer = get_tracer()


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "gateway": GATEWAY_WS_URL,
        "model_mode": MODEL_MODE,
        "gateway_model": GATEWAY_WS_URL_MODEL,
    }


@app.get("/modes")
async def modes():
    """Which voice modes the backend can serve — the UI uses this to build its toggle."""
    return {
        "agent": {"available": True, "route": "/realtime", "label": "Agent (cascaded)"},
        "model": {"available": MODEL_MODE, "route": "/realtime-model", "label": "gpt-realtime (native S2S)"},
    }


@app.on_event("shutdown")
def _flush_telemetry_on_shutdown() -> None:
    # Export any buffered spans so the final turns of a session are not lost.
    flush_telemetry()


# APIM (and Foundry) return one of these request-id headers on the WS handshake.
# We stamp whatever is present onto the session span so an App Insights trace can be
# joined to the matching row in `ApiManagementGatewayLogs` (column CorrelationId /
# RequestId) — the cross-hop correlation key.
_APIM_ID_HEADERS = (
    "apim-request-id",
    "x-ms-request-id",
    "request-id",
    "x-ms-correlation-request-id",
)


class TurnProfiler:
    """Reconstructs a per-turn latency timeline from the Voice Live event stream.

    A "turn" spans from the user's speech (or the response being created) to
    `response.done`. For each turn we emit a child span `voice.turn` under the
    session span, carrying stage durations (ASR, reasoning, tool, first-audio) plus
    the client-perceived mouth-to-ear wait when the browser posts `client.*` marks.
    Stage deltas use a monotonic clock; span start/end use epoch time so the App
    Insights end-to-end transaction view renders a correct waterfall.
    """

    _STAGE_EVENTS = {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.committed",
        "conversation.item.input_audio_transcription.completed",
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.text.delta",
        "response.audio_transcript.delta",
        "response.audio.delta",
        "response.audio.done",
        "response.done",
    }

    def __init__(self, tracer, session_span, session_id: str, mode: str = "agent"):
        self._tracer = tracer
        self._parent_ctx = trace.set_span_in_context(session_span)
        self._session_id = session_id
        self._mode = mode
        self._index = 0
        self._reset()

    def _reset(self) -> None:
        self._active = False
        self._start_epoch_ns = None
        self._m: dict[str, float] = {}          # stage -> perf_counter seconds
        self._transcript = ""
        self._agent_text = ""
        self._tool_used = False
        self._usage: dict | None = None
        self._client_marks: dict[str, float] = {}  # mark -> browser epoch ms

    def _mark(self, name: str) -> None:
        self._m.setdefault(name, time.perf_counter())

    def _begin(self) -> None:
        if not self._active:
            self._active = True
            self._start_epoch_ns = time.time_ns()

    def note_client_mark(self, mark: str, client_ts_ms: float) -> None:
        self._begin()
        self._client_marks[mark] = client_ts_ms

    def on_audio_frame(self) -> None:
        """First binary audio frame of a turn = TTS audio started arriving."""
        if self._active:
            self._mark("first_audio")

    def on_text_event(self, obj: dict) -> None:
        etype = obj.get("type")
        if etype not in self._STAGE_EVENTS:
            return
        if etype == "input_audio_buffer.speech_started":
            self._begin()
            self._mark("speech_started")
        elif etype == "input_audio_buffer.speech_stopped":
            self._begin()
            self._mark("speech_stopped")
        elif etype == "input_audio_buffer.committed":
            self._begin()
            self._mark("committed")
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._begin()
            self._mark("asr")
            self._transcript = str(obj.get("transcript", ""))[:400]
        elif etype == "response.created":
            self._begin()
            self._mark("created")
        elif etype == "response.output_item.added":
            item = obj.get("item") or {}
            if item.get("type") == "mcp_call":
                self._tool_used = True
                self._mark("tool_start")
        elif etype == "response.output_item.done":
            item = obj.get("item") or {}
            if item.get("type") == "mcp_call":
                self._mark("tool_end")
        elif etype in ("response.text.delta", "response.audio_transcript.delta"):
            self._mark("first_token")
            delta = obj.get("delta")
            if delta and len(self._agent_text) < 400:
                self._agent_text += str(delta)
        elif etype == "response.audio.delta":
            # Audio streams as base64 JSON deltas (not binary frames) on this path,
            # so the first one marks TTS audio start.
            self._mark("first_token")   # audio-only turns have no text delta
            self._mark("first_audio")
        elif etype == "response.audio.done":
            self._mark("audio_done")
        elif etype == "response.done":
            self._mark("done")
            resp = obj.get("response") or {}
            self._usage = resp.get("usage")
            self._emit()

    @staticmethod
    def _delta_ms(m: dict, end: str, start: str) -> float | None:
        if end in m and start in m:
            return round((m[end] - m[start]) * 1000, 1)
        return None

    def _emit(self) -> None:
        if not self._active or self._start_epoch_ns is None:
            self._reset()
            return
        self._index += 1
        m = self._m
        ref_start = "speech_stopped" if "speech_stopped" in m else (
            "committed" if "committed" in m else (
                "speech_started" if "speech_started" in m else "created"))

        span = self._tracer.start_span(
            "voice.turn", context=self._parent_ctx, start_time=self._start_epoch_ns
        )
        try:
            span.set_attribute("session.id", self._session_id)
            span.set_attribute("voice.mode", self._mode)
            span.set_attribute("turn.index", self._index)
            if self._transcript:
                span.set_attribute("turn.transcript", self._transcript)
            if self._agent_text:
                span.set_attribute("turn.agent_transcript", self._agent_text[:400])
            span.set_attribute("turn.tool_used", self._tool_used)

            stages = {
                "asr_ms": self._delta_ms(m, "asr", ref_start),
                "reasoning_ms": self._delta_ms(m, "first_token", "created"),
                "tool_ms": self._delta_ms(m, "tool_end", "tool_start"),
                "tts_first_ms": self._delta_ms(m, "first_audio", "first_token"),
                "total_ms": self._delta_ms(m, "done", ref_start),
            }
            for k, v in stages.items():
                if v is not None:
                    span.set_attribute(f"turn.{k}", v)

            ss, fa = self._client_marks.get("speech_stopped"), self._client_marks.get("first_audio_played")
            if ss is not None and fa is not None:
                span.set_attribute("turn.client_wait_ms", round(fa - ss, 1))
            if "barge_in" in self._client_marks:
                span.set_attribute("turn.barge_in", True)

            if isinstance(self._usage, dict):
                for tk in ("total_tokens", "input_tokens", "output_tokens"):
                    if tk in self._usage:
                        span.set_attribute(f"turn.usage.{tk}", self._usage[tk])

            span.add_event("voice.turn.stages", {k: v for k, v in stages.items() if v is not None})
        finally:
            span.end(end_time=time.time_ns())
        self._reset()


@app.websocket("/realtime")
async def realtime(client: WebSocket):
    """Agent mode: relay the browser to the Foundry agent via APIM (cascaded STT→agent→TTS)."""
    await _proxy(client, mode="agent", upstream_url=UPSTREAM_URL)


@app.websocket("/realtime-model")
async def realtime_model(client: WebSocket):
    """Model mode: relay the browser to a native speech-to-speech model (e.g. gpt-realtime).

    Uses the separate `voice-agent-model` APIM API + subscription, so it never collides
    with agent mode. Returns 1011 if model mode is not configured in `.env`.
    """
    if not MODEL_MODE:
        await client.accept()
        await client.close(code=1011, reason="model mode not configured (set GATEWAY_WS_URL_MODEL + APIM_SUBSCRIPTION_KEY_MODEL)")
        return
    await _proxy(client, mode="model", upstream_url=UPSTREAM_URL_MODEL)


async def _proxy(client: WebSocket, mode: str, upstream_url: str):
    """Relay a browser WS session to Voice Live through APIM, in the given mode.

    The whole session is one `voice.session` span (tagged with `voice.mode`) in
    Application Insights, with lifecycle events, per-session counters and per-turn
    `voice.turn` child spans. `mode` is "agent" (cascaded) or "model" (native S2S);
    both share this identical proxy — only the upstream URL and the injected
    server-side `session.update` differ.
    """
    await client.accept()
    peer = f"{client.client.host}:{client.client.port}" if client.client else "unknown"
    gateway_url = GATEWAY_WS_URL_MODEL if mode == "model" else GATEWAY_WS_URL

    with tracer.start_as_current_span("voice.session") as span:
        session_id = uuid.uuid4().hex
        span.set_attribute("session.id", session_id)
        span.set_attribute("voice.mode", mode)
        span.set_attribute("net.peer", peer)
        span.set_attribute("gateway.url", gateway_url)
        started = time.perf_counter()
        counters = {"client_to_upstream": 0, "audio_deltas": 0, "server_events": 0}
        profiler = TurnProfiler(tracer, span, session_id, mode)

        connect_t0 = time.perf_counter()
        try:
            upstream = await websockets.connect(upstream_url, max_size=None)
        except Exception as exc:  # upstream handshake failed
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "upstream connect failed"))
            await client.close(code=1011, reason=f"upstream connect failed: {exc}"[:120])
            return
        # Capture the APIM handshake request-id → cross-hop join key with the gateway logs.
        try:
            hs_headers = upstream.response.headers
            for h in _APIM_ID_HEADERS:
                val = hs_headers.get(h)
                if val:
                    span.set_attribute(f"apim.{h}", val)
        except Exception:
            pass
        span.add_event(
            "upstream.connected",
            {"connect_ms": round((time.perf_counter() - connect_t0) * 1000, 1)},
        )

        # Inject the server-side session.update (voice + — for model mode — instructions).
        # Kept on the server so the browser stays secret-free and config-free.
        server_update = _server_session_update(mode)
        if server_update is not None:
            try:
                await upstream.send(json.dumps(server_update))
                span.add_event(
                    "server.session_update",
                    {"keys": ",".join(server_update["session"].keys())},
                )
            except Exception as exc:
                span.record_exception(exc)

        async def client_to_upstream():
            try:
                while True:
                    msg = await client.receive_text()
                    # Intercept browser-only control frames (client.* marks): record
                    # them on the turn timeline but never forward them upstream —
                    # Voice Live would reject unknown message types and close the socket.
                    if _handle_client_mark(msg, profiler):
                        continue
                    counters["client_to_upstream"] += 1
                    await upstream.send(msg)
            except WebSocketDisconnect:
                pass

        async def upstream_to_client():
            try:
                async for msg in upstream:
                    if isinstance(msg, bytes):
                        counters["audio_deltas"] += 1
                        profiler.on_audio_frame()
                        await client.send_bytes(msg)
                    else:
                        counters["server_events"] += 1
                        _record_server_event(span, msg, counters, profiler)
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


def _handle_client_mark(raw: str, profiler: "TurnProfiler") -> bool:
    """Intercept a browser `client.*` control frame. Returns True if handled
    (and therefore must NOT be forwarded upstream)."""
    if '"client.' not in raw:
        return False
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    etype = obj.get("type", "")
    if not isinstance(etype, str) or not etype.startswith("client."):
        return False
    mark = etype.split(".", 1)[1]  # e.g. "speech_stopped", "first_audio_played", "barge_in"
    ts = obj.get("t")
    try:
        ts = float(ts) if ts is not None else time.time() * 1000
    except (TypeError, ValueError):
        ts = time.time() * 1000
    profiler.note_client_mark(mark, ts)
    return True


def _record_server_event(span, raw: str, counters: dict, profiler: "TurnProfiler | None" = None) -> None:
    """Parse an upstream text frame and record interesting events on the span."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if profiler is not None:
        profiler.on_text_event(obj)
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
