"""Instrumented controlled child for the propagation test.

Reads W3C trace context from the environment (TRACEPARENT / TRACESTATE /
BAGGAGE), extracts it with composite W3C propagators, and reports whether
it is genuinely parented. Injection alone proves nothing: the child must
report child.parentSpanId == handoff.spanId and the same traceId, or it
is NOT parented.

The child prints exactly one JSON object to stdout (the report) and logs
everything else to stderr, so the coordinator can parse stdout safely.

Exit codes: 0 = ok, 3 = abrupt-exit test path (FLEET_CHILD_ABRUPT_EXIT=1),
2 = usage/config error.

Environment (all passed in by the coordinator's allowlisted env):
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT  OTLP/HTTP JSON endpoint (optional;
                                      without it spans are dropped but IDs
                                      are still generated and reported)
  OTEL_SERVICE_NAME                   default "fleet-child"
  FLEET_TASK_ID / FLEET_SESSION_ID    correlation IDs (opaque)
  FLEET_HANDOFF_REF                   opaque handoff reference
  TRACEPARENT / TRACESTATE / BAGGAGE  W3C context from the coordinator
  FLEET_CHILD_ABRUPT_EXIT=1           exit abruptly after creating spans
                                      (early-termination test; spans are
                                      exported synchronously on span end)
"""

from __future__ import annotations

import json
import os
import sys

from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from otlp_json import NullExporter, OTLPJSONExporter


def _log(*args):
    print(*args, file=sys.stderr, flush=True)


def main():
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    service_name = os.environ.get("OTEL_SERVICE_NAME", "fleet-child")
    task_id = os.environ.get("FLEET_TASK_ID", "")
    session_id = os.environ.get("FLEET_SESSION_ID", "")
    handoff_ref = os.environ.get("FLEET_HANDOFF_REF", "")

    # --- Child extraction (S1 fix): extract, then validate. ---
    # W3C header names are lowercase ("traceparent"); env vars are
    # uppercase ("TRACEPARENT"). The propagator's getter is case-sensitive,
    # so the carrier must use lowercase keys.
    carrier = {
        key.lower(): os.environ[key]
        for key in ("TRACEPARENT", "TRACESTATE", "BAGGAGE")
        if key in os.environ
    }
    ctx = propagate.extract(carrier)
    parent_ctx = trace.get_current_span(ctx).get_span_context()
    parented = bool(parent_ctx.is_valid and parent_ctx.is_remote)
    parent_span_id = format_span_id(parent_ctx.span_id) if parented else None
    parent_trace_id = format_trace_id(parent_ctx.trace_id) if parent_ctx.is_valid else None
    _log(f"child: extracted parented={parented} parent_span_id={parent_span_id}")

    exporter = OTLPJSONExporter(endpoint) if endpoint else NullExporter()
    provider = TracerProvider(
        resource=Resource(
            {"service.name": service_name, "agent.name": "controlled-child"}
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("fleet.child", "1.0.0")

    base_attrs = {
        "agent.name": "controlled-child",
        "agent.role": "child",
        "session.id": session_id,
        "task.id": task_id,
    }
    # A child that is NOT parented runs as a new root and correlates only
    # via task/session IDs — it must never claim the coordinator's parent.
    with tracer.start_as_current_span(
        "agent.run",
        context=ctx if parented else None,
        kind=SpanKind.INTERNAL,
        attributes=dict(base_attrs),
    ) as run_span:
        run_ctx = run_span.get_span_context()
        trace_id = format_trace_id(run_ctx.trace_id)
        span_id = format_span_id(run_ctx.span_id)
        with tracer.start_as_current_span(
            "llm.call",
            kind=SpanKind.INTERNAL,
            attributes={
                **base_attrs,
                "gen_ai.provider.name": "controlled-child",
                "gen_ai.request.model": "child-model",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 40,
            },
        ):
            with tracer.start_as_current_span(
                "tool.exec",
                kind=SpanKind.INTERNAL,
                attributes={**base_attrs, "gen_ai.tool.name": "Bash"},
            ):
                pass  # simulated tool work; no real command executed

    provider.force_flush()
    provider.shutdown()

    report = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "parented": parented,
        "parent_trace_id": parent_trace_id,
        "task_id": task_id,
        "session_id": session_id,
        "handoff_ref": handoff_ref,
        "env_keys": sorted(os.environ.keys()),
    }
    print(json.dumps(report), flush=True)

    if os.environ.get("FLEET_CHILD_ABRUPT_EXIT") == "1":
        # Early-termination path: spans were already exported synchronously
        # on span end (SimpleSpanProcessor). Exit abruptly without any
        # further shutdown work.
        sys.exit(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
