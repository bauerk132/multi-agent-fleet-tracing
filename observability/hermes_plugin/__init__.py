"""Hermes observer plugin — Milestone 3 (thin adapter).

Wires the documented Hermes observer hooks to a HermesObserver (see
observer.py in this package, which holds all the logic and is
Hermes-independent and fully testable with synthetic hook events).

Self-contained (Fix Round 5, FR5-01): this package vendors everything
the observer needs (observer.py, _sanitize.py, _otlp_json.py) with
relative imports only, so ``hermes plugins doctor`` can load it from a
staged copy of ONLY the hermes_plugin directory — the parent
observability/ package is never on sys.path there.

Contract (plan §5, documented v0.23.0 observer guide): the host calls
register(ctx); ctx.register_hook(name, callback) registers a read-only
observer callback for each lifecycle/tool event. Callbacks receive
keyword arguments from the Hermes runtime (Fix Round 5, FR5-03/FR5-04:
``def _callback(**kwargs)``); this adapter maps those kwargs onto
schema-v1 events and forwards them to observer.handle_event, which
validates, allowlists and enqueues — it never raises and never performs
network I/O. A ``payload`` dict/object kwarg is still accepted as a
fallback source (FR5-04 keeps _event_from_payload working).

OpenTelemetry guard (Fix Round 5, FR5-02): importing this package never
crashes the host — only stdlib imports happen at import time. The OTel
dependency is declared in plugin.yaml (python_dependencies, pinned
1.45.1, same as observability/requirements.txt). If OTel is absent at
register() time, register() raises a clear RuntimeError naming the
missing dependency instead of leaking an ImportError traceback into
the host.

IMPORTANT: importing this module registers nothing. Only register(ctx)
wires hooks, and only when Hermes loads the plugin in a profile. Do NOT
enable in the active Hermes profile until the installed-build contract
test (Milestone 3 acceptance) passes on the dev machine: the exact
register_hook signatures and payload shapes are validated there, in a
disposable profile, never the active one.
"""

from __future__ import annotations

import os
import uuid

# Documented hook names (plan §5). The observer maps each to a span
# lifecycle; unknown hook names are never registered. Also declared as
# provides_hooks in plugin.yaml (Fix Round 5, FR5-05) — keep in sync.
_HOOK_NAMES = (
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_tool_call",
    "post_tool_call",
    "subagent_start",
    "subagent_stop",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "on_human_input_request",
    "on_human_input_resolved",
)

# Bounded schema-v1 vocabularies (mirrors observer.OUTCOMES /
# observer.ERROR_TYPES; duplicated here so this module stays importable
# without OpenTelemetry — see FR5-02).
_OUTCOMES = frozenset(
    {"observed_success", "observed_failure", "cancelled", "timed_out", "incomplete", "unknown"}
)
_ERROR_TYPES = frozenset(
    {"LLMError", "ToolError", "Timeout", "Cancelled", "Unknown"}
)

# Hermes status values -> schema-v1 outcomes (FR5-04). Unknown statuses
# map to "unknown" (the observer enforces the bound the same way).
_STATUS_TO_OUTCOME = {
    "success": "observed_success",
    "succeeded": "observed_success",
    "ok": "observed_success",
    "completed": "observed_success",
    "complete": "observed_success",
    "error": "observed_failure",
    "failed": "observed_failure",
    "failure": "observed_failure",
    "fail": "observed_failure",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "cancel": "cancelled",
    "timeout": "timed_out",
    "timed_out": "timed_out",
    "timedout": "timed_out",
    "incomplete": "incomplete",
    # Fix Round 6 (FR6-03): on_human_input_resolved reports a bare
    # outcome kwarg; 'provided' means the human answered.
    "provided": "observed_success",
}


def _map_status_to_outcome(status):
    """Map a Hermes status value onto the bounded OUTCOMES enum."""
    if status is None:
        return None
    if isinstance(status, bool):
        return "observed_success" if status else "observed_failure"
    key = str(status).strip().lower().replace(" ", "_").replace("-", "_")
    return _STATUS_TO_OUTCOME.get(key, "unknown")


def _map_error_type(error):
    """Extract a bounded error type from a Hermes error value.

    Accepts an error dict (``type``/``kind``/``name``/``code`` keys), an
    exception instance, or a plain string. Unknown shapes map to
    "Unknown" — raw error text is never emitted.
    """
    if error is None:
        return None
    if isinstance(error, dict):
        raw = (
            error.get("type")
            or error.get("kind")
            or error.get("name")
            or error.get("code")
            or ""
        )
    elif isinstance(error, BaseException):
        raw = type(error).__name__
    else:
        raw = str(error)
    raw = str(raw).strip()
    if raw in _ERROR_TYPES:
        return raw
    low = raw.lower().replace("_", "").replace(" ", "")
    if "timeout" in low or "deadlineexceeded" in low:
        return "Timeout"
    if "cancel" in low:
        return "Cancelled"
    if "tool" in low:
        return "ToolError"
    if "llm" in low or "model" in low or "api" in low or "provider" in low:
        return "LLMError"
    return "Unknown"


def _pick(source, keys, default=""):
    """First present, non-empty value for any of ``keys`` in a dict/object."""
    for key in keys:
        if isinstance(source, dict):
            value = source.get(key)
        else:
            value = getattr(source, key, None)
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        return value
    return default


def _event_from_payload(event_type, payload):
    """Adapt a hook payload to a schema-v1 event dict.

    Defensive: payloads may be dicts or objects with attributes; missing
    fields become empty strings and fail validation in the observer
    (counted, never raised). Kept for dict/object payload delivery;
    **kwargs are now the primary path (see _event_from_kwargs).
    """
    def _get(key, default=""):
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    return {
        "event_id": str(_get("event_id", _get("id", ""))),
        "event_type": event_type,
        "session_id": str(_get("session_id", "")),
        "turn_id": str(_get("turn_id", _get("turn", ""))),
        "request_id": str(_get("request_id", _get("api_request_id", _get("tool_call_id", "")))),
        "parent_request_id": str(_get("parent_request_id", "")),
        "attempt": _get("attempt", 1),
        "outcome": _get("outcome", None),
        "error_type": _get("error_type", None),
        "model": _get("model", None),
        "tool_name": _get("tool_name", _get("tool", None)),
    }


def _event_from_kwargs(event_type, kwargs):
    """Map Hermes runtime kwargs to a schema-v1 event dict (FR5-04).

    Primary path: the host calls each hook callback with keyword
    arguments. A ``payload`` kwarg holding a dict/object is used as a
    fallback source for any field the kwargs do not carry (kwargs win).

    Mappings (Fix Round 6: exact live Hermes v0.23.0 kwargs per
    Antigravity handoff hof_01M6VANB718PNZVHNP):
    - status -> outcome (via _map_status_to_outcome); child_status
      (subagent_stop) is checked next; a bare ``outcome`` kwarg already
      in the bounded enum is passed through, otherwise it is normalized
      through the status map (FR6-03: 'timeout' -> 'timed_out',
      'error' -> 'observed_failure', 'provided' -> 'observed_success');
      a ``success`` bool maps to observed_success / observed_failure.
    - error dict -> error_type (via _map_error_type); a bare
      ``error_type`` kwarg is normalized the same way (FR6-04:
      'tool_error' -> 'ToolError'), even when ``error`` is None.
    - subagent coordinates (FR6-01): parent_session_id /
      child_session_id -> session_id, parent_turn_id -> turn_id,
      child_subagent_id -> request_id, parent_subagent_id ->
      parent_request_id.
    - request_id precedence (FR6-05): for pre/post_tool_call
      tool_call_id wins over api_request_id (parallel tools share one
      api_request_id); other events keep request_id first.
    - model_name -> model; tool / tool_call_name -> tool_name.
    - event_id defaults to a fresh uuid4 hex when nothing identifies
      the delivery; request_id falls back to the event_id so open/close
      pairing still has a key.
    """
    kwargs = kwargs or {}
    payload = kwargs.get("payload")
    if payload is not None:
        base = _event_from_payload(event_type, payload)
    else:
        base = {
            "event_id": "",
            "event_type": event_type,
            "session_id": "",
            "turn_id": "",
            "request_id": "",
            "parent_request_id": "",
            "attempt": 1,
            "outcome": None,
            "error_type": None,
            "model": None,
            "tool_name": None,
        }

    event_id = str(
        _pick(kwargs, ("event_id", "id"), base["event_id"] or uuid.uuid4().hex)
    )
    session_id = str(
        _pick(
            kwargs,
            (
                "parent_session_id",
                "session_id",
                "session",
                "child_session_id",
                "subagent_session_id",
                "agent_session_id",
            ),
            base["session_id"],
        )
    )
    turn_id = str(
        _pick(
            kwargs,
            ("parent_turn_id", "turn_id", "turn", "subagent_turn_id"),
            base["turn_id"],
        )
    )
    # Fix Round 6 (FR6-05): parallel tool calls share one api_request_id,
    # so for pre/post_tool_call tool_call_id must win or every parallel
    # tool collapses onto one dedup key and all but the first are dropped
    # as duplicates. Other events keep request_id first; subagent ids
    # (FR6-01) follow api_request_id there.
    if event_type in ("pre_tool_call", "post_tool_call"):
        request_id = str(
            _pick(
                kwargs,
                ("tool_call_id", "request_id", "call_id", "api_request_id"),
                base["request_id"] or event_id,
            )
        )
    else:
        request_id = str(
            _pick(
                kwargs,
                (
                    "request_id",
                    "api_request_id",
                    "child_subagent_id",
                    "subagent_id",
                    "call_id",
                ),
                base["request_id"] or event_id,
            )
        )
    parent_request_id = str(
        _pick(
            kwargs,
            (
                "parent_subagent_id",
                "parent_request_id",
                "parent_id",
                "parent_tool_call_id",
                "parent_request",
            ),
            base["parent_request_id"],
        )
    )
    attempt = kwargs.get("attempt", base["attempt"])
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 1

    # outcome: status wins, then child_status (subagent_stop, FR6-01),
    # then a bare outcome kwarg, then success bool.
    outcome = None
    if "status" in kwargs and kwargs["status"] is not None:
        outcome = _map_status_to_outcome(kwargs["status"])
    elif kwargs.get("child_status") is not None:
        outcome = _map_status_to_outcome(kwargs["child_status"])
    elif kwargs.get("outcome") in _OUTCOMES:
        outcome = kwargs["outcome"]
    elif kwargs.get("outcome") is not None:
        # Fix Round 6 (FR6-03): a bare outcome kwarg outside the bounded
        # enum (e.g. on_human_input_resolved 'timeout' / 'error' /
        # 'provided') is normalized through the status map instead of
        # degrading to 'unknown'.
        outcome = _map_status_to_outcome(kwargs["outcome"])
    elif isinstance(kwargs.get("success"), bool):
        outcome = "observed_success" if kwargs["success"] else "observed_failure"
    else:
        outcome = base["outcome"]

    # error_type: an error value wins, then a bare error_type kwarg.
    error_type = None
    if kwargs.get("error") is not None:
        error_type = _map_error_type(kwargs["error"])
    elif kwargs.get("error_type") is not None:
        # Fix Round 6 (FR6-04): a bare error_type kwarg (e.g.
        # post_tool_call 'tool_error' with error=None) is normalized
        # through the taxonomy instead of passing the raw string through
        # to validation, where it would degrade to 'Unknown'.
        error_type = _map_error_type(kwargs["error_type"])
    else:
        error_type = base["error_type"]

    model = _pick(kwargs, ("model", "model_name"), base["model"] or "")
    model = model if model != "" else None
    tool_name = _pick(kwargs, ("tool_name", "tool_call_name"), "")
    if tool_name == "":
        tool = kwargs.get("tool", base["tool_name"])
        if isinstance(tool, dict):
            tool_name = tool.get("name") or tool.get("tool_name") or ""
        elif tool is not None:
            tool_name = str(tool)
        else:
            tool_name = ""
    tool_name = tool_name if tool_name != "" else None

    return {
        "event_id": event_id,
        "event_type": event_type,
        "session_id": session_id,
        "turn_id": turn_id,
        "request_id": request_id,
        "parent_request_id": parent_request_id,
        "attempt": attempt,
        "outcome": outcome,
        "error_type": error_type,
        "model": model,
        "tool_name": tool_name,
    }


def register(ctx):
    """Register the documented observer hooks on ctx. Returns the observer.

    Creates one HermesObserver (process-scoped provider + worker) and
    registers each hook name on ctx. The observer is owned by the plugin;
    the host calls observer.shutdown() at teardown — never a hook
    callback. The OTLP endpoint comes from
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (loopback default); no credentials
    are read or stored.

    If OpenTelemetry is not installed, raises RuntimeError with a clear
    message naming the missing python_dependencies (FR5-02) instead of
    leaking an ImportError traceback into the host. Any other import
    failure is re-raised unchanged.
    """
    try:
        from .observer import HermesObserver
    except ImportError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing.startswith("opentelemetry") or "opentelemetry" in str(exc):
            raise RuntimeError(
                "fleet-trace-observer: OpenTelemetry is not installed; "
                "install the plugin's python_dependencies "
                "(opentelemetry-api==1.45.1, opentelemetry-sdk==1.45.1, "
                "opentelemetry-exporter-otlp-proto-http==1.45.1) and retry."
            ) from None
        raise

    endpoint = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"
    )
    observer = HermesObserver(endpoint=endpoint)

    def _make_callback(event_type):
        def _callback(**kwargs):
            # Fix Round 6 (FR6-06): nothing from the observer may escape
            # into the Hermes host runtime. handle_event never raises,
            # and this trap covers the kwargs -> event mapping step as
            # well. A mapping failure is swallowed, never raised.
            try:
                observer.handle_event(_event_from_kwargs(event_type, kwargs))
            except Exception:
                pass
            return None

        return _callback

    for name in _HOOK_NAMES:
        ctx.register_hook(name, _make_callback(name))
    return observer
