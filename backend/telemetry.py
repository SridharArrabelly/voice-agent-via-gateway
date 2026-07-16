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

        FastAPIInstrumentor.instrument_app(app)

    _ENABLED = True
    logger.info("Application Insights tracing enabled.")
    return True


def get_tracer():
    return trace.get_tracer("voice-agent.backend")


def enabled() -> bool:
    return _ENABLED
