"""Milestone 2 tests (unittest) — Claude Code controlled child trace.

Proves the harness with a stub `claude` stand-in (a small script on
PATH that prints its env and posts one OTLP/HTTP JSON span to the local
receiver). The live test against the real `claude` binary SKIPS cleanly
when no real binary is installed; it runs only on a machine with Claude
Code installed and signed in (the dev machine).

Run from observability/:
    ../.venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_child import (
    CLAUDE_TELEMETRY_FLAGS,
    build_claude_child_env,
    claude_available,
    detect_parenting,
    launch_claude,
    run_claude_intent,
)
from receiver import LocalReceiver

HERE = Path(__file__).resolve().parent.parent

FAKE_SECRET = "FAKE-SECRET-789"
FAKE_TOKEN = "tok-fake-012"

# Fake `claude` CLI for tests. NOT real Claude Code.
# - `claude --version` prints a stub version string.
# - Otherwise reads TRACEPARENT, posts one OTLP/HTTP JSON span to the
#   receiver endpoint, dumps its environment to OUT_FILE (baked in by
#   the test when the file is written), and exits 0.
# - STUB_FAIL=1 in env -> exit 2 without posting (error path).
STUB_CLAUDE = '''#!/usr/bin/env python3
"""Fake `claude` CLI for Milestone 2 tests. NOT real Claude Code."""
import json
import os
import sys
import time
import urllib.request
import uuid

OUT_FILE = "__OUT_FILE__"


def main():
    if "--version" in sys.argv:
        print("claude-code-stub 0.0.0-test")
        return 0
    if os.environ.get("STUB_FAIL") == "1":
        print("stub: simulated failure", file=sys.stderr)
        return 2
    env = dict(os.environ)
    with open(OUT_FILE, "w") as fh:
        json.dump(env, fh, sort_keys=True)
    endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    traceparent = env.get("TRACEPARENT", "")
    parented = False
    trace_id = uuid.uuid4().hex
    parent_span_id = None
    parts = traceparent.split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        try:
            int(parts[1], 16)
            int(parts[2], 16)
            trace_id, parent_span_id = parts[1], parts[2]
            parented = True
        except ValueError:
            pass
    span_id = uuid.uuid4().hex[:16]
    now = time.time_ns()
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "claude_code.interaction",
        "kind": 1,
        "startTimeUnixNano": str(now),
        "endTimeUnixNano": str(now + 1000000),
        "attributes": [
            {"key": "agent.name", "value": {"stringValue": "claude-code"}},
            {"key": "task.id", "value": {"stringValue": env.get("FLEET_TASK_ID", "")}},
            {"key": "session.id", "value": {"stringValue": env.get("FLEET_SESSION_ID", "")}},
        ],
        "status": {},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                "scopeSpans": [{"scope": {"name": "claude-code"}, "spans": [span]}],
            }
        ]
    }
    if endpoint:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:  # noqa: BLE001 - stub reports, never crashes
            print(f"stub: post failed: {exc}", file=sys.stderr)
    print(json.dumps({"stub": True, "parented": parented, "trace_id": trace_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _real_claude_on_path():
    """True only if a NON-stub `claude` binary is on the real PATH."""
    path = shutil.which("claude")
    if not path:
        return False
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "stub" not in (proc.stdout or "").lower()


class ClaudeChildTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-m2-")
        self.tmpdir = Path(self.tmp)
        self.recv = LocalReceiver(out_path=str(self.tmpdir / "received.jsonl")).start()
        self.endpoint = self.recv.endpoint
        self._saved_env = dict(os.environ)
        # Stub `claude` on PATH (prepended; the real PATH is restored in
        # tearDown and in the live test).
        self.bindir = self.tmpdir / "bin"
        self.bindir.mkdir()
        self.env_file = self.tmpdir / "stub_env.json"
        stub = self.bindir / "claude"
        stub.write_text(STUB_CLAUDE.replace("__OUT_FILE__", str(self.env_file)))
        stub.chmod(0o755)
        os.environ["PATH"] = str(self.bindir) + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        self.recv.stop()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _claude_spans(self):
        return [
            s
            for s in self.recv.spans
            if (s.get("resource") or {}).get("service.name") == "claude-code"
        ]

    def _stub_env(self):
        return json.loads(self.env_file.read_text())

    # -- stub claude: full intent, parented end to end ----------------------
    def test_stub_claude_parented_end_to_end(self):
        result = run_claude_intent(
            "Reply with exactly: OK",
            receiver=self.recv,
            span_wait_timeout=5.0,
        )
        self.assertEqual(result["status"], "parented", f"result: {result}")
        self.assertTrue(result["claude_installed"])
        self.assertIn("stub", result["claude_version"])
        self.assertTrue(result["child_parented"])
        self.assertEqual(result["detection"]["classification"], "parented")
        self.assertEqual(result["child_trace_ids"], [result["trace_id"]])
        self.assertTrue(result["flush_ok"])
        spans = self._claude_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["traceId"], result["trace_id"])
        self.assertEqual(spans[0]["parentSpanId"], result["handoff_span_id"])
        self.assertEqual(spans[0]["name"], "claude_code.interaction")
        # The stub saw the injected context and the telemetry flags.
        env = self._stub_env()
        self.assertIn("TRACEPARENT", env)
        self.assertTrue(env["TRACEPARENT"].startswith("00-" + result["trace_id"] + "-"))
        for key, value in CLAUDE_TELEMETRY_FLAGS.items():
            self.assertEqual(env.get(key), value, f"flag {key} missing/wrong")
        self.assertEqual(env.get("OTEL_SERVICE_NAME"), "claude-code")
        self.assertEqual(env.get("FLEET_TASK_ID"), result["task_id"])

    # -- no secrets reach the claude child env -------------------------------
    def test_no_secrets_in_claude_child_env(self):
        os.environ["FAKE_API_KEY"] = FAKE_SECRET
        os.environ["MY_TOKEN"] = FAKE_TOKEN
        result = run_claude_intent(
            "Reply with exactly: OK",
            receiver=self.recv,
            span_wait_timeout=5.0,
        )
        self.assertEqual(result["status"], "parented", f"result: {result}")
        env = self._stub_env()
        self.assertNotIn("FAKE_API_KEY", env)
        self.assertNotIn("MY_TOKEN", env)
        self.assertNotIn(FAKE_SECRET, json.dumps(env))
        blob = json.dumps(self.recv.payloads)
        for banned in (FAKE_SECRET, FAKE_TOKEN, "FAKE_API_KEY"):
            self.assertNotIn(banned, blob)

    # -- missing / invalid TRACEPARENT -> unparented, external --------------
    def test_stub_claude_missing_traceparent_unparented(self):
        # Case A: no TRACEPARENT at all.
        env_a = build_claude_child_env(
            {}, "task-unparented-1", "session-unparented-1",
            "payload:unparented-1", self.endpoint,
        )
        launch_a = launch_claude("Reply with exactly: OK", env_a)
        self.assertTrue(launch_a["ok"], f"launch failed: {launch_a}")
        self.assertNotIn("TRACEPARENT", self._stub_env())
        # Case B: malformed TRACEPARENT.
        env_b = build_claude_child_env(
            {"traceparent": "00-garbage-garbage-01"},
            "task-unparented-2", "session-unparented-2",
            "payload:unparented-2", self.endpoint,
        )
        launch_b = launch_claude("Reply with exactly: OK", env_b)
        self.assertTrue(launch_b["ok"], f"launch failed: {launch_b}")
        spans = self._claude_spans()
        self.assertEqual(len(spans), 2)
        for span in spans:
            self.assertIsNone(span.get("parentSpanId"))
        detection = detect_parenting(
            self.recv.spans, "a" * 32, "b" * 16
        )
        self.assertEqual(detection["classification"], "external")
        self.assertEqual(len(detection["child_trace_ids"]), 2)
        # Both children ran as new roots, correlated only by FLEET_* IDs.
        self.assertEqual(
            {s["attributes"].get("task.id") for s in spans},
            {"task-unparented-1", "task-unparented-2"},
        )

    # -- non-zero exit -> error, safe generic metadata, no span leak ---------
    def test_stub_claude_nonzero_exit(self):
        env = build_claude_child_env(
            {}, "task-fail-1", "session-fail-1", "payload:fail-1", self.endpoint
        )
        env["STUB_FAIL"] = "1"
        launch = launch_claude("Reply with exactly: OK", env)
        self.assertFalse(launch["ok"])
        self.assertEqual(launch["returncode"], 2)
        self.assertEqual(launch["error"], "ChildProcessFailure")
        self.assertEqual(len(self._claude_spans()), 0)

    # -- detect_parenting classification (unit-level) -------------------------
    def test_detect_parenting_classification(self):
        def span(trace_id, parent=None, name="claude_code.interaction"):
            s = {
                "traceId": trace_id,
                "spanId": "1234567890abcdef",
                "name": name,
                "resource": {"service.name": "claude-code"},
                "scope": {"name": "claude-code"},
            }
            if parent:
                s["parentSpanId"] = parent
            return s

        self.assertEqual(
            detect_parenting([], "a" * 32, "b" * 16)["classification"],
            "unverified",
        )
        self.assertEqual(
            detect_parenting(
                [span("a" * 32, "b" * 16)], "a" * 32, "b" * 16
            )["classification"],
            "parented",
        )
        self.assertEqual(
            detect_parenting(
                [span("c" * 32, "d" * 16)], "a" * 32, "b" * 16
            )["classification"],
            "external",
        )
        self.assertEqual(
            detect_parenting(
                [span("a" * 32, "e" * 16)], "a" * 32, "b" * 16
            )["classification"],
            "parented_to_other",
        )
        # Coordinator spans are not claude spans and are ignored.
        self.assertEqual(
            detect_parenting(
                [
                    {
                        "traceId": "a" * 32,
                        "spanId": "f" * 16,
                        "parentSpanId": "b" * 16,
                        "name": "agent.handoff",
                        "resource": {"service.name": "fleet-claude-coordinator"},
                        "scope": {"name": "fleet.coordinator"},
                    }
                ],
                "a" * 32,
                "b" * 16,
            )["classification"],
            "unverified",
        )

    # -- receiver records rejected (non-JSON) posts ---------------------------
    def test_receiver_records_rejected_payloads(self):
        before = len(self.recv.spans)
        req = urllib.request.Request(
            self.endpoint,
            data=b"\x0a\x10\x0a\x0e\x0a\x0cprotobuf-ish-bytes",
            headers={"Content-Type": "application/x-protobuf"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected HTTP 400 for non-JSON payload")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        self.assertEqual(len(self.recv.spans), before)
        self.assertEqual(len(self.recv.rejected), 1)
        self.assertEqual(
            self.recv.rejected[0]["content_type"], "application/x-protobuf"
        )
        self.assertGreater(self.recv.rejected[0]["length"], 0)

    # -- LIVE: real claude binary (skips cleanly when absent) ----------------
    def test_live_claude_real_binary(self):
        # Restore the real PATH (no stub) for this check.
        os.environ["PATH"] = self._saved_env.get("PATH", "")
        if not _real_claude_on_path():
            self.skipTest("real claude CLI not installed")
        info = claude_available()
        self.assertTrue(info["installed"])
        self.assertIsNotNone(info["version"])
        result = run_claude_intent(
            "Reply with exactly: OK",
            receiver=self.recv,
            span_wait_timeout=10.0,
            timeout=180,
        )
        # The harness must report a structured outcome; it never claims
        # parenting it did not observe.
        self.assertIn(
            result["status"], ("parented", "external", "unverified", "error")
        )
        self.assertIsNotNone(result["claude_version"])
        if result["status"] == "parented":
            self.assertEqual(result["child_trace_ids"], [result["trace_id"]])
            spans = self._claude_spans()
            self.assertTrue(
                any(
                    s["parentSpanId"] == result["handoff_span_id"] for s in spans
                )
            )


if __name__ == "__main__":
    unittest.main()
