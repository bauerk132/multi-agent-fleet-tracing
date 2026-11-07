"""Milestone 4 tests (unittest) — copilot_plugin adapter.

Covers the verified Copilot surface (official VS Code docs, crawled
2026-11-07): native span kinds invoke_agent / chat / execute_tool /
execute_hook, subagent nesting via runSubagent, content + identity
stripping, error taxonomy, dedup, out-of-scope services (Claude),
lazy roots (children before root), session fallback, span-event
counting, JSONL ingestion (both line shapes), queue overflow, stale
pruning, staged standalone loading, and the missing-OTel guard.

Synthetic OTLP payloads only — no VS Code build is installed here.
The live contract (real file-exporter line shape, real span delivery)
is the reviewer's job on the laptop (see copilot_plugin/config.md).

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
import uuid
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent.parent)
import sys

sys.path.insert(0, sys_path)

import copilot_plugin
from copilot_plugin import create_observer
from copilot_plugin.observer import (
    _map_error_type,
    _status_outcome,
    flatten_payload,
)
from receiver import LocalReceiver

HERE = Path(__file__).resolve().parent.parent
PLUGIN_DIR = HERE / "copilot_plugin"

TRACE_ID = "a" * 32
TRACE_ID_2 = "b" * 32


def _hex16(n):
    return "%016x" % n


def _span(name, span_id, trace_id=TRACE_ID, parent=None, attrs=None,
          status=0, start=1_000_000_000_000_000_000, end=1_000_000_001_000_000_000,
          events=None, service="copilot-chat"):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": dict(attrs or {}),
        "events": list(events or []),
        "status": {"code": status, "message": None},
        "resource": {"service.name": service},
        "scope": {"name": "copilot-chat", "version": "1.0.0"},
    }


def _payload(spans, service="copilot-chat"):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "copilot-chat", "version": "1.0.0"},
                        "spans": [dict(s, attributes=_to_otlp_attrs(s["attributes"])) for s in spans],
                    }
                ],
            }
        ]
    }


def _to_otlp_attrs(attrs):
    out = []
    for key, value in (attrs or {}).items():
        if isinstance(value, bool):
            out.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            out.append({"key": key, "value": {"intValue": str(value)}})
        elif isinstance(value, float):
            out.append({"key": key, "value": {"doubleValue": value}})
        else:
            out.append({"key": key, "value": {"stringValue": str(value)}})
    return out


def _invoke_agent(span_id, trace_id=TRACE_ID, parent=None, conv="conv-1",
                  model="gpt-4o", turn_count=3, status=0, **extra):
    attrs = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": "github",
        "gen_ai.agent.name": "copilot",
        "gen_ai.conversation.id": conv,
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 300,
        "github.copilot.agent.type": "builtin",
        "github.copilot.git.branch": "staging",
        "github.copilot.git.commit_sha": "d2919f14e38afe4764ac832eda01fe58f77750f5",
        "copilot_chat.turn_count": turn_count,
    }
    attrs.update(extra)
    return _span("invoke_agent", span_id, trace_id, parent, attrs, status=status)


def _chat(span_id, trace_id=TRACE_ID, parent=None, model="gpt-4o", status=0, **extra):
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "github",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": 400,
        "gen_ai.usage.output_tokens": 100,
        "copilot_chat.time_to_first_token": 250,
        "server.address": "api.githubcopilot.com",
    }
    attrs.update(extra)
    return _span("chat", span_id, trace_id, parent, attrs, status=status)


def _tool(span_id, trace_id=TRACE_ID, parent=None, tool="readFile",
          call_id=None, status=0, **extra):
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool,
        "gen_ai.tool.type": "function",
        "gen_ai.tool.call.id": call_id or ("call-" + span_id),
    }
    attrs.update(extra)
    return _span("execute_tool", span_id, trace_id, parent, attrs, status=status)


def _hook(span_id, trace_id=TRACE_ID, parent=None, status=0, **extra):
    attrs = {
        "gen_ai.operation.name": "execute_hook",
        "github.copilot.hook.decision": "pass",
        "github.copilot.hook.duration": 0.05,
        "github.copilot.hook.tool_names": '["readFile"]',
        "copilot_chat.hook_type": "PreToolUse",
        "copilot_chat.hook_result_kind": "success",
    }
    attrs.update(extra)
    return _span("execute_hook", span_id, trace_id, parent, attrs, status=status)


# Subprocess script: load the plugin from a staged copy of ONLY the
# copilot_plugin directory (no observability/ on sys.path), create an
# observer, drive handle_payload, print STAGED-OK.
_STAGED_SCRIPT = '''
import os
import sys

staged = sys.argv[1]
assert not any("observability" in p for p in sys.path), sys.path

import copilot_plugin

assert copilot_plugin.__file__.startswith(staged), copilot_plugin.__file__

observer = copilot_plugin.create_observer()
trace = "c" * 32
root = "1111111111111111"
chat = "2222222222222222"
payload = {
    "resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "copilot-chat"}}]},
        "scopeSpans": [{"scope": {"name": "copilot-chat", "version": "1.0.0"}, "spans": [
            {"traceId": trace, "spanId": root, "name": "invoke_agent",
             "startTimeUnixNano": "1000000000000000000", "endTimeUnixNano": "1000000005000000000",
             "attributes": [
                 {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                 {"key": "gen_ai.conversation.id", "value": {"stringValue": "conv-staged"}},
                 {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
             ], "status": {"code": 1}},
            {"traceId": trace, "spanId": chat, "parentSpanId": root, "name": "chat",
             "startTimeUnixNano": "1000000001000000000", "endTimeUnixNano": "1000000002000000000",
             "attributes": [
                 {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                 {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                 {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "10"}},
                 {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "5"}},
             ], "status": {"code": 1}},
        ]}],
    }]
}
assert observer.handle_payload(payload) == 2
assert observer.drain(timeout=5.0)
diag = observer.diagnostics()
assert diag["accepted"] == 2, diag
observer.shutdown(timeout=5.0)
print("STAGED-OK kinds=4 accepted=2")
'''

# Subprocess script: block every opentelemetry import, then prove that
# importing the plugin still succeeds and create_observer() fails with a
# clear RuntimeError (never a leaked ImportError traceback).
_GUARD_SCRIPT = '''
import importlib.abc
import sys

class _BlockOTel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "opentelemetry" or fullname.startswith("opentelemetry."):
            raise ImportError("No module named %r (blocked for test)" % fullname)
        return None

sys.meta_path.insert(0, _BlockOTel())

import copilot_plugin  # must succeed: stdlib imports only

try:
    copilot_plugin.create_observer()
except RuntimeError as exc:
    assert "OpenTelemetry" in str(exc), str(exc)
    assert "opentelemetry-api==1.45.1" in str(exc), str(exc)
    print("GUARD-OK", exc)
except ImportError as exc:
    print("GUARD-FAIL ImportError leaked:", exc)
    sys.exit(2)
else:
    print("GUARD-FAIL no error raised")
    sys.exit(2)
'''


class CopilotPluginTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-m4-")
        self.tmpdir = Path(self.tmp)
        self.recv = LocalReceiver(out_path=str(self.tmpdir / "received.jsonl")).start()
        self._old_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = self.recv.endpoint
        self.observer = create_observer()
        self.observers = [self.observer]

    def tearDown(self):
        for observer in self.observers:
            try:
                observer.shutdown(timeout=5.0)
            except Exception:
                pass
        self.recv.stop()
        if self._old_endpoint is None:
            os.environ.pop("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", None)
        else:
            os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = self._old_endpoint

    def _spans_named(self, name):
        return [s for s in self.recv.spans if s["name"] == name]

    def _deliver(self, payload):
        n = self.observer.handle_payload(payload)
        self.assertTrue(self.observer.drain(timeout=5.0))
        return n

    # -- full turn: root + chat + tool + hook + subagent nesting --------
    def test_full_turn_end_to_end(self):
        root = _hex16(1)
        chat1 = _hex16(2)
        tool1 = _hex16(3)
        hook1 = _hex16(4)
        chat2 = _hex16(5)
        sub_root = _hex16(6)
        sub_chat = _hex16(7)
        spans = [
            _invoke_agent(root),
            _chat(chat1, parent=root),
            _tool(tool1, parent=chat1, tool="readFile"),
            _hook(hook1, parent=tool1),
            _chat(chat2, parent=root),
            # Subagent: runSubagent tool whose child invoke_agent nests
            # under the tool span; the subagent's own chat nests under it.
            _tool(_hex16(8), parent=chat2, tool="runSubagent", call_id="call-sub"),
            _invoke_agent(sub_root, parent=_hex16(8), conv="conv-1"),
            _chat(sub_chat, parent=sub_root),
        ]
        self.assertEqual(self._deliver(_payload(spans)), 8)
        diag = self.observer.diagnostics()
        self.assertEqual(diag["accepted"], 8, diag)
        self.assertEqual(diag["invalid_spans"], 0, diag)
        self.assertEqual(diag["out_of_scope"], 0, diag)

        roots = self._spans_named("agent.run")
        llm = self._spans_named("llm.call")
        tools = self._spans_named("tool.exec")
        hooks = self._spans_named("hook.exec")
        self.assertEqual(len(roots), 2)
        self.assertEqual(len(llm), 3)
        self.assertEqual(len(tools), 2)
        self.assertEqual(len(hooks), 1)

        # One fleet trace for the whole native trace; subagent root
        # nests under the runSubagent tool span.
        trace_ids = {s["traceId"] for s in self.recv.spans}
        self.assertEqual(len(trace_ids), 1)
        parent_root = [r for r in roots if r["attributes"]["turn.id"] == TRACE_ID + ":" + root][0]
        subagent_root = [r for r in roots if r["attributes"]["turn.id"] == TRACE_ID + ":" + sub_root][0]
        run_tool = [t for t in tools if t["attributes"]["gen_ai.tool.name"] == "runSubagent"][0]
        self.assertEqual(subagent_root["parentSpanId"], run_tool["spanId"])
        self.assertEqual(parent_root.get("parentSpanId"), None)
        # Operation spans parent to their turn root (or native parent).
        self.assertEqual(llm[0]["attributes"]["turn.id"], TRACE_ID + ":" + root)
        self.assertEqual(tools[0]["attributes"]["request.id"], "call-" + tool1)
        # Tokens and model ride through; server.address is dropped.
        self.assertEqual(parent_root["attributes"]["gen_ai.usage.input_tokens"], 1200)
        self.assertEqual(parent_root["attributes"]["gen_ai.request.model"], "gpt-4o")
        self.assertEqual(parent_root["attributes"]["copilot.turn_count"], 3)
        self.assertNotIn("server.address", llm[0]["attributes"])
        # Native timestamps preserved.
        self.assertEqual(int(llm[0]["startTimeUnixNano"]), 1_000_000_000_000_000_000)

    # -- privacy: content + identity + repo URL stripped ------------------
    def test_content_and_identity_stripped(self):
        root = _hex16(1)
        spans = [
            _invoke_agent(root, **{
                "github.copilot.git.repository": "https://github.com/someone/secret-repo.git",
                "github.copilot.github.org": "someone",
            }),
            _chat(_hex16(2), parent=root, **{
                "gen_ai.input.messages": "PROMPT: my password is hunter2",
                "gen_ai.output.messages": "SECRET ANSWER",
                "gen_ai.tool.definitions": "SCHEMAS",
            }),
            _tool(_hex16(3), parent=root, tool="runCommand", **{
                "gen_ai.tool.call.arguments": '{"cmd": "rm -rf /"}',
                "gen_ai.tool.call.result": "OUTPUT DATA",
                "github.copilot.tool.parameters.command": "rm -rf /",
                "github.copilot.tool.parameters.file_path": "/home/kbaue/secret.txt",
            }),
            _hook(_hex16(4), parent=root, **{
                "copilot_chat.hook_input": "HOOK PAYLOAD",
                "copilot_chat.hook_output": "HOOK STDOUT",
            }),
        ]
        # Identity attributes ride on the resource in a misconfigured
        # upstream; the adapter drops them at the span level too.
        spans[0]["attributes"]["user.name"] = "kbaue"
        spans[0]["attributes"]["host.name"] = "DESKTOP-QQ98CIM"
        self.assertEqual(self._deliver(_payload(spans)), 4)
        diag = self.observer.diagnostics()
        self.assertGreater(diag["stripped_fields"], 0, diag)
        blob = json.dumps(self.recv.spans)
        for banned in (
            "hunter2", "SECRET ANSWER", "SCHEMAS", "rm -rf", "OUTPUT DATA",
            "secret.txt", "HOOK PAYLOAD", "HOOK STDOUT", "secret-repo",
            "someone", "kbaue", "DESKTOP-QQ98CIM",
        ):
            self.assertNotIn(banned, blob, banned)
        # The safe metadata still lands.
        self.assertEqual(len(self._spans_named("agent.run")), 1)
        self.assertEqual(len(self._spans_named("tool.exec")), 1)

    # -- error taxonomy ----------------------------------------------------
    def test_error_taxonomy(self):
        self.assertEqual(_map_error_type("TimeoutError"), "Timeout")
        self.assertEqual(_map_error_type("tool_error"), "ToolError")
        self.assertEqual(_map_error_type("LLMError"), "LLMError")
        self.assertEqual(_map_error_type("SomethingWeird"), "Unknown")
        self.assertEqual(_map_error_type(None), None)
        self.assertEqual(_status_outcome(2), (True, "observed_failure"))
        self.assertEqual(_status_outcome(1), (False, "observed_success"))
        self.assertEqual(_status_outcome(0), (False, "unknown"))

        root = _hex16(1)
        spans = [
            _invoke_agent(root),
            _chat(_hex16(2), parent=root, status=2, **{"error.type": "TimeoutError"}),
            _tool(_hex16(3), parent=root, status=2, **{"error.type": "tool_error"}),
        ]
        self._deliver(_payload(spans))
        llm = self._spans_named("llm.call")
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(llm), 1)
        self.assertEqual(len(tools), 1)
        self.assertEqual(llm[0]["status"]["code"], 2)
        self.assertEqual(llm[0]["attributes"]["error.type"], "Timeout")
        self.assertEqual(tools[0]["status"]["code"], 2)
        self.assertEqual(tools[0]["attributes"]["error.type"], "ToolError")

    # -- dedup --------------------------------------------------------------
    def test_duplicate_spans_dropped(self):
        root = _hex16(1)
        payload = _payload([_invoke_agent(root), _chat(_hex16(2), parent=root)])
        self.assertEqual(self._deliver(payload), 2)
        # Same payload again: every span is a duplicate.
        self.assertEqual(self.observer.handle_payload(payload), 0)
        self.assertTrue(self.observer.drain(timeout=5.0))
        diag = self.observer.diagnostics()
        self.assertEqual(diag["duplicates"], 2, diag)
        self.assertEqual(len(self._spans_named("agent.run")), 1)
        self.assertEqual(len(self._spans_named("llm.call")), 1)

    # -- out of scope: Claude agent + claude-code service -------------------
    def test_claude_spans_out_of_scope(self):
        root = _hex16(1)
        claude_root = _span(
            "invoke_agent", _hex16(9), TRACE_ID, None,
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "claude",
                "gen_ai.conversation.id": "conv-claude",
                "gen_ai.request.model": "claude-opus-4-6",
            },
        )
        claude_code = _span(
            "invoke_agent", _hex16(10), TRACE_ID_2, None,
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": "conv-cc",
            },
            service="claude-code",
        )
        self._deliver(_payload([_invoke_agent(root), claude_root]))
        self._deliver(_payload([claude_code], service="claude-code"))
        diag = self.observer.diagnostics()
        self.assertEqual(diag["out_of_scope"], 2, diag)
        self.assertEqual(diag["accepted"], 1, diag)
        self.assertEqual(len(self._spans_named("agent.run")), 1)
        self.assertNotIn("claude-opus-4-6", json.dumps(self.recv.spans))

    # -- unknown operation ---------------------------------------------------
    def test_unknown_operation_dropped(self):
        span = _span("summarize", _hex16(1), TRACE_ID, None,
                     {"gen_ai.operation.name": "summarize"})
        self._deliver(_payload([span]))
        diag = self.observer.diagnostics()
        self.assertEqual(diag["unknown_operation"], 1, diag)
        self.assertEqual(len(self.recv.spans), 0)

    # -- children before root: lazy root folds -------------------------------
    def test_children_before_root_lazy_root_folds(self):
        root = _hex16(1)
        chat = _hex16(2)
        # Children first, in their own payload; root arrives later.
        self._deliver(_payload([_chat(chat, parent=root)]))
        self._deliver(_payload([_invoke_agent(root)]))
        diag = self.observer.diagnostics()
        self.assertEqual(diag["lazy_roots_folded"], 1, diag)
        roots = self._spans_named("agent.run")
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["attributes"]["session.id"], "conv-1")
        self.assertEqual(roots[0]["attributes"]["turn.id"], TRACE_ID + ":" + root)
        self.assertEqual(roots[0]["attributes"]["gen_ai.request.model"], "gpt-4o")
        self.assertEqual(len(self._spans_named("llm.call")), 1)

    # -- parallel tools keep distinct request ids ---------------------------
    def test_parallel_tools_distinct(self):
        root = _hex16(1)
        spans = [
            _invoke_agent(root),
            _tool(_hex16(2), parent=root, tool="readFile", call_id="call-a"),
            _tool(_hex16(3), parent=root, tool="runCommand", call_id="call-b"),
        ]
        self._deliver(_payload(spans))
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(tools), 2)
        self.assertEqual(
            sorted(t["attributes"]["request.id"] for t in tools),
            ["call-a", "call-b"],
        )

    # -- session fallback when no conversation id is anywhere ----------------
    def test_session_fallback(self):
        root = _hex16(1)
        invoke = _invoke_agent(root)
        del invoke["attributes"]["gen_ai.conversation.id"]
        chat = _chat(_hex16(2), parent=root)
        self._deliver(_payload([invoke, chat]))
        diag = self.observer.diagnostics()
        self.assertGreater(diag["session_fallback"], 0, diag)
        roots = self._spans_named("agent.run")
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["attributes"]["session.id"], "trace:" + TRACE_ID)

    # -- span events counted, never emitted ----------------------------------
    def test_span_events_counted_not_emitted(self):
        root = _hex16(1)
        events = [
            {"name": "copilot_chat.session.start", "timeUnixNano": "1000000000000000000", "attributes": {}},
            {"name": "copilot_chat.agent.turn", "timeUnixNano": "1000000000000000000", "attributes": {}},
            {"name": "copilot_chat.tool.call", "timeUnixNano": "1000000000000000000", "attributes": {}},
            {"name": "copilot_chat.edit.feedback", "timeUnixNano": "1000000000000000000", "attributes": {}},
            {"name": "copilot_chat.user.feedback", "timeUnixNano": "1000000000000000000", "attributes": {}},
            {"name": "gen_ai.client.inference.operation.details", "timeUnixNano": "1000000000000000000", "attributes": {}},
            {"name": "something.else", "timeUnixNano": "1000000000000000000", "attributes": {}},
        ]
        span = _invoke_agent(root)
        span["events"] = events
        self._deliver(_payload([span]))
        diag = self.observer.diagnostics()
        self.assertEqual(diag["session_start_seen"], 1, diag)
        self.assertEqual(diag["agent_turn_seen"], 1, diag)
        self.assertEqual(diag["tool_call_event_seen"], 1, diag)
        self.assertEqual(diag["user_event_seen"], 2, diag)
        self.assertEqual(diag["inference_event_seen"], 1, diag)
        self.assertEqual(diag["unknown_event_seen"], 1, diag)
        self.assertEqual(len(self.recv.spans), 1)  # only the root span

    # -- JSONL ingestion: both line shapes + junk ----------------------------
    def test_jsonl_ingestion(self):
        root = _hex16(1)
        chat = _hex16(2)
        payload_line = json.dumps(_payload([_invoke_agent(root)]))
        single_line = json.dumps(
            flatten_payload(_payload([_chat(chat, parent=root)]))[0]
        )
        metrics_line = json.dumps({"resourceMetrics": [{"resource": {}}]})
        path = self.tmpdir / "copilot-otel.jsonl"
        path.write_text(
            "\n".join([payload_line, single_line, metrics_line, "not json", "", '{"weird": 1}'])
            + "\n"
        )
        summary = self.observer.ingest_jsonl(str(path))
        self.assertTrue(self.observer.drain(timeout=5.0))
        self.assertEqual(summary["lines"], 5, summary)
        self.assertEqual(summary["spans_enqueued"], 2, summary)
        self.assertEqual(summary["skipped_non_trace"], 1, summary)
        self.assertEqual(summary["invalid_lines"], 2, summary)
        self.assertEqual(summary["ingest_errors"], 0, summary)
        self.assertEqual(len(self._spans_named("agent.run")), 1)
        self.assertEqual(len(self._spans_named("llm.call")), 1)

    def test_jsonl_missing_file(self):
        summary = self.observer.ingest_jsonl(str(self.tmpdir / "nope.jsonl"))
        self.assertEqual(summary["ingest_errors"], 1, summary)
        self.assertEqual(summary["lines"], 0, summary)

    # -- ingest never raises --------------------------------------------------
    def test_handle_payload_never_raises(self):
        for bad in (None, "garbage", {}, {"resourceSpans": "nope"},
                    {"resourceSpans": [{"resource": {}, "scopeSpans": "nope"}]},
                    [{"traceId": "x"}]):
            self.assertEqual(self.observer.handle_payload(bad), 0)
        # Malformed span dicts inside a payload are counted, not raised.
        payload = {"resourceSpans": [{"resource": {}, "scopeSpans": [{"scope": {}, "spans": [
            {"traceId": "not-hex", "spanId": "zz", "name": "chat", "attributes": []},
            {"traceId": TRACE_ID, "spanId": _hex16(1), "name": "chat",
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}]},
        ]}]}]}
        self.assertEqual(self.observer.handle_payload(payload), 1)
        self.assertTrue(self.observer.drain(timeout=5.0))
        self.assertEqual(len(self._spans_named("llm.call")), 1)

    # -- queue overflow drops + counts, never blocks --------------------------
    def test_queue_overflow(self):
        small = create_observer(queue_maxsize=2)
        self.observers.append(small)
        spans = [_invoke_agent(_hex16(i), trace_id="%032x" % i) for i in range(1, 12)]
        # Enqueue faster than the worker drains: some must drop.
        enqueued = small.handle_payload(_payload(spans))
        self.assertTrue(small.drain(timeout=5.0))
        diag = small.diagnostics()
        self.assertGreater(diag["dropped_queue_full"], 0, diag)
        self.assertEqual(diag["accepted"], enqueued, diag)

    # -- stale pruning closes a lazy root incomplete --------------------------
    def test_stale_pruning(self):
        short = create_observer(stale_ttl_seconds=0.3)
        self.observers.append(short)
        root = _hex16(1)
        short.handle_payload(_payload([_chat(_hex16(2), parent=root)]))
        self.assertTrue(short.drain(timeout=5.0))
        import time as _time
        _time.sleep(1.0)
        self.assertTrue(short.drain(timeout=5.0))
        diag = short.diagnostics()
        self.assertEqual(diag["stale_closed"], 1, diag)
        self.assertEqual(diag["incomplete_closed"], 1, diag)
        roots = [s for s in self.recv.spans if s["name"] == "agent.run"]
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["attributes"]["observation.completeness"], "incomplete")

    # -- config doc names the verified settings --------------------------------
    def test_config_doc_names_verified_settings(self):
        text = (PLUGIN_DIR / "config.md").read_text()
        for required in (
            "github.copilot.chat.otel.enabled",
            "github.copilot.chat.otel.exporterType",
            "github.copilot.chat.otel.outfile",
            "github.copilot.chat.otel.captureContent",
            "github.copilot.chat.otel.captureIdentity",
            "COPILOT_OTEL_ENABLED",
            "COPILOT_OTEL_FILE_EXPORTER_PATH",
            "COPILOT_OTEL_CAPTURE_CONTENT",
            "COPILOT_OTEL_CAPTURE_IDENTITY",
        ):
            self.assertIn(required, text, required)
        settings = json.loads((HERE / "examples" / "copilot-settings.json").read_text())
        self.assertFalse(settings["github.copilot.chat.otel.captureContent"])
        self.assertFalse(settings["github.copilot.chat.otel.captureIdentity"])
        self.assertEqual(settings["github.copilot.chat.otel.exporterType"], "file")

    # -- staged standalone load (subprocess) -----------------------------------
    def test_package_loads_standalone_from_staged_copy(self):
        staged = self.tmpdir / "staged"
        shutil.copytree(
            PLUGIN_DIR, staged / "copilot_plugin", ignore=shutil.ignore_patterns("__pycache__")
        )
        script = self.tmpdir / "staged_check.py"
        script.write_text(_STAGED_SCRIPT)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(staged)
        env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = self.recv.endpoint
        proc = subprocess.run(
            [sys.executable, str(script), str(staged)],
            cwd=str(staged),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STAGED-OK kinds=4 accepted=2", proc.stdout)
        self.assertEqual(len(self._spans_named("agent.run")), 1)
        self.assertEqual(len(self._spans_named("llm.call")), 1)

    # -- missing OTel -> clear RuntimeError, no ImportError leak ----------------
    def test_missing_otel_guard(self):
        script = self.tmpdir / "guard_check.py"
        script.write_text(_GUARD_SCRIPT)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(HERE)
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(self.tmpdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("GUARD-OK", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertNotIn("GUARD-FAIL", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
