"""Milestone 2 — verify the Claude Code controlled child trace.

A controlled non-interactive `claude -p` session must accept incoming
TRACEPARENT and emit spans under the coordinator's trace with the
handoff span as parent. This module is the harness that proves (or
disproves) that, and reports what actually happened.

It does NOT invent Claude Code telemetry behavior. It sets the plan's
candidate telemetry flags, records the installed version and the flags
used, launches the child with the coordinator's FIXED env allowlist,
and classifies the outcome from what the local receiver actually got:

  PARENTED   — a claude span arrived with traceId == coordinator
               trace_id AND parentSpanId == handoff span id.
  EXTERNAL   — claude spans arrived under a different trace id;
               correlated only by FLEET_* IDs (the fallback).
  PARENTED_TO_OTHER — claude spans under the coordinator trace but
               parented to a different span (context propagated, wrong
               parent). Reported as NOT handoff-parented.
  UNVERIFIED — no claude spans arrived at the receiver at all.
  SKIPPED    — `claude` binary not resolvable (PATH, FLEET_CLAUDE_PATH
               env override, or VS Code extension install).
  ERROR      — the child exited non-zero or timed out.

Native OTel export alone does NOT prove inbound W3C support: only the
traceId + parentSpanId match proves the child accepted the injected
context.

The live run CANNOT happen on a machine without Claude Code installed
and signed in. Here (no `claude` binary, no Anthropic auth) the CLI
exits 2 (skipped). On the dev machine the run is one command; see
README.md ("Milestone 2"). Tests prove the harness with a stub `claude`
stand-in.

Privacy: the child's raw stdout/stderr are never placed on a span.
Only bounded tails (500 chars) go in the result dict. The handoff span
carries only safe generic error metadata, as in Milestone 1.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from opentelemetry import propagate, trace
from opentelemetry.trace import (
    SpanKind,
    Status,
    StatusCode,
    format_span_id,
    format_trace_id,
)

from fleet_trace import (
    build_child_env,
    init_tracing,
    new_intent_ids,
    sanitize_attributes,
)
from receiver import LocalReceiver

# Candidate Claude Code telemetry flags, named by the plan. They are
# CANDIDATE flags: spelling/values are not verified against any installed
# Claude Code build. The harness sets them, records them in the result,
# and reports the installed version — it does not claim they work.
CLAUDE_TELEMETRY_FLAGS = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    # Steer the child toward OTLP/HTTP JSON so the stdlib-only local
    # receiver can read it. If the child exports protobuf anyway, the
    # receiver records the attempt as rejected (see receiver.py) and the
    # outcome is UNVERIFIED, not silently parented.
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
}

DEFAULT_PROMPT = "Reply with exactly: OK"
CLAUDE_SERVICE_NAME = "claude-code"
_STDOUT_TAIL_CHARS = 500

# Fix Round 1 (M2-P1-01): the `claude` binary is not on PATH on the dev
# machine; it ships inside the VS Code extension. Resolution order:
#   1. FLEET_CLAUDE_PATH env override (explicit absolute path)
#   2. `claude` on PATH
#   3. VS Code extension install under ~/.vscode/extensions (newest first)
CLAUDE_PATH_ENV_VAR = "FLEET_CLAUDE_PATH"


def resolve_claude_path():
    """Find the `claude` binary. Returns the path string or None."""
    override = os.environ.get(CLAUDE_PATH_ENV_VAR, "").strip()
    if override and os.path.isfile(override):
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    pattern = str(
        Path.home()
        / ".vscode"
        / "extensions"
        / "anthropic.claude-code-*-win32-arm64"
        / "resources"
        / "native-binary"
        / "claude.exe"
    )
    candidates = sorted(glob.glob(pattern))
    if candidates:
        return candidates[-1]
    return None


def claude_available():
    """Check for a `claude` binary and its version.

    Returns {"installed", "path", "version", "version_error"}.
    A missing binary is a skip, not a crash. A binary whose --version
    fails is still reported as installed with version None.
    """
    path = resolve_claude_path()
    if not path:
        return {
            "installed": False,
            "path": None,
            "version": None,
            "version_error": None,
        }
    try:
        proc = subprocess.run(
            [path, "--version"],
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "installed": True,
            "path": path,
            "version": None,
            "version_error": "version_check_failed",
        }
    version = None
    if proc.stdout and proc.stdout.strip():
        version = proc.stdout.strip().splitlines()[0][:200]
    if proc.returncode != 0:
        return {
            "installed": True,
            "path": path,
            "version": version,
            "version_error": f"version_exit_{proc.returncode}",
        }
    return {"installed": True, "path": path, "version": version, "version_error": None}


def build_claude_child_env(carrier, task_id, session_id, handoff_ref, endpoint,
                           service_name=CLAUDE_SERVICE_NAME):
    """Coordinator allowlist env + Claude Code telemetry flags.

    Reuses fleet_trace.build_child_env (never os.environ.copy()), then
    sets the service name and the plan's candidate telemetry flags.
    The OTLP endpoint is passed explicitly (M1 lesson: relying on the
    parent process's env would silently leave the child with no exporter).
    """
    env = build_child_env(carrier, task_id, session_id, handoff_ref, endpoint=endpoint)
    env["OTEL_SERVICE_NAME"] = service_name
    env.update(CLAUDE_TELEMETRY_FLAGS)
    return env


def launch_claude(prompt, child_env, timeout=120, extra_args=None, claude_path=None):
    """Run `claude -p <prompt>` with the given env. Returns a report dict.

    Raw output is bounded to a 500-char tail and never placed on a span.
    Non-zero exit / timeout / spawn failure -> ok False with a bounded
    error enum (ChildProcessFailure or Timeout). The binary is the
    resolved path when given, else resolve_claude_path(), else "claude".
    """
    binary = claude_path or resolve_claude_path() or "claude"
    cmd = [binary, "-p", prompt] + list(extra_args or [])
    try:
        proc = subprocess.run(
            cmd, env=child_env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "Timeout",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    except OSError:
        return {
            "ok": False,
            "error": "ChildProcessFailure",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    return {
        "ok": proc.returncode == 0,
        "error": None if proc.returncode == 0 else "ChildProcessFailure",
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-_STDOUT_TAIL_CHARS:],
        "stderr_tail": (proc.stderr or "")[-_STDOUT_TAIL_CHARS:],
    }


def _is_claude_span(span):
    """A span is a Claude Code span if its service/scope/name says so."""
    resource = span.get("resource") or {}
    scope = span.get("scope") or {}
    name = span.get("name") or ""
    return (
        resource.get("service.name") == CLAUDE_SERVICE_NAME
        or "claude" in (scope.get("name") or "").lower()
        or name.startswith("claude_code")
    )


def detect_parenting(spans, trace_id, handoff_span_id):
    """Classify claude spans against the coordinator's trace/handoff.

    Returns a dict with classification in
    {"parented", "external", "parented_to_other", "unverified"} plus
    counts and the child trace ids seen. Never claims parenting without
    the traceId + parentSpanId match.
    """
    claude_spans = [s for s in spans if _is_claude_span(s)]
    child_trace_ids = sorted({s.get("traceId") for s in claude_spans if s.get("traceId")})
    parent_span_ids = sorted(
        {s.get("parentSpanId") for s in claude_spans if s.get("parentSpanId")}
    )
    base = {
        "claude_span_count": len(claude_spans),
        "child_trace_ids": child_trace_ids,
        "child_parent_span_ids": parent_span_ids,
    }
    if not claude_spans:
        return {**base, "classification": "unverified", "reason": "no_claude_spans_received"}
    parented = [
        s
        for s in claude_spans
        if s.get("traceId") == trace_id and s.get("parentSpanId") == handoff_span_id
    ]
    if parented:
        return {
            **base,
            "classification": "parented",
            "parented_span_count": len(parented),
        }
    same_trace = [s for s in claude_spans if s.get("traceId") == trace_id]
    if same_trace:
        return {
            **base,
            "classification": "parented_to_other",
            "reason": "claude spans share the coordinator trace but are parented to a different span",
            "same_trace_span_count": len(same_trace),
        }
    return {
        **base,
        "classification": "external",
        "reason": "claude spans arrived under a different trace id; correlated only by FLEET_* IDs",
    }


def wait_for_claude_spans(receiver, timeout=10.0, poll_interval=0.25):
    """Poll the receiver until a claude span arrives (or timeout).

    Returns the claude spans seen. Once the first span arrives, one
    extra 0.5s settle pass collects the rest of the batch.
    """
    deadline = time.monotonic() + timeout
    while True:
        spans = [s for s in receiver.spans if _is_claude_span(s)]
        if spans:
            time.sleep(0.5)
            return [s for s in receiver.spans if _is_claude_span(s)]
        if time.monotonic() >= deadline:
            return spans
        time.sleep(poll_interval)


def run_claude_intent(prompt, endpoint=None, receiver=None,
                      service_name="fleet-claude-coordinator", timeout=120,
                      claude_extra_args=None, span_wait_timeout=10.0):
    """Run one intent: root agent.run + handoff to a controlled `claude -p`.

    If neither endpoint nor receiver is given, an in-process
    LocalReceiver is started on an ephemeral port and stopped at the end.
    Returns a structured, JSON-serializable result dict.
    """
    info = claude_available()
    result = {
        "claude_installed": info["installed"],
        "claude_path": info["path"],
        "claude_version": info["version"],
        "claude_version_error": info["version_error"],
        "flags": dict(CLAUDE_TELEMETRY_FLAGS),
        "prompt": prompt,
    }
    if not info["installed"]:
        result.update(
            {
                "status": "skipped",
                "reason": "claude_not_installed",
                "message": (
                    "claude CLI not found on PATH. Install Claude Code and "
                    "sign in, then re-run. Nothing was launched."
                ),
            }
        )
        return result

    own_receiver = None
    if receiver is None and endpoint is None:
        own_receiver = LocalReceiver().start()
        receiver = own_receiver
        endpoint = receiver.endpoint
    elif receiver is not None:
        endpoint = receiver.endpoint

    provider, tracer = init_tracing(endpoint, service_name)
    task_id, session_id = new_intent_ids()
    result.update({"task_id": task_id, "session_id": session_id, "endpoint": endpoint})

    with tracer.start_as_current_span(
        "agent.run",
        kind=SpanKind.INTERNAL,
        attributes=sanitize_attributes(
            {
                "agent.name": "fleet-claude-coordinator",
                "agent.role": "coordinator",
                "session.id": session_id,
                "task.id": task_id,
            }
        ),
    ) as root_span:
        root_trace_id = format_trace_id(root_span.get_span_context().trace_id)
        result["trace_id"] = root_trace_id
        handoff_ref = f"payload:claude-job-{task_id[:8]}"
        with tracer.start_as_current_span(
            "agent.handoff",
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(
                {
                    "agent.name": "fleet-claude-coordinator",
                    "agent.from": "fleet-coordinator",
                    "agent.to": "claude-code",
                    "session.id": session_id,
                    "task.id": task_id,
                    "handoff.reason": "delegate-claude-work",
                    "handoff.payload_ref": handoff_ref,
                }
            ),
        ) as handoff_span:
            handoff_span_id = format_span_id(handoff_span.get_span_context().span_id)
            result["handoff_span_id"] = handoff_span_id
            # inject() WHILE the handoff span is current: the child
            # receives THIS span's context as its parent.
            carrier = {}
            propagate.inject(carrier)
            child_env = build_claude_child_env(
                carrier, task_id, session_id, handoff_ref, endpoint
            )
            launch = launch_claude(
                prompt, child_env, timeout=timeout, extra_args=claude_extra_args,
                claude_path=info["path"],
            )
            result["child_returncode"] = launch.get("returncode")
            result["child_stdout_tail"] = launch.get("stdout_tail")
            result["child_stderr_tail"] = launch.get("stderr_tail")
            if not launch.get("ok"):
                handoff_span.set_attribute(
                    "handoff.child_error", launch.get("error", "ChildProcessFailure")
                )
                handoff_span.set_status(
                    Status(StatusCode.ERROR, "Process failed; details withheld")
                )
                result.update(
                    {
                        "status": "error",
                        "error": launch.get("error", "ChildProcessFailure"),
                        "child_parented": False,
                    }
                )
            else:
                if receiver is not None:
                    wait_for_claude_spans(receiver, timeout=span_wait_timeout)
                    detection = detect_parenting(
                        receiver.spans, root_trace_id, handoff_span_id
                    )
                    result["rejected_payloads"] = list(getattr(receiver, "rejected", []))
                else:
                    detection = {
                        "classification": "unverified",
                        "reason": "no_receiver_attached",
                        "claude_span_count": 0,
                        "child_trace_ids": [],
                        "child_parent_span_ids": [],
                    }
                result["detection"] = detection
                parented = detection["classification"] == "parented"
                result["child_parented"] = parented
                result["child_trace_ids"] = detection.get("child_trace_ids")
                handoff_span.set_attribute(
                    "handoff.child_parented", str(parented).lower()
                )
                if parented:
                    result["status"] = "parented"
                elif detection["classification"] in ("external", "parented_to_other"):
                    result["status"] = "external"
                else:
                    result["status"] = "unverified"

    result["flush_ok"] = bool(provider.force_flush(timeout_millis=5000))
    provider.shutdown()
    if own_receiver is not None:
        own_receiver.stop()
    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Milestone 2: verify the Claude Code controlled child trace"
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt for the controlled `claude -p` run (default: %(default)s)",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="OTLP endpoint to use instead of the in-process local receiver",
    )
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--service-name", default="fleet-claude-coordinator")
    args = parser.parse_args()

    result = run_claude_intent(
        args.prompt,
        endpoint=args.endpoint,
        service_name=args.service_name,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2, default=str))
    # Exit code is the verification outcome: 0 = PARENTED (M2 verified),
    # 1 = not parented / error / unverified, 2 = skipped (no claude CLI).
    if result.get("status") == "parented":
        return 0
    if result.get("status") == "skipped":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
