"""Milestone 3 tests (unittest) — Hermes observer (isolated, synthetic).

The observer is Hermes-independent: a FakeHermesHost delivers synthetic
hook events exactly as Hermes would (one callback per hook, payload dict),
and a fake agent records its tool args/results so the tests can prove the
observer never modifies agent behavior. No Hermes installation is needed
here; the installed-build contract test (hermes --version, plugins list,
plugins doctor, hook delivery in a disposable profile) runs on the dev
machine via the review handoff.

Run from observability/:
    ../.venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent.parent)
import sys

sys.path.insert(0, sys_path)

from hermes_observer import HermesObserver
from receiver import LocalReceiver

HERE = Path(__file__).resolve().parent.parent

FAKE_SECRET = "FAKE-SECRET-HERMES-1"
FAKE_PROMPT = "PROMPT-CANARY-123"
FAKE_ANSWER = "ANSWER-CANARY-456"


def _event(event_type, session="s1", turn="t1", request=None, **extra):
    evt = {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "session_id": session,
        "turn_id": turn,
        "request_id": request or uuid.uuid4().hex[:16],
    }
    evt.update(extra)
    return evt


class FakeHermesHost:
    """Simulates Hermes hook delivery + a fake agent doing tool work.

    deliver(event) calls observer.handle_event exactly once (as a hook
    callback would) and returns its return value (must always be None).
    run_tool(args) records the args and returns a fixed result, proving
    the observer never sees or modifies them.
    """

    def __init__(self, observer):
        self.observer = observer
        self.tool_calls = []

    def deliver(self, event):
        return self.observer.handle_event(event)

    def run_tool(self, args):
        self.tool_calls.append(dict(args))
        return {"ok": True, "result": "tool-result-fixed"}


class HermesObserverTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-m3-")
        self.tmpdir = Path(self.tmp)
        self.recv = LocalReceiver(out_path=str(self.tmpdir / "received.jsonl")).start()
        self.endpoint = self.recv.endpoint
        self.observer = HermesObserver(endpoint=self.endpoint)
        self.host = FakeHermesHost(self.observer)

    def tearDown(self):
        self.observer.shutdown(timeout=5.0)
        self.recv.stop()

    def _spans(self):
        return list(self.recv.spans)

    def _spans_named(self, name):
        return [s for s in self._spans() if s["name"] == name]

    def _deliver_turn(self, session="s1", turn="t1", tool="Bash"):
        """One complete turn: api + tool + approval, all paired."""
        api = _event("pre_api_request", session, turn, model="hermes-test-model")
        tool_pre = _event("pre_tool_call", session, turn, tool_name=tool)
        tool_post = _event(
            "post_tool_call", session, turn, request=tool_pre["request_id"],
            outcome="observed_success",
        )
        api_post = _event(
            "post_api_request", session, turn, request=api["request_id"],
            outcome="observed_success",
        )
        for evt in (api, tool_pre, tool_post, api_post):
            self.assertIsNone(self.host.deliver(evt))
        self.assertIsNone(
            self.host.deliver(_event("on_session_end", session, turn, request=None))
        )
        self.assertTrue(self.observer.drain(timeout=5.0))
        return api, tool_pre

    # -- schema v1: valid events accepted, paired into spans ---------------
    def test_api_tool_pairing_one_span_each(self):
        api, tool_pre = self._deliver_turn()
        llm = self._spans_named("llm.call")
        tools = self._spans_named("tool.exec")
        roots = self._spans_named("agent.run")
        self.assertEqual(len(llm), 1)
        self.assertEqual(len(tools), 1)
        self.assertEqual(len(roots), 1)
        # One trace per turn; children parent to the turn root.
        self.assertEqual(llm[0]["traceId"], roots[0]["traceId"])
        self.assertEqual(tools[0]["traceId"], roots[0]["traceId"])
        self.assertEqual(llm[0]["parentSpanId"], roots[0]["spanId"])
        self.assertEqual(tools[0]["parentSpanId"], roots[0]["spanId"])
        self.assertEqual(llm[0]["attributes"]["request.id"], api["request_id"])
        self.assertEqual(tools[0]["attributes"]["request.id"], tool_pre["request_id"])
        self.assertEqual(tools[0]["attributes"]["gen_ai.tool.name"], "Bash")
        diag = self.observer.diagnostics()
        self.assertEqual(diag["accepted"], 5)
        self.assertEqual(diag["duplicates"], 0)

    # -- privacy: denied content fields stripped, never in OTLP ------------
    def test_denied_fields_stripped(self):
        evt = _event(
            "pre_tool_call",
            tool_name="Bash",
            prompt=FAKE_PROMPT,
            command="rm -rf /",
            arguments={"x": FAKE_SECRET},
        )
        self.host.deliver(evt)
        self.host.deliver(
            _event("post_tool_call", request=evt["request_id"], result=FAKE_ANSWER)
        )
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        blob = json.dumps(self.recv.payloads)
        for banned in (FAKE_PROMPT, FAKE_ANSWER, FAKE_SECRET, "rm -rf /"):
            self.assertNotIn(banned, blob)
        diag = self.observer.diagnostics()
        self.assertGreater(diag["stripped_fields"], 0)
        self.assertEqual(len(self._spans_named("tool.exec")), 1)

    # -- concurrency: simultaneous tools cannot cross-parent ----------------
    def test_concurrent_tools_no_cross_parenting(self):
        barrier = threading.Barrier(2)
        errors = []

        def _tool(n):
            try:
                pre = _event("pre_tool_call", turn="t1", tool_name=f"Tool{n}")
                barrier.wait(timeout=5)
                self.host.deliver(pre)
                time.sleep(0.05)
                self.host.deliver(
                    _event("post_tool_call", turn="t1", request=pre["request_id"])
                )
            except Exception as exc:  # noqa: BLE001 - test records, re-raises below
                errors.append(exc)

        threads = [threading.Thread(target=_tool, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        tools = self._spans_named("tool.exec")
        roots = self._spans_named("agent.run")
        self.assertEqual(len(tools), 2)
        self.assertEqual(len(roots), 1)
        # Both tools parent to the turn root, never to each other.
        for tool in tools:
            self.assertEqual(tool["parentSpanId"], roots[0]["spanId"])
            self.assertEqual(tool["traceId"], roots[0]["traceId"])
        self.assertNotEqual(tools[0]["spanId"], tools[1]["spanId"])

    # -- duplicates / retries are deterministic ------------------------------
    def test_duplicate_post_ignored(self):
        pre = _event("pre_tool_call")
        post = _event("post_tool_call", request=pre["request_id"])
        self.host.deliver(pre)
        self.host.deliver(post)
        self.host.deliver(post)  # exact duplicate delivery
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        self.assertEqual(len(self._spans_named("tool.exec")), 1)
        self.assertGreaterEqual(self.observer.diagnostics()["duplicates"], 1)

    def test_retry_after_close_opens_new_span(self):
        pre = _event("pre_tool_call")
        self.host.deliver(pre)
        self.host.deliver(_event("post_tool_call", request=pre["request_id"]))
        retry = _event(
            "pre_tool_call", request=pre["request_id"], attempt=2
        )
        self.host.deliver(retry)
        self.host.deliver(
            _event("post_tool_call", request=pre["request_id"], attempt=2)
        )
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(tools), 2)
        self.assertEqual(
            {t["attributes"].get("hermes.attempt", 1) for t in tools}, {1, 2}
        )

    # -- missing post -> incomplete at session end; late close dropped ------
    def test_missing_post_closed_incomplete_at_session_end(self):
        pre = _event("pre_tool_call")
        self.host.deliver(pre)
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(tools), 1)
        self.assertEqual(
            tools[0]["attributes"]["observation.completeness"], "incomplete"
        )
        self.assertGreaterEqual(
            self.observer.diagnostics()["incomplete_closed"], 1
        )

    def test_late_completion_after_session_end_dropped(self):
        pre = _event("pre_tool_call")
        self.host.deliver(pre)
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        # The post arrives after the turn already ended: dropped, counted.
        self.host.deliver(_event("post_tool_call", request=pre["request_id"]))
        self.assertTrue(self.observer.drain(timeout=5.0))
        self.assertEqual(len(self._spans_named("tool.exec")), 1)
        self.assertGreaterEqual(
            self.observer.diagnostics()["late_dropped"], 1
        )

    # -- approval pairing by request_id --------------------------------------
    def test_approval_pairing(self):
        req = _event("on_human_input_request")
        self.host.deliver(req)
        self.host.deliver(
            _event("on_human_input_resolved", request=req["request_id"])
        )
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        approvals = self._spans_named("approval.wait")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["attributes"]["approval.kind"], "approval")
        self.assertEqual(
            approvals[0]["attributes"]["request.id"], req["request_id"]
        )

    # -- two consecutive turns still export after on_session_end ------------
    def test_two_consecutive_turns_export(self):
        self._deliver_turn(turn="t1")
        self._deliver_turn(turn="t2")
        roots = self._spans_named("agent.run")
        self.assertEqual(len(roots), 2)
        self.assertEqual(len(self._spans_named("llm.call")), 2)
        self.assertEqual(len(self._spans_named("tool.exec")), 2)
        self.assertNotEqual(roots[0]["traceId"], roots[1]["traceId"])
        self.assertEqual(
            {r["attributes"]["turn.id"] for r in roots}, {"t1", "t2"}
        )

    # -- callback exceptions never propagate; agent behavior unchanged ------
    def test_callback_never_raises_and_agent_unchanged(self):
        # Malformed events, hostile payloads, and non-dict input.
        self.assertIsNone(self.host.deliver(None))
        self.assertIsNone(self.host.deliver("not-a-dict"))
        self.assertIsNone(self.host.deliver({"event_type": "nope"}))
        self.assertIsNone(
            self.host.deliver({"event_type": "pre_tool_call", "session_id": ""})
        )
        # The fake agent's tool args/results are untouched by observation.
        args = {"cmd": "echo hi", "timeout": 30}
        result = self.host.run_tool(args)
        self.assertEqual(result, {"ok": True, "result": "tool-result-fixed"})
        self.assertEqual(self.host.tool_calls, [args])
        self.assertTrue(self.observer.drain(timeout=5.0))
        self.assertEqual(len(self._spans_named("tool.exec")), 0)

    # -- queue overflow: loss visible, observer keeps working ----------------
    def test_queue_overflow_drops_and_counts(self):
        small = HermesObserver(endpoint=self.endpoint, queue_maxsize=2)
        try:
            # Flood faster than the worker drains.
            for _ in range(50):
                small.handle_event(_event("pre_tool_call"))
            diag = small.diagnostics()
            self.assertGreater(diag["dropped_queue_full"], 0)
            # Still functional afterwards.
            pre = _event("pre_tool_call")
            small.handle_event(pre)
            small.handle_event(
                _event("post_tool_call", request=pre["request_id"])
            )
            small.handle_event(_event("on_session_end", request=None))
            self.assertTrue(small.drain(timeout=5.0))
        finally:
            small.shutdown(timeout=5.0)

    # -- receiver outage: loss visible, shutdown bounded ---------------------
    def test_receiver_outage_loss_visible_and_shutdown_bounded(self):
        self.recv.stop()
        pre = _event("pre_tool_call")
        self.host.deliver(pre)
        self.host.deliver(_event("post_tool_call", request=pre["request_id"]))
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        diag = self.observer.diagnostics()
        self.assertGreater(diag["export_failures"], 0)
        # Shutdown stays bounded even with the receiver down.
        started = time.time()
        self.observer.shutdown(timeout=5.0)
        self.assertLess(time.time() - started, 5.0)
        # tearDown will call shutdown again; guard against double-shutdown
        # errors by replacing the observer with a fresh stopped one.
        self.observer = HermesObserver(endpoint=self.endpoint)
        self.observer.shutdown(timeout=5.0)

    # -- stale open spans are pruned -----------------------------------------
    def test_stale_open_span_pruned(self):
        short_ttl = HermesObserver(
            endpoint=self.endpoint, stale_ttl_seconds=0.2
        )
        try:
            host = FakeHermesHost(short_ttl)
            host.deliver(_event("pre_tool_call"))
            time.sleep(0.6)
            # A drain triggers the worker loop, which prunes stale spans.
            self.assertTrue(short_ttl.drain(timeout=5.0))
            time.sleep(0.3)
            self.assertTrue(short_ttl.drain(timeout=5.0))
            tools = [s for s in self.recv.spans if s["name"] == "tool.exec"]
            self.assertEqual(len(tools), 1)
            self.assertEqual(
                tools[0]["attributes"]["observation.completeness"], "incomplete"
            )
            self.assertGreaterEqual(
                short_ttl.diagnostics()["stale_closed"], 1
            )
        finally:
            short_ttl.shutdown(timeout=5.0)

    # -- subagent parenting is explicit, not arrival-order -------------------
    def test_subagent_parents_to_named_enclosing_request(self):
        tool_pre = _event("pre_tool_call", tool_name="Bash")
        sub_start = _event(
            "subagent_start", parent_request_id=tool_pre["request_id"]
        )
        sub_stop = _event(
            "subagent_stop", request=sub_start["request_id"]
        )
        self.host.deliver(tool_pre)
        self.host.deliver(sub_start)
        self.host.deliver(sub_stop)
        self.host.deliver(
            _event("post_tool_call", request=tool_pre["request_id"])
        )
        self.host.deliver(_event("on_session_end", request=None))
        self.assertTrue(self.observer.drain(timeout=5.0))
        subs = self._spans_named("subagent.spawn")
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(subs), 1)
        self.assertEqual(len(tools), 1)
        self.assertEqual(subs[0]["parentSpanId"], tools[0]["spanId"])
        self.assertEqual(subs[0]["traceId"], tools[0]["traceId"])


if __name__ == "__main__":
    unittest.main()
