"""Deterministic 12-span fixture (Milestone 1).

Exactly 12 spans, all parents explicit, one nonzero 32-char hex trace ID
for the coordinator intent. The second agent.run is the controlled
instrumented child's run (NOT another coordinator intent): it inherits
the trace ID with the actual handoff span as parent.

Span list (creation order == span-ID order, reproducible every run):
  1  agent.run        root (coordinator)
  2  state.write      parent 1
  3  agent.handoff    parent 1  (coordinator -> controlled child)
  4  agent.run        parent 3  (controlled child)
  5  llm.call         parent 4
  6  tool.exec        parent 5  (ok)
  7  tool.exec        parent 5  (FAILED: error.type=ToolError, ERROR)
  8  subagent.spawn   parent 4
  9  retrieval.search parent 4
 10  state.write      parent 4
 11  agent.handoff    parent 1  (coordinator -> external agent)
 12  approval.wait    parent 1  (SYNTHETIC — real approval events are
                                 tested separately)

state.write spans carry state.key + state.before_ref/state.after_ref
(opaque refs only — content hash / record id — never raw content).
approval.wait is labeled synthetic.

Determinism: a fixed trace ID, fixed span IDs (via a deterministic
IdGenerator), and fixed start/end times. Exporting twice produces
identical span IDs, names, parents, and attributes.

Usage:
    python fixture.py --endpoint http://127.0.0.1:<port>/v1/traces [--dump out/fixture.json]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)

from fleet_trace import sanitize_attributes
from otlp_json import OTLPJSONExporter

TRACE_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TASK_ID = "task-fixture-0001"
SESSION_ID = "session-fixture-0001"

# Fixed base time: 2026-11-05T10:00:00Z in nanoseconds.
BASE_NS = 1793872800000000000
MS = 1_000_000

SPAN_IDS = [
    "1111111111111111",
    "2222222222222222",
    "3333333333333333",
    "4444444444444444",
    "5555555555555555",
    "6666666666666666",
    "7777777777777777",
    "8888888888888888",
    "9999999999999999",
    "aaaaaaaaaaaaaaaa",
    "bbbbbbbbbbbbbbbb",
    "cccccccccccccccc",
]

_COMMON = {"session.id": SESSION_ID, "task.id": TASK_ID}

SPECS = [
    {
        "name": "agent.run",
        "span_id": SPAN_IDS[0],
        "parent_span_id": None,
        "start_ns": BASE_NS,
        "duration_ns": 5_000 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "fleet-coordinator",
            "agent.role": "coordinator",
        },
    },
    {
        "name": "state.write",
        "span_id": SPAN_IDS[1],
        "parent_span_id": SPAN_IDS[0],
        "start_ns": BASE_NS + 100 * MS,
        "duration_ns": 200 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "fleet-coordinator",
            "state.key": f"fleet.task.{TASK_ID}.status",
            "state.before_ref": "hash:0000000000000000",
            "state.after_ref": "hash:9f86d081884c7d659a2feaa0c55ad015",
        },
    },
    {
        "name": "agent.handoff",
        "span_id": SPAN_IDS[2],
        "parent_span_id": SPAN_IDS[0],
        "start_ns": BASE_NS + 500 * MS,
        "duration_ns": 2_000 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "fleet-coordinator",
            "agent.from": "fleet-coordinator",
            "agent.to": "controlled-child",
            "handoff.reason": "delegate-tool-work",
            "handoff.payload_ref": "payload:child-job-0001",
        },
    },
    {
        "name": "agent.run",
        "span_id": SPAN_IDS[3],
        "parent_span_id": SPAN_IDS[2],
        "start_ns": BASE_NS + 600 * MS,
        "duration_ns": 1_500 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "agent.role": "child",
        },
    },
    {
        "name": "llm.call",
        "span_id": SPAN_IDS[4],
        "parent_span_id": SPAN_IDS[3],
        "start_ns": BASE_NS + 700 * MS,
        "duration_ns": 1_000 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "gen_ai.provider.name": "fixture",
            "gen_ai.request.model": "fixture-model",
            "gen_ai.usage.input_tokens": 1200,
            "gen_ai.usage.output_tokens": 180,
        },
    },
    {
        "name": "tool.exec",
        "span_id": SPAN_IDS[5],
        "parent_span_id": SPAN_IDS[4],
        "start_ns": BASE_NS + 800 * MS,
        "duration_ns": 300 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "gen_ai.tool.name": "Bash",
        },
    },
    {
        "name": "tool.exec",
        "span_id": SPAN_IDS[6],
        "parent_span_id": SPAN_IDS[4],
        "start_ns": BASE_NS + 1_200 * MS,
        "duration_ns": 400 * MS,
        "status": "error",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "gen_ai.tool.name": "Bash",
            "error.type": "ToolError",
            "error.message": "Tool failed; details withheld",
        },
    },
    {
        "name": "subagent.spawn",
        "span_id": SPAN_IDS[7],
        "parent_span_id": SPAN_IDS[3],
        "start_ns": BASE_NS + 1_700 * MS,
        "duration_ns": 200 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "agent.from": "controlled-child",
            "agent.to": "subagent-worker",
        },
    },
    {
        "name": "retrieval.search",
        "span_id": SPAN_IDS[8],
        "parent_span_id": SPAN_IDS[3],
        "start_ns": BASE_NS + 1_800 * MS,
        "duration_ns": 200 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "retrieval.query_ref": "query:fixture-search-0001",
        },
    },
    {
        "name": "state.write",
        "span_id": SPAN_IDS[9],
        "parent_span_id": SPAN_IDS[3],
        "start_ns": BASE_NS + 2_000 * MS,
        "duration_ns": 100 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "controlled-child",
            "state.key": f"fleet.task.{TASK_ID}.child-status",
            "state.before_ref": "hash:9f86d081884c7d659a2feaa0c55ad015",
            "state.after_ref": "hash:5d41402abc4b2a76b9719d911017c592",
        },
    },
    {
        "name": "agent.handoff",
        "span_id": SPAN_IDS[10],
        "parent_span_id": SPAN_IDS[0],
        "start_ns": BASE_NS + 2_500 * MS,
        "duration_ns": 500 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "fleet-coordinator",
            "agent.from": "fleet-coordinator",
            "agent.to": "external-agent",
            "handoff.reason": "delegate-external-review",
            "handoff.payload_ref": "payload:external-job-0001",
        },
    },
    {
        "name": "approval.wait",
        "span_id": SPAN_IDS[11],
        "parent_span_id": SPAN_IDS[0],
        "start_ns": BASE_NS + 3_000 * MS,
        "duration_ns": 500 * MS,
        "status": "ok",
        "attributes": {
            **_COMMON,
            "agent.name": "fleet-coordinator",
            "approval.kind": "synthetic",
            "synthetic": "true",
        },
    },
]


class DeterministicIdGenerator(IdGenerator):
    """Returns the fixed trace ID and the fixture span IDs in order."""

    def __init__(self, trace_id, span_ids):
        self._trace_id = int(trace_id, 16)
        self._span_ids = [int(s, 16) for s in span_ids]
        self._index = 0

    def generate_span_id(self):
        value = self._span_ids[self._index % len(self._span_ids)]
        self._index += 1
        return value

    def generate_trace_id(self):
        return self._trace_id


def fixture_spec():
    """The 12 span specs (name, span_id, parent_span_id, attributes, status)."""
    return [
        {
            "name": spec["name"],
            "span_id": spec["span_id"],
            "parent_span_id": spec["parent_span_id"],
            "attributes": dict(spec["attributes"]),
            "status": spec["status"],
        }
        for spec in SPECS
    ]


def export_fixture(endpoint, service_name="fleet-fixture"):
    """Build the 12 spans with the SDK and export them to `endpoint`.

    Returns the fixture spec list. Raises on export failure.
    """
    provider = TracerProvider(
        resource=Resource({"service.name": service_name, "agent.name": "fleet-fixture"}),
        id_generator=DeterministicIdGenerator(TRACE_ID, SPAN_IDS),
    )
    provider.add_span_processor(SimpleSpanProcessor(OTLPJSONExporter(endpoint)))
    tracer = provider.get_tracer("fleet.fixture", "1.0.0")

    for spec in SPECS:
        parent_context = None
        if spec["parent_span_id"]:
            # The parent SpanContext must carry the SAMPLED flag: the
            # SDK's default ParentBased sampler drops children of an
            # unsampled parent (they become NonRecordingSpans and are
            # never exported).
            parent_context = set_span_in_context(
                NonRecordingSpan(
                    SpanContext(
                        trace_id=int(TRACE_ID, 16),
                        span_id=int(spec["parent_span_id"], 16),
                        is_remote=False,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    )
                )
            )
        span = tracer.start_span(
            spec["name"],
            context=parent_context,
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(spec["attributes"]),
            start_time=spec["start_ns"],
        )
        if spec["status"] == "error":
            span.set_status(
                Status(StatusCode.ERROR, "Tool failed; details withheld")
            )
        span.end(end_time=spec["start_ns"] + spec["duration_ns"])

    provider.force_flush()
    provider.shutdown()
    return fixture_spec()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Export the deterministic 12-span fixture")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--service-name", default="fleet-fixture")
    parser.add_argument("--dump", default=None)
    args = parser.parse_args()

    endpoint = args.endpoint or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://127.0.0.1:4318/v1/traces",
    )
    spec = export_fixture(endpoint, args.service_name)
    if args.dump:
        Path(args.dump).parent.mkdir(parents=True, exist_ok=True)
        Path(args.dump).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "trace_id": TRACE_ID,
                "span_count": len(spec),
                "spans": [
                    {
                        "name": s["name"],
                        "span_id": s["span_id"],
                        "parent_span_id": s["parent_span_id"],
                    }
                    for s in spec
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
