"""Application Insights / OpenTelemetry setup for the voice-agent backend.

Enables distributed tracing to Azure Application Insights via the Azure Monitor
OpenTelemetry distro. When ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set the
FastAPI app is auto-instrumented (HTTP requests) and we expose a tracer so the
WebSocket relay can record a span per voice session.

If the connection string is absent, tracing is a no-op so the app still runs.
"""
from __future__ import annotations

import logging
import os

from opentelemetry import trace

logger = logging.getLogger("voice-agent.telemetry")

_ENABLED = False


def configure(app=None) -> bool:
    """Configure Azure Monitor OTel. Returns True when tracing is enabled."""
    global _ENABLED
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not conn:
        logger.info("APPLICATIONINSIGHTS_CONNECTION_STRING not set — tracing disabled.")
        return False

    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=conn,
        service_name=os.environ.get("OTEL_SERVICE_NAME", "voice-agent-backend"),
    )

    if app is not None:
        # Instrument FastAPI so each HTTP request is traced too.
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Suppress the per-frame ASGI "websocket send/receive" child spans. On the
        # audio relay each base64 audio delta is one WS frame, so leaving these on
        # emits hundreds of empty 0 ms spans per turn — pure noise that also floods
        # the BatchSpanProcessor queue and can evict the meaningful `voice.turn`
        # span (emitted at `response.done`, i.e. at peak audio flow). Excluding them
        # keeps only the real request/session/turn spans.
        try:
            FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
        except TypeError:
            # Older instrumentation without exclude_spans support.
            FastAPIInstrumentor.instrument_app(app)

    _ENABLED = True
    logger.info("Application Insights tracing enabled.")
    return True


def get_tracer():
    return trace.get_tracer("voice-agent.backend")


def flush(timeout_ms: int = 5000) -> None:
    """Force-export any buffered spans. Call on shutdown so the last turns of a
    session are never lost when the process stops."""
    if not _ENABLED:
        return
    try:
        provider = trace.get_tracer_provider()
        force = getattr(provider, "force_flush", None)
        if callable(force):
            force(timeout_ms)
    except Exception:  # best-effort; never block shutdown
        logger.debug("tracer flush on shutdown failed", exc_info=True)


def enabled() -> bool:
    return _ENABLED
