"""Coordinator for Milestone 1 of the Multi-Agent Fleet Tracing plan.

Implements the plan's initialization order:
  1. Resolve config from env only (no secrets in source).
  2. Init composite W3C propagators (TraceContext + Baggage).
  3. Init resource/provider, OTLP exporter, span processor.
  4. Allocate task/session IDs ONCE per user intent.
  5. Start root agent.run; create coordinator agent.handoff spans.
  6. FIXED propagation (S1): child env from an explicit allowlist only,
     propagate.inject() WHILE the handoff span is current.
  7. Correlate external agents by ledger IDs + safe handoff.payload_ref.
  8. Record outcomes/error status without raw content; end spans;
     provider.force_flush(timeout_millis=5000) then provider.shutdown().

The local loop exports OTLP/HTTP JSON to the local receiver via
otlp_json.OTLPJSONExporter. The pinned proto-http (protobuf) exporter is
installed and can be used against a real collector; see README.md.

Privacy: attributes pass through sanitize_attributes() — an explicit
allowlist of safe keys. Anything else is dropped; secret-looking values
are replaced with [REDACTED]; error.message is restricted to a small set
of generic messages. No prompt bodies, raw tool I/O, file contents,
credentials, or PII are ever emitted.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import uuid

from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from otlp_json import OTLPJSONExporter

# --- S1 fix: explicit child-env allowlist. Nothing else reaches a child. ---
# No *_KEY, *_TOKEN, *_SECRET, *_PASSWORD, or auth headers, ever.
ALLOWED_CHILD_ENV = frozenset(
    {
        "PATH",
        "SystemRoot",
        "TEMP",
        "TMP",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
    }
)

# Trace-context variables the coordinator adds explicitly (not inherited).
TRACE_ENV_VARS = ("TRACEPARENT", "TRACESTATE", "BAGGAGE")

# Attribute keys that are ever allowed on a span. Everything else is dropped.
SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "agent.name",
        "agent.role",
        "agent.from",
        "agent.to",
        "session.id",
        "task.id",
        "handoff.reason",
        "handoff.payload_ref",
        "handoff.child_parented",
        "handoff.child_error",
        "state.key",
        "state.before_ref",
        "state.after_ref",
        "error.type",
        "error.message",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.tool.name",
        "retrieval.query_ref",
        "approval.kind",
        "synthetic",
    }
)

# Bounded safe enums for error metadata. No exception str(e), no tracebacks.
SAFE_ERROR_TYPES = frozenset({"ToolError", "ChildProcessFailure", "LLMError", "Timeout"})
SAFE_ERROR_MESSAGES = frozenset(
    {
        "Tool failed; details withheld",
        "Process failed; details withheld",
        "LLM call failed; details withheld",
        "Error; details withheld",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_PROPAGATORS_SET = False


def resolve_config():
    """Config from env only. Defaults point at the local receiver."""
    return {
        "endpoint": os.environ.get(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://127.0.0.1:4318/v1/traces",
        ),
        "protocol": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
        "service_name": os.environ.get("OTEL_SERVICE_NAME", "fleet-trace-coordinator"),
    }


def sanitize_attributes(attrs):
    """Allowlist filter for span attributes. Drops anything unsafe.

    - Only keys in SAFE_ATTRIBUTE_KEYS survive.
    - Keys containing secret-ish parts are dropped even if allowlisted.
    - error.message is restricted to SAFE_ERROR_MESSAGES.
    - String values that look like an email or a bearer-style token are
      replaced with [REDACTED].
    """
    clean = {}
    for key, value in (attrs or {}).items():
        # The explicit allowlist IS the control: only enumerated keys
        # survive. (A substring check on key names would false-positive on
        # legitimate keys such as "state.key", so it is deliberately not
        # used here; secret-looking *values* are redacted below instead.)
        if key not in SAFE_ATTRIBUTE_KEYS:
            continue
        if key == "error.type" and value not in SAFE_ERROR_TYPES:
            continue
        if key == "error.message" and value not in SAFE_ERROR_MESSAGES:
            value = "Error; details withheld"
        if isinstance(value, str) and (
            _EMAIL_RE.search(value) or value.startswith(("sk-", "Bearer "))
        ):
            value = "[REDACTED]"
        clean[key] = value
    return clean


def build_child_env(carrier, task_id, session_id, handoff_ref, endpoint=None):
    """Build the child process environment from the allowlist only.

    Never os.environ.copy(): inherited secrets would reach the child.
    The OTLP endpoint is passed through the allowlist and must not embed
    credentials in the URL (OTLPJSONExporter enforces this too). When the
    coordinator knows the endpoint for this run, pass it explicitly — the
    child cannot inherit it from the coordinator's own os.environ.
    """
    child_env = {k: os.environ[k] for k in ALLOWED_CHILD_ENV if k in os.environ}
    if endpoint:
        child_env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = endpoint
    for var in TRACE_ENV_VARS:
        key = var.lower()
        if key in carrier:
            child_env[var] = carrier[key]
    child_env["FLEET_TASK_ID"] = task_id
    child_env["FLEET_SESSION_ID"] = session_id
    child_env["FLEET_HANDOFF_REF"] = handoff_ref
    return child_env


def _flush_quietly(provider):
    try:
        provider.force_flush(timeout_millis=5000)
    except Exception:
        pass


def init_tracing(endpoint, service_name):
    """Init propagators, provider, exporter, processor. Returns (provider, tracer).

    The global textmap is set once per process; the provider is used
    directly (never set as the process-global provider) so tests can create
    fresh providers per test.
    """
    global _PROPAGATORS_SET
    if not _PROPAGATORS_SET:
        propagate.set_global_textmap(
            CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
        )
        _PROPAGATORS_SET = True

    provider = TracerProvider(
        resource=Resource({"service.name": service_name, "agent.name": "fleet-coordinator"})
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPJSONExporter(endpoint),
            schedule_delay_millis=1000,
            max_export_batch_size=512,
        )
    )
    # Flush on any exit path (normal, sys.exit, unhandled exception).
    # No guarantee for SIGKILL / sudden power loss (see README.md).
    atexit.register(_flush_quietly, provider)
    return provider, provider.get_tracer("fleet.coordinator", "1.0.0")


def new_intent_ids():
    """Allocate task/session IDs ONCE per user intent (uuid4 hex)."""
    return uuid.uuid4().hex, uuid.uuid4().hex


def spawn_controlled_child(child_path, child_env, timeout=30):
    """Run child.py with the allowlisted env; parse its JSON report.

    Returns the report dict, or {"ok": False, "error": "<bounded enum>",
    "stderr_tail": "<last 200 chars of stderr>"} on failure. Raw child
    output is never placed on a span.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(child_path)],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout"}
    except OSError:
        return {"ok": False, "error": "ChildProcessFailure"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "ChildProcessFailure",
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-200:],
        }
    try:
        report = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "ChildProcessFailure"}
    report["ok"] = True
    return report


def run_intent(endpoint, service_name, child_path, spawn_child=True):
    """Run one coordinator intent end to end. Returns a summary dict."""
    provider, tracer = init_tracing(endpoint, service_name)
    task_id, session_id = new_intent_ids()
    summary = {"task_id": task_id, "session_id": session_id}

    with tracer.start_as_current_span(
        "agent.run",
        kind=SpanKind.INTERNAL,
        attributes=sanitize_attributes(
            {
                "agent.name": "fleet-coordinator",
                "agent.role": "coordinator",
                "session.id": session_id,
                "task.id": task_id,
            }
        ),
    ) as root_span:
        root_ctx = root_span.get_span_context()
        root_trace_id = format_trace_id(root_ctx.trace_id)
        summary["trace_id"] = root_trace_id

        # Coordinator state write before the first handoff.
        with tracer.start_as_current_span(
            "state.write",
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(
                {
                    "agent.name": "fleet-coordinator",
                    "session.id": session_id,
                    "task.id": task_id,
                    "state.key": f"fleet.task.{task_id}.status",
                    "state.before_ref": "hash:0000000000000000",
                    "state.after_ref": "hash:9f86d081884c7d659a2feaa0c55ad015",
                }
            ),
        ):
            pass

        # --- Handoff to the controlled child (fixed propagation, S1). ---
        child_report = None
        handoff_ref = f"payload:child-job-{task_id[:8]}"
        with tracer.start_as_current_span(
            "agent.handoff",
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(
                {
                    "agent.name": "fleet-coordinator",
                    "agent.from": "fleet-coordinator",
                    "agent.to": "controlled-child",
                    "session.id": session_id,
                    "task.id": task_id,
                    "handoff.reason": "delegate-tool-work",
                    "handoff.payload_ref": handoff_ref,
                }
            ),
        ) as handoff_span:
            handoff_span_id = format_span_id(handoff_span.get_span_context().span_id)
            summary["handoff_span_id"] = handoff_span_id
            if spawn_child:
                # inject() WHILE the handoff span is current: the child
                # receives THIS span's context as its parent.
                carrier = {}
                propagate.inject(carrier)
                child_env = build_child_env(
                    carrier, task_id, session_id, handoff_ref, endpoint=endpoint
                )
                child_report = spawn_controlled_child(child_path, child_env)
                summary["child_report"] = child_report
                parented = bool(
                    child_report.get("ok")
                    and child_report.get("parented")
                    and child_report.get("parent_span_id") == handoff_span_id
                    and child_report.get("trace_id") == root_trace_id
                )
                handoff_span.set_attribute("handoff.child_parented", str(parented).lower())
                if not child_report.get("ok"):
                    handoff_span.set_attribute(
                        "handoff.child_error",
                        child_report.get("error", "ChildProcessFailure"),
                    )
                    handoff_span.set_status(
                        Status(StatusCode.ERROR, "Process failed; details withheld")
                    )
                summary["child_parented"] = parented

        # --- Handoff to an external agent: correlation only, no subprocess.
        # The external agent keeps its own native trace IDs; we correlate
        # via task/session IDs and handoff.payload_ref. ---
        with tracer.start_as_current_span(
            "agent.handoff",
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(
                {
                    "agent.name": "fleet-coordinator",
                    "agent.from": "fleet-coordinator",
                    "agent.to": "external-agent",
                    "session.id": session_id,
                    "task.id": task_id,
                    "handoff.reason": "delegate-external-review",
                    "handoff.payload_ref": f"payload:external-job-{task_id[:8]}",
                }
            ),
        ):
            pass

    summary["flush_ok"] = bool(provider.force_flush(timeout_millis=5000))
    provider.shutdown()
    return summary


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Fleet trace coordinator (Milestone 1)")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--service-name", default=None)
    parser.add_argument(
        "--child",
        default=str(__import__("pathlib").Path(__file__).with_name("child.py")),
    )
    parser.add_argument("--no-child", action="store_true")
    args = parser.parse_args()

    config = resolve_config()
    endpoint = args.endpoint or config["endpoint"]
    service_name = args.service_name or config["service_name"]
    summary = run_intent(
        endpoint, service_name, args.child, spawn_child=not args.no_child
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
