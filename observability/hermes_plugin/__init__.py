"""Hermes observer plugin — Milestone 3 (thin adapter).

Wires the documented Hermes observer hooks to a HermesObserver (see
../hermes_observer.py, which holds all the logic and is Hermes-independent
and fully testable with synthetic hook events).

Contract (plan §5, documented v0.23.0 observer guide): the host calls
register(ctx); ctx.register_hook(name, callback) registers a read-only
observer callback for each lifecycle/tool event. Callbacks receive a
payload dict; this adapter forwards it to observer.handle_event, which
validates, allowlists and enqueues — it never raises and never performs
network I/O.

IMPORTANT: importing this module registers nothing. Only register(ctx)
wires hooks, and only when Hermes loads the plugin in a profile. Do NOT
enable in the active Hermes profile until the installed-build contract
test (Milestone 3 acceptance) passes on the dev machine: the exact
register_hook signatures and payload shapes are validated there, in a
disposable profile, never the active one.
"""

from __future__ import annotations

import os

# Documented hook names (plan §5). The observer maps each to a span
# lifecycle; unknown hook names are never registered.
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


def _event_from_payload(event_type, payload):
    """Adapt a hook payload to a schema-v1 event dict.

    Defensive: payloads may be dicts or objects with attributes; missing
    fields become empty strings and fail validation in the observer
    (counted, never raised).
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


def register(ctx):
    """Register the documented observer hooks on ctx. Returns the observer.

    Creates one HermesObserver (process-scoped provider + worker) and
    registers each hook name on ctx. The observer is owned by the plugin;
    the host calls observer.shutdown() at teardown — never a hook
    callback. The OTLP endpoint comes from
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (loopback default); no credentials
    are read or stored.
    """
    from hermes_observer import HermesObserver

    endpoint = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"
    )
    observer = HermesObserver(endpoint=endpoint)

    def _make_callback(event_type):
        def _callback(payload=None):
            observer.handle_event(_event_from_payload(event_type, payload or {}))
            return None

        return _callback

    for name in _HOOK_NAMES:
        ctx.register_hook(name, _make_callback(name))
    return observer
