"""Milestone 1 tests (unittest).

Covers the plan's acceptance criteria:
- one intent, multiple handoffs -> one coordinator root trace_id
- controlled child propagates (real subprocess) -> trace_id + parent match
- external handoff -> separate native IDs, correlation via ledger IDs
- every handoff has nonempty from/to/reason/payload_ref
- error handling -> ERROR status + safe error metadata
- secrets/PII injection -> nothing secret in received serialized OTLP
- env allowlist -> fake secret in parent env never reaches the child
- missing/invalid TRACEPARENT -> child runs unparented, correlated only
  by task/session IDs
- graceful flush -> spans arrive before exit; force_flush succeeds
- early termination -> spans still delivered (flush on exit path)
- deterministic 12-span fixture arrives intact, reproducible

Run from observability/:
    ../.venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixture import TRACE_ID as FIXTURE_TRACE_ID
from fixture import export_fixture, fixture_spec
from fleet_trace import (
    ALLOWED_CHILD_ENV,
    TRACE_ENV_VARS,
    build_child_env,
    run_intent,
    sanitize_attributes,
    spawn_controlled_child,
)
from receiver import LocalReceiver

HERE = Path(__file__).resolve().parent.parent
CHILD_PATH = HERE / "child.py"

FAKE_SECRET = "FAKE-SECRET-123"
FAKE_TOKEN = "tok-fake-456"

EXPECTED_NAMES = [
    "agent.run",
    "agent.run",
    "agent.handoff",
    "agent.handoff",
    "subagent.spawn",
    "llm.call",
    "tool.exec",
    "tool.exec",
    "retrieval.search",
    "state.write",
    "state.write",
    "approval.wait",
]


def _emit_span(endpoint, service_name, name, attributes, status_error=False):
    """Emit one span via a fresh provider. Returns (trace_id, span_id)."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import SpanKind, Status, StatusCode, format_span_id, format_trace_id

    from otlp_json import OTLPJSONExporter

    provider = TracerProvider(resource=Resource({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPJSONExporter(endpoint)))
    tracer = provider.get_tracer("fleet.test", "1.0.0")
    with tracer.start_as_current_span(
        name, kind=SpanKind.INTERNAL, attributes=sanitize_attributes(attributes)
    ) as span:
        ctx = span.get_span_context()
        trace_id, span_id = format_trace_id(ctx.trace_id), format_span_id(ctx.span_id)
        if status_error:
            span.set_status(Status(StatusCode.ERROR, "Tool failed; details withheld"))
    provider.force_flush()
    provider.shutdown()
    return trace_id, span_id


class FleetTraceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-test-")
        self.recv = LocalReceiver(out_path=str(Path(self.tmp) / "received.jsonl")).start()
        self.endpoint = self.recv.endpoint
        self._saved_env = dict(os.environ)

    def tearDown(self):
        self.recv.stop()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _spans(self):
        return list(self.recv.spans)

    def _spans_named(self, name):
        return [s for s in self._spans() if s["name"] == name]

    # -- one intent, multiple handoffs -------------------------------------
    def test_one_intent_multiple_handoffs(self):
        summary = run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        spans = self._spans()
        self.assertGreater(len(spans), 0)
        trace_ids = {s["traceId"] for s in spans}
        self.assertEqual(trace_ids, {summary["trace_id"]})
        task_ids = {s["attributes"].get("task.id") for s in spans}
        session_ids = {s["attributes"].get("session.id") for s in spans}
        self.assertEqual(task_ids, {summary["task_id"]})
        self.assertEqual(session_ids, {summary["session_id"]})
        roots = [s for s in spans if s["name"] == "agent.run" and not s.get("parentSpanId")]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(self._spans_named("agent.handoff")), 2)

    # -- controlled child propagates (real subprocess) ----------------------
    def test_controlled_child_propagates(self):
        summary = run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        report = summary.get("child_report") or {}
        self.assertTrue(report.get("ok"), f"child failed: {report}")
        self.assertTrue(report.get("parented"))
        self.assertEqual(report.get("trace_id"), summary["trace_id"])
        self.assertEqual(report.get("parent_span_id"), summary["handoff_span_id"])
        child_spans = [
            s
            for s in self._spans()
            if s["attributes"].get("agent.name") == "controlled-child"
        ]
        self.assertGreaterEqual(len(child_spans), 3)  # agent.run + llm.call + tool.exec
        self.assertTrue(all(s["traceId"] == summary["trace_id"] for s in child_spans))

    # -- external handoff: separate native IDs, correlation only ------------
    def test_external_handoff(self):
        summary = run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        handoffs = self._spans_named("agent.handoff")
        external = [
            h for h in handoffs if h["attributes"].get("agent.to") == "external-agent"
        ]
        self.assertEqual(len(external), 1)
        payload_ref = external[0]["attributes"]["handoff.payload_ref"]
        # Simulate the external agent doing its own work in its own trace,
        # correlated only by the ledger IDs and payload ref.
        ext_trace_id, _ = _emit_span(
            self.endpoint,
            "external-agent",
            "agent.run",
            {
                "agent.name": "external-agent",
                "session.id": summary["session_id"],
                "task.id": summary["task_id"],
                "handoff.payload_ref": payload_ref,
            },
        )
        self.assertNotEqual(ext_trace_id, summary["trace_id"])
        ext_spans = [s for s in self._spans() if s["traceId"] == ext_trace_id]
        self.assertEqual(len(ext_spans), 1)
        self.assertEqual(ext_spans[0]["attributes"]["task.id"], summary["task_id"])
        self.assertEqual(
            ext_spans[0]["attributes"]["handoff.payload_ref"], payload_ref
        )

    # -- every handoff has nonempty from/to/reason/payload_ref --------------
    def test_handoff_fields_nonempty(self):
        run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        for span in self._spans_named("agent.handoff"):
            attrs = span["attributes"]
            for field in ("agent.from", "agent.to", "handoff.reason", "handoff.payload_ref"):
                self.assertTrue(attrs.get(field), f"{field} empty on {span}")

    # -- error handling ------------------------------------------------------
    def test_error_handling(self):
        _emit_span(
            self.endpoint,
            "fleet-test",
            "llm.call",
            {
                "agent.name": "fleet-coordinator",
                "error.type": "LLMError",
                "error.message": "LLM call failed; details withheld",
            },
            status_error=True,
        )
        _emit_span(
            self.endpoint,
            "fleet-test",
            "tool.exec",
            {
                "agent.name": "fleet-coordinator",
                "gen_ai.tool.name": "Bash",
                "error.type": "ToolError",
                "error.message": "Tool failed; details withheld",
            },
            status_error=True,
        )
        _emit_span(
            self.endpoint,
            "fleet-test",
            "tool.exec",
            {"agent.name": "fleet-coordinator", "gen_ai.tool.name": "Bash"},
        )
        failed = [s for s in self._spans() if s["status"]["code"] == 2]
        self.assertEqual(len(failed), 2)
        for span in failed:
            self.assertIn(span["attributes"]["error.type"], ("ToolError", "LLMError"))
            self.assertIn(
                span["attributes"]["error.message"],
                (
                    "Tool failed; details withheld",
                    "LLM call failed; details withheld",
                ),
            )
        blob = json.dumps(self.recv.payloads)
        self.assertNotIn("Traceback", blob)
        ok_tools = [
            s
            for s in self._spans_named("tool.exec")
            if s["status"]["code"] != 2
        ]
        self.assertEqual(len(ok_tools), 1)

    # -- no secrets / PII in received serialized OTLP ------------------------
    def test_no_secrets_in_otlp(self):
        poisoned = {
            "api_key": FAKE_SECRET,
            "email": "someone@example.com",
            "prompt": "raw prompt text with secrets",
            "file_content": "full file contents here",
            "password": "hunter2",
            "agent.name": "fleet-coordinator",
        }
        clean = sanitize_attributes(poisoned)
        self.assertEqual(clean, {"agent.name": "fleet-coordinator"})
        os.environ["FAKE_API_KEY"] = FAKE_SECRET
        os.environ["MY_TOKEN"] = FAKE_TOKEN
        run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        blob = json.dumps(self.recv.payloads)
        for banned in (FAKE_SECRET, FAKE_TOKEN, "FAKE_API_KEY", "someone@example.com"):
            self.assertNotIn(banned, blob)

    # -- env allowlist -------------------------------------------------------
    def test_env_allowlist(self):
        os.environ["FAKE_API_KEY"] = FAKE_SECRET
        os.environ["MY_TOKEN"] = FAKE_TOKEN
        summary = run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        report = summary.get("child_report") or {}
        self.assertTrue(report.get("ok"), f"child failed: {report}")
        env_keys = set(report.get("env_keys") or [])
        self.assertNotIn("FAKE_API_KEY", env_keys)
        self.assertNotIn("MY_TOKEN", env_keys)
        allowed = (
            set(ALLOWED_CHILD_ENV)
            | set(TRACE_ENV_VARS)
            | {"FLEET_TASK_ID", "FLEET_SESSION_ID", "FLEET_HANDOFF_REF"}
        )
        # CPython's locale coercion (PEP 538) sets LC_CTYPE inside the child
        # interpreter itself; the coordinator never passes it. It is a
        # locale setting, not a secret carrier, so it is excluded here.
        self.assertTrue(
            env_keys - {"LC_CTYPE"} <= allowed,
            f"child saw non-allowlisted vars: {sorted(env_keys - allowed - {'LC_CTYPE'})}",
        )
        self.assertIn("TRACEPARENT", env_keys)
        self.assertIn("FLEET_TASK_ID", env_keys)

    # -- missing / invalid TRACEPARENT -> unparented -------------------------
    def test_missing_invalid_traceparent(self):
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": self.endpoint,
            "FLEET_TASK_ID": "task-unparented-1",
            "FLEET_SESSION_ID": "session-unparented-1",
            "FLEET_HANDOFF_REF": "payload:unparented-1",
        }
        # Case A: no TRACEPARENT at all.
        report_a = spawn_controlled_child(CHILD_PATH, dict(base_env))
        self.assertTrue(report_a.get("ok"), f"child failed: {report_a}")
        self.assertFalse(report_a.get("parented"))
        self.assertIsNone(report_a.get("parent_span_id"))
        # Case B: malformed TRACEPARENT.
        bad_env = dict(base_env, TRACEPARENT="00-garbage-garbage-01")
        report_b = spawn_controlled_child(CHILD_PATH, bad_env)
        self.assertTrue(report_b.get("ok"), f"child failed: {report_b}")
        self.assertFalse(report_b.get("parented"))
        self.assertIsNone(report_b.get("parent_span_id"))
        # Both children ran as new roots, correlated only by task/session IDs.
        self.assertNotEqual(report_a["trace_id"], report_b["trace_id"])
        child_runs = [
            s
            for s in self._spans()
            if s["name"] == "agent.run"
            and s["attributes"].get("agent.name") == "controlled-child"
        ]
        self.assertEqual(len(child_runs), 2)
        self.assertTrue(all(s.get("parentSpanId") is None for s in child_runs))
        self.assertEqual(
            {s["attributes"]["task.id"] for s in child_runs},
            {"task-unparented-1"},
        )

    # -- graceful flush -------------------------------------------------------
    def test_graceful_flush(self):
        summary = run_intent(self.endpoint, "fleet-test-coordinator", CHILD_PATH)
        self.assertTrue(summary.get("flush_ok"))
        spans = self._spans()
        self.assertGreater(len(spans), 0)
        self.assertTrue(all(s["traceId"] == summary["trace_id"] for s in spans))

    # -- early termination ----------------------------------------------------
    def test_early_termination(self):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": self.endpoint,
            "FLEET_TASK_ID": "task-abrupt-1",
            "FLEET_SESSION_ID": "session-abrupt-1",
            "FLEET_HANDOFF_REF": "payload:abrupt-1",
            "FLEET_CHILD_ABRUPT_EXIT": "1",
        }
        proc = subprocess.run(
            [sys.executable, str(CHILD_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 3)
        child_runs = [
            s
            for s in self._spans()
            if s["name"] == "agent.run"
            and s["attributes"].get("agent.name") == "controlled-child"
            and s["attributes"].get("task.id") == "task-abrupt-1"
        ]
        self.assertEqual(len(child_runs), 1)
        child_tools = [
            s
            for s in self._spans()
            if s["name"] == "tool.exec"
            and s["attributes"].get("task.id") == "task-abrupt-1"
        ]
        self.assertEqual(len(child_tools), 1)

    # -- deterministic 12-span fixture ---------------------------------------
    def test_fixture_12_spans(self):
        before = len(self._spans())
        spec = export_fixture(self.endpoint)
        self.assertEqual(len(spec), 12)
        batch = self._spans()[before:]
        self.assertEqual(len(batch), 12)
        self.assertEqual(sorted(s["name"] for s in batch), sorted(EXPECTED_NAMES))
        self.assertEqual({s["traceId"] for s in batch}, {FIXTURE_TRACE_ID})
        by_id = {s["spanId"]: s for s in batch}
        for expected in fixture_spec():
            got = by_id.get(expected["span_id"])
            self.assertIsNotNone(got, f"missing span {expected['span_id']}")
            self.assertEqual(got["name"], expected["name"])
            self.assertEqual(got.get("parentSpanId"), expected["parent_span_id"])
            for key, value in expected["attributes"].items():
                self.assertEqual(got["attributes"].get(key), value)
        failed_tools = [
            s
            for s in batch
            if s["name"] == "tool.exec" and s["status"]["code"] == 2
        ]
        self.assertEqual(len(failed_tools), 1)
        self.assertEqual(failed_tools[0]["attributes"]["error.type"], "ToolError")
        self.assertEqual(
            failed_tools[0]["attributes"]["error.message"],
            "Tool failed; details withheld",
        )
        state_spans = [s for s in batch if s["name"] == "state.write"]
        self.assertEqual(len(state_spans), 2)
        for span in state_spans:
            self.assertTrue(span["attributes"].get("state.key"))
            self.assertTrue(span["attributes"].get("state.before_ref"))
            self.assertTrue(span["attributes"].get("state.after_ref"))
        approval = [s for s in batch if s["name"] == "approval.wait"]
        self.assertEqual(len(approval), 1)
        self.assertEqual(approval[0]["attributes"].get("synthetic"), "true")
        # Determinism: a second export produces identical span IDs.
        before2 = len(self._spans())
        export_fixture(self.endpoint)
        batch2 = self._spans()[before2:]
        self.assertEqual(len(batch2), 12)
        self.assertEqual(
            sorted(s["spanId"] for s in batch2), sorted(s["spanId"] for s in batch)
        )
        self.assertEqual(
            sorted(s["traceId"] for s in batch2), sorted(s["traceId"] for s in batch)
        )


if __name__ == "__main__":
    unittest.main()
