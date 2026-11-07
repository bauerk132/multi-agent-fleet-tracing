"""Milestone 3 Fix Round 5 tests (unittest) — hermes_plugin itself.

Covers the five FR5 findings: standalone staged loading (FR5-01),
python_dependencies + missing-OTel guard (FR5-02), **kwargs callbacks
(FR5-03), kwargs -> schema-v1 mapping (FR5-04), provides_hooks +
manifest parity (FR5-05).

Plus the six FR6 findings (exact live Hermes v0.23.0 kwargs per
Antigravity handoff hof_01M6VANB718PNZVHNP): subagent kwargs mapping
(FR6-01), session-boundary turn_id exemption (FR6-02), human-input
outcome normalization (FR6-03), tool error taxonomy (FR6-04),
tool_call_id precedence for parallel tools (FR6-05), callback
exception trap (FR6-06).

The two isolation tests (staged copy, missing OTel) run in a subprocess
so the parent process's sys.path / installed OTel cannot mask a failure.

Run from observability/:
    ../.venv/bin/python -m unittest discover -s tests -t . -v
"""

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys_path = str(Path(__file__).resolve().parent.parent)
import sys

sys.path.insert(0, sys_path)

import hermes_plugin
from hermes_plugin import (
    _HOOK_NAMES,
    _event_from_kwargs,
    _event_from_payload,
    _map_error_type,
    _map_status_to_outcome,
)
from receiver import LocalReceiver

HERE = Path(__file__).resolve().parent.parent
PLUGIN_DIR = HERE / "hermes_plugin"

EXPECTED_DEPENDENCIES = [
    "opentelemetry-api==1.45.1",
    "opentelemetry-sdk==1.45.1",
    "opentelemetry-exporter-otlp-proto-http==1.45.1",
]

# Subprocess script: load the plugin from a staged copy of ONLY the
# hermes_plugin directory (no observability/ on sys.path), register
# against a fake ctx, drive kwargs-only callbacks, print STAGED-OK.
_STAGED_SCRIPT = '''
import os
import sys

staged = sys.argv[1]
assert not any("observability" in p for p in sys.path), sys.path

import hermes_plugin

assert hermes_plugin.__file__.startswith(staged), hermes_plugin.__file__

class Ctx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

ctx = Ctx()
observer = hermes_plugin.register(ctx)
assert sorted(ctx.hooks) == sorted([
    "pre_api_request", "post_api_request", "api_request_error",
    "pre_tool_call", "post_tool_call", "subagent_start", "subagent_stop",
    "on_session_end", "on_session_finalize", "on_session_reset",
    "on_human_input_request", "on_human_input_resolved",
]), sorted(ctx.hooks)

ctx.hooks["pre_tool_call"](session_id="s1", turn_id="t1",
                            request_id="req-staged-1", tool_name="Bash")
ctx.hooks["post_tool_call"](session_id="s1", turn_id="t1",
                            request_id="req-staged-1", status="success")
ctx.hooks["on_session_end"](session_id="s1", turn_id="t1")
assert observer.drain(timeout=5.0)
diag = observer.diagnostics()
assert diag["accepted"] == 3, diag
observer.shutdown(timeout=5.0)
print("STAGED-OK hooks=12 accepted=3")
'''

# Subprocess script: block every opentelemetry import, then prove that
# importing the plugin still succeeds and register() fails with a clear
# RuntimeError (never a leaked ImportError traceback).
_GUARD_SCRIPT = '''
import importlib.abc
import sys

class _BlockOTel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "opentelemetry" or fullname.startswith("opentelemetry."):
            raise ImportError("No module named %r (blocked for test)" % fullname)
        return None

sys.meta_path.insert(0, _BlockOTel())

import hermes_plugin  # must succeed: stdlib imports only

class Ctx:
    def register_hook(self, name, cb):
        raise AssertionError("must not register without OTel")

try:
    hermes_plugin.register(Ctx())
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


def _parse_manifest_list(text, section):
    """Minimal YAML-list reader: items under a top-level ``section:`` key."""
    items = []
    in_section = False
    for line in text.splitlines():
        if line and not line.startswith((" ", "\t", "#")) and line.endswith(":"):
            in_section = line[:-1].strip() == section
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("- "):
                items.append(stripped[2:].strip().strip('"').strip("'"))
    return items


class FakeCtx:
    """Minimal host ctx: records register_hook(name, callback)."""

    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class HermesPluginTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-m3-fr5-")
        self.tmpdir = Path(self.tmp)
        self.recv = LocalReceiver(out_path=str(self.tmpdir / "received.jsonl")).start()
        self._old_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = self.recv.endpoint
        self.ctx = FakeCtx()
        self.observer = hermes_plugin.register(self.ctx)
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

    def _deliver_session_end(self, session="s1", turn="t1"):
        self.ctx.hooks["on_session_end"](session_id=session, turn_id=turn)
        self.assertTrue(self.observer.drain(timeout=5.0))

    # -- FR5-03: callbacks literally take **kwargs --------------------------
    def test_callback_signature_accepts_var_kwargs(self):
        for name, cb in self.ctx.hooks.items():
            params = inspect.signature(cb).parameters
            self.assertEqual(
                list(params),
                ["kwargs"],
                f"{name} signature is {list(params)}, expected ['kwargs']",
            )
            self.assertEqual(
                params["kwargs"].kind, inspect.Parameter.VAR_KEYWORD
            )
        # Keyword args only — no positional payload.
        self.ctx.hooks["pre_tool_call"](
            session_id="s1", turn_id="t1", request_id="req-kw-1", tool_name="Bash"
        )
        self.assertEqual(self.observer.diagnostics()["accepted"], 1)

    # -- FR5-04: status -> outcome, error dict -> error_type ----------------
    def test_kwargs_status_and_error_mapping(self):
        # Unit-level mapping table.
        self.assertEqual(_map_status_to_outcome("success"), "observed_success")
        self.assertEqual(_map_status_to_outcome("completed"), "observed_success")
        self.assertEqual(_map_status_to_outcome("failed"), "observed_failure")
        self.assertEqual(_map_status_to_outcome("canceled"), "cancelled")
        self.assertEqual(_map_status_to_outcome("timed_out"), "timed_out")
        self.assertEqual(_map_status_to_outcome("bogus-status"), "unknown")
        self.assertEqual(_map_error_type({"type": "TimeoutError"}), "Timeout")
        self.assertEqual(_map_error_type({"kind": "ToolError"}), "ToolError")
        self.assertEqual(_map_error_type({"type": "CancelledError"}), "Cancelled")
        self.assertEqual(_map_error_type(ValueError("boom")), "Unknown")
        self.assertEqual(_map_error_type("LLMError"), "LLMError")

        # End to end: a failed tool call with an error dict.
        self.ctx.hooks["pre_tool_call"](
            session_id="s1", turn_id="t1", request_id="req-err-1", tool_name="Bash"
        )
        self.ctx.hooks["post_tool_call"](
            session_id="s1",
            turn_id="t1",
            request_id="req-err-1",
            status="error",
            error={"type": "TimeoutError"},
        )
        # And a successful api call via status.
        self.ctx.hooks["pre_api_request"](
            session_id="s1", turn_id="t1", request_id="req-ok-1", model="m1"
        )
        self.ctx.hooks["post_api_request"](
            session_id="s1", turn_id="t1", request_id="req-ok-1", status="success"
        )
        self._deliver_session_end()
        tools = self._spans_named("tool.exec")
        llm = self._spans_named("llm.call")
        self.assertEqual(len(tools), 1)
        self.assertEqual(len(llm), 1)
        self.assertEqual(tools[0]["status"]["code"], 2)  # ERROR
        self.assertEqual(tools[0]["attributes"]["error.type"], "Timeout")
        self.assertEqual(llm[0]["status"]["code"], 0)  # UNSET: success

    # -- FR5-04: subagent coordinates ---------------------------------------
    def test_kwargs_subagent_coordinates(self):
        self.ctx.hooks["pre_tool_call"](
            session_id="s1", turn_id="t1", request_id="req-tool-1", tool_name="Bash"
        )
        # Subagent kwargs use subagent_* names and a parent tool call id.
        self.ctx.hooks["subagent_start"](
            subagent_session_id="s1",
            turn_id="t1",
            subagent_id="sub-1",
            parent_tool_call_id="req-tool-1",
        )
        self.ctx.hooks["subagent_stop"](
            session_id="s1", turn_id="t1", subagent_id="sub-1"
        )
        self.ctx.hooks["post_tool_call"](
            session_id="s1", turn_id="t1", request_id="req-tool-1", status="success"
        )
        self._deliver_session_end()
        subs = self._spans_named("subagent.spawn")
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(subs), 1)
        self.assertEqual(len(tools), 1)
        self.assertEqual(subs[0]["attributes"]["request.id"], "sub-1")
        self.assertEqual(subs[0]["attributes"]["session.id"], "s1")
        self.assertEqual(subs[0]["parentSpanId"], tools[0]["spanId"])
        self.assertEqual(subs[0]["traceId"], tools[0]["traceId"])

    # -- FR5-04: human-input events without a turn_id are accepted ----------
    def test_kwargs_human_input_without_turn_id(self):
        self.ctx.hooks["on_human_input_request"](
            session_id="s1", event_id="human-req-1"
        )
        self.ctx.hooks["on_human_input_resolved"](
            session_id="s1", request_id="human-req-1"
        )
        self._deliver_session_end(turn="human-input")
        approvals = self._spans_named("approval.wait")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["attributes"]["turn.id"], "human-input")
        self.assertEqual(approvals[0]["attributes"]["request.id"], "human-req-1")
        self.assertEqual(self.observer.diagnostics()["accepted"], 3)

    # -- FR5-04: payload dict/object path still works -----------------------
    def test_payload_dict_and_object_still_work(self):
        evt = _event_from_payload(
            "pre_tool_call",
            {
                "event_id": "e1",
                "session_id": "s1",
                "turn_id": "t1",
                "tool_call_id": "req-payload-1",
                "tool": "Bash",
            },
        )
        self.assertEqual(evt["request_id"], "req-payload-1")
        self.assertEqual(evt["tool_name"], "Bash")
        obj = SimpleNamespace(
            event_id="e2",
            session_id="s1",
            turn_id="t1",
            api_request_id="req-payload-2",
            model="m1",
        )
        evt = _event_from_payload("pre_api_request", obj)
        self.assertEqual(evt["request_id"], "req-payload-2")
        self.assertEqual(evt["model"], "m1")
        # A payload kwarg is used as fallback under kwargs.
        merged = _event_from_kwargs(
            "pre_tool_call",
            {
                "session_id": "s1",
                "turn_id": "t1",
                "payload": {"tool_call_id": "req-merged-1", "tool": "Bash"},
            },
        )
        self.assertEqual(merged["request_id"], "req-merged-1")
        self.assertEqual(merged["tool_name"], "Bash")

    # -- FR5-05: plugin.yaml declares dependencies + hooks ------------------
    def test_plugin_yaml_declares_dependencies_and_hooks(self):
        text = (PLUGIN_DIR / "plugin.yaml").read_text()
        self.assertEqual(
            _parse_manifest_list(text, "python_dependencies"),
            EXPECTED_DEPENDENCIES,
        )
        self.assertEqual(
            _parse_manifest_list(text, "provides_hooks"), list(_HOOK_NAMES)
        )
        # The manifest and register() agree on the hook set.
        self.assertEqual(sorted(self.ctx.hooks), sorted(_HOOK_NAMES))

    # -- FR5-01: plugin loads standalone from a staged copy -----------------
    def test_plugin_loads_standalone_from_staged_copy(self):
        staged = self.tmpdir / "staged"
        shutil.copytree(
            PLUGIN_DIR, staged / "hermes_plugin", ignore=shutil.ignore_patterns("__pycache__")
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
        self.assertIn("STAGED-OK hooks=12 accepted=3", proc.stdout)
        # The staged plugin exported to this test's receiver.
        self.assertEqual(len(self._spans_named("tool.exec")), 1)

    # -- FR5-02: missing OTel -> clear RuntimeError, no ImportError leak ----
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

    # -- FR6-01: live subagent kwargs (Hermes v0.23.0) ---------------------
    def test_fr6_subagent_live_kwargs_mapping(self):
        # Unit-level: exact live kwargs from tools/delegate_tool.py:356
        # (subagent_start) and tools/delegate_tool_results.py:382
        # (subagent_stop), per handoff hof_01M6VANB718PNZVHNP.
        start = _event_from_kwargs(
            "subagent_start",
            {
                "parent_session_id": "ps-1",
                "parent_turn_id": "pt-1",
                "parent_subagent_id": "par-1",
                "child_session_id": "cs-1",
                "child_subagent_id": "child-1",
                "child_role": "coder",
                "child_goal": "fix bug",
            },
        )
        self.assertEqual(start["session_id"], "ps-1")
        self.assertEqual(start["turn_id"], "pt-1")
        self.assertEqual(start["request_id"], "child-1")
        self.assertEqual(start["parent_request_id"], "par-1")
        stop = _event_from_kwargs(
            "subagent_stop",
            {
                "parent_session_id": "ps-1",
                "parent_turn_id": "pt-1",
                "child_session_id": "cs-1",
                "child_role": "coder",
                "child_summary": "done",
                "child_status": "completed",
                "duration_ms": 1234,
            },
        )
        self.assertEqual(stop["session_id"], "ps-1")
        self.assertEqual(stop["turn_id"], "pt-1")
        self.assertEqual(stop["outcome"], "observed_success")
        # End to end: both live-shaped events are accepted, none invalid,
        # and the span is exported (reconciled incomplete at turn end —
        # the live stop carries no subagent id, so it cannot pair-close
        # the span opened by the start; see fix-rounds.md FR6 notes).
        self.ctx.hooks["subagent_start"](
            parent_session_id="ps-1", parent_turn_id="pt-1",
            parent_subagent_id="par-1", child_session_id="cs-1",
            child_subagent_id="child-1", child_role="coder",
            child_goal="fix bug",
        )
        self.ctx.hooks["subagent_stop"](
            parent_session_id="ps-1", parent_turn_id="pt-1",
            child_session_id="cs-1", child_role="coder",
            child_summary="done", child_status="completed",
            duration_ms=1234,
        )
        self._deliver_session_end(session="ps-1", turn="pt-1")
        diag = self.observer.diagnostics()
        self.assertEqual(diag["accepted"], 3, diag)
        self.assertEqual(diag["invalid_events"], 0, diag)
        subs = self._spans_named("subagent.spawn")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["attributes"]["session.id"], "ps-1")
        self.assertEqual(subs[0]["attributes"]["turn.id"], "pt-1")
        self.assertEqual(subs[0]["attributes"]["request.id"], "child-1")
        self.assertEqual(
            subs[0]["attributes"]["observation.completeness"], "incomplete"
        )

    # -- FR6-02: session boundaries without turn_id ------------------------
    def test_fr6_session_boundary_without_turn_id(self):
        # Live kwargs shape (lifecycle.py / cli_session_mixin.py):
        # session_id, platform, reason — no turn_id is sent.
        self.ctx.hooks["pre_tool_call"](
            session_id="s1", turn_id="t1", request_id="req-b-1", tool_name="Bash"
        )
        self.ctx.hooks["on_session_finalize"](
            session_id="s1", platform="darwin", reason="shutdown"
        )
        self.ctx.hooks["on_session_reset"](
            session_id="s1", platform="darwin", reason="reset"
        )
        self.assertTrue(self.observer.drain(timeout=5.0))
        diag = self.observer.diagnostics()
        self.assertEqual(diag["accepted"], 3, diag)
        self.assertEqual(diag["invalid_events"], 0, diag)
        # _end_session ran: the orphaned tool span was closed incomplete.
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(tools), 1)
        self.assertEqual(
            tools[0]["attributes"]["observation.completeness"], "incomplete"
        )

    # -- FR6-03: human-input bare outcome normalization --------------------
    def test_fr6_human_input_outcome_normalization(self):
        # Unit-level: exact live kwargs from human_input_hooks.py:72 —
        # a bare outcome kwarg, no status kwarg.
        cases = {
            "timeout": "timed_out",
            "error": "observed_failure",
            "cancelled": "cancelled",
            "provided": "observed_success",
        }
        for raw, expected in cases.items():
            evt = _event_from_kwargs(
                "on_human_input_resolved",
                {"request_id": "hr-1", "session_id": "s1", "outcome": raw},
            )
            self.assertEqual(evt["outcome"], expected, raw)
        # End to end: a timed-out approval closes with ERROR status.
        self.ctx.hooks["on_human_input_request"](
            session_id="s1", request_id="hr-1"
        )
        self.ctx.hooks["on_human_input_resolved"](
            session_id="s1", request_id="hr-1", outcome="timeout"
        )
        self._deliver_session_end(turn="human-input")
        approvals = self._spans_named("approval.wait")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["status"]["code"], 2)  # ERROR
        self.assertEqual(self.observer.diagnostics()["accepted"], 3)

    # -- FR6-04: tool error_type normalization ------------------------------
    def test_fr6_tool_error_type_normalization(self):
        # Unit-level: exact live kwargs from model_tools.py:704 —
        # error=None, error_type='tool_error' as a bare string.
        evt = _event_from_kwargs(
            "post_tool_call",
            {
                "session_id": "s1",
                "turn_id": "t1",
                "tool_call_id": "tc-1",
                "tool_name": "Bash",
                "status": "error",
                "error_type": "tool_error",
                "error_message": "exit 1",
                "error": None,
            },
        )
        self.assertEqual(evt["error_type"], "ToolError")
        self.assertEqual(evt["outcome"], "observed_failure")
        self.assertEqual(evt["request_id"], "tc-1")
        # End to end: the closed span carries error.type ToolError.
        self.ctx.hooks["pre_tool_call"](
            session_id="s1", turn_id="t1", tool_call_id="tc-1", tool_name="Bash"
        )
        self.ctx.hooks["post_tool_call"](
            session_id="s1", turn_id="t1", tool_call_id="tc-1",
            status="error", error_type="tool_error",
            error_message="exit 1", error=None,
        )
        self._deliver_session_end()
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"]["code"], 2)  # ERROR
        self.assertEqual(tools[0]["attributes"]["error.type"], "ToolError")

    # -- FR6-05: parallel tools keep distinct request_ids ------------------
    def test_fr6_parallel_tool_call_ids_stay_distinct(self):
        # Unit-level: same api_request_id, different tool_call_ids.
        shared = {
            "session_id": "s1",
            "turn_id": "t1",
            "api_request_id": "api-shared",
        }
        a = _event_from_kwargs(
            "pre_tool_call", {**shared, "tool_call_id": "tc-a", "tool_name": "Bash"}
        )
        b = _event_from_kwargs(
            "pre_tool_call", {**shared, "tool_call_id": "tc-b", "tool_name": "Read"}
        )
        self.assertEqual(a["request_id"], "tc-a")
        self.assertEqual(b["request_id"], "tc-b")
        # Non-tool events keep request_id first.
        api = _event_from_kwargs(
            "pre_api_request", {**shared, "request_id": "req-api-1"}
        )
        self.assertEqual(api["request_id"], "req-api-1")
        # End to end: two parallel tools sharing one api_request_id both
        # export as distinct spans — no dedup collapse.
        for call_id, tool in (("tc-a", "Bash"), ("tc-b", "Read")):
            self.ctx.hooks["pre_tool_call"](
                session_id="s1", turn_id="t1", tool_call_id=call_id,
                api_request_id="api-shared", tool_name=tool,
            )
        for call_id in ("tc-a", "tc-b"):
            self.ctx.hooks["post_tool_call"](
                session_id="s1", turn_id="t1", tool_call_id=call_id,
                api_request_id="api-shared", status="success",
            )
        self._deliver_session_end()
        diag = self.observer.diagnostics()
        self.assertEqual(diag["duplicates"], 0, diag)
        tools = self._spans_named("tool.exec")
        self.assertEqual(len(tools), 2)
        self.assertEqual(
            sorted(t["attributes"]["request.id"] for t in tools), ["tc-a", "tc-b"]
        )

    # -- FR6-06: callback exception trap ------------------------------------
    def test_fr6_callback_exception_trap(self):
        # An unhashable outcome value breaks the `in _OUTCOMES` check
        # inside the mapping step; the callback must swallow it and
        # return None instead of raising into the host.
        result = self.ctx.hooks["on_human_input_resolved"](
            session_id="s1", request_id="hr-trap-1",
            outcome=["not", "a", "string"],
        )
        self.assertIsNone(result)
        diag = self.observer.diagnostics()
        self.assertEqual(diag["accepted"], 0, diag)
        # Normal delivery still works afterwards.
        self.ctx.hooks["pre_tool_call"](
            session_id="s1", turn_id="t1", request_id="req-trap-1",
            tool_name="Bash",
        )
        self.assertEqual(self.observer.diagnostics()["accepted"], 1)


if __name__ == "__main__":
    unittest.main()
