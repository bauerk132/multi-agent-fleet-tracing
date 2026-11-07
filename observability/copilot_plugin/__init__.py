"""Copilot observer — Milestone 4 (thin adapter).

Wires Copilot's native OpenTelemetry export to a CopilotObserver (see
observer.py in this package, which holds all the logic and is fully
testable with synthetic OTLP payloads).

There is NO host plugin to install: Copilot Chat exposes no hook /
callback registration surface (unlike Hermes). The owner opts in by
configuring Copilot's own OTel export — see ``config.md`` in this
package for the exact VS Code settings and environment variables.
This adapter only consumes what Copilot exports:

- ``handle_payload(payload)`` — an OTLP/HTTP JSON payload
  (``resourceSpans``), the shape the local receiver flattens.
- ``ingest_jsonl(path)`` — the documented file exporter
  (``github.copilot.chat.otel.outfile`` /
  ``COPILOT_OTEL_FILE_EXPORTER_PATH``), one JSON value per line.

Importing this package registers nothing and starts nothing. Only
``create_observer(endpoint)`` builds an observer (process-scoped
provider + worker), and only the owner enables Copilot telemetry, in
VS Code settings — never from this code. Do NOT enable content capture
or identity capture: the adapter strips those attributes even if they
arrive, but the correct control is to leave them off upstream.

OpenTelemetry guard (mirrors M3 FR5-02): importing this package never
crashes the host — only stdlib imports happen at import time. The OTel
dependency is pinned 1.45.1, same as observability/requirements.txt.
If OTel is absent at create_observer() time, it raises a clear
RuntimeError naming the missing dependency instead of leaking an
ImportError traceback.
"""

from __future__ import annotations

import os

# Documented Copilot span kinds (plan §5; official VS Code docs). The
# adapter maps each to a fleet span; unknown kinds are never emitted.
# Keep in sync with OPERATIONS in observer.py.
SPAN_KINDS = (
    "invoke_agent",
    "chat",
    "execute_tool",
    "execute_hook",
)


def create_observer(endpoint=None, **kwargs):
    """Create a CopilotObserver. Returns the observer.

    The OTLP endpoint comes from the argument, or
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (loopback default); no
    credentials are read or stored. The observer is owned by the
    caller; call observer.shutdown() at teardown — never from an
    ingest callback.

    If OpenTelemetry is not installed, raises RuntimeError with a
    clear message naming the missing dependency instead of leaking an
    ImportError traceback.
    """
    try:
        from .observer import CopilotObserver
    except ImportError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing.startswith("opentelemetry") or "opentelemetry" in str(exc):
            raise RuntimeError(
                "fleet-trace-copilot-observer: OpenTelemetry is not installed; "
                "install opentelemetry-api==1.45.1, opentelemetry-sdk==1.45.1, "
                "opentelemetry-exporter-otlp-proto-http==1.45.1 and retry."
            ) from None
        raise

    if endpoint is None:
        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"
        )
    return CopilotObserver(endpoint=endpoint, **kwargs)
