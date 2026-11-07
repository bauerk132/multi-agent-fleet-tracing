"""Span-attribute sanitizer — vendored copy (Fix Round 5, FR5-01).

Verbatim copy of the sanitize logic from observability/fleet_trace.py
(``SAFE_ATTRIBUTE_KEYS``, ``SAFE_ERROR_TYPES``, ``SAFE_ERROR_MESSAGES``,
``MAX_ATTRIBUTE_CHARS``, ``sanitize_attributes``), kept inside
hermes_plugin so the plugin is self-contained when staged alone
(``hermes plugins doctor`` loads ONLY the hermes_plugin directory; the
parent observability/ package is not on sys.path there, so the top-level
``from fleet_trace import ...`` in hermes_observer.py could not resolve).

Only the sanitize half of fleet_trace is vendored — the coordinator half
(child spawning, signal handlers, intent running) is not needed by the
observer and stays out of the plugin. If fleet_trace.py changes the
sanitize logic, copy it here again — do not import across the package
boundary.
"""

from __future__ import annotations

import re

# Attribute keys that are ever allowed on a span. Everything else is dropped.
SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "agent.name",
        "agent.role",
        "agent.from",
        "agent.to",
        "session.id",
        "task.id",
        "handoff.reason",
        "handoff.payload_ref",
        "handoff.child_parented",
        "handoff.child_error",
        "state.key",
        "state.before_ref",
        "state.after_ref",
        "error.type",
        "error.message",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.tool.name",
        "retrieval.query_ref",
        "approval.kind",
        "synthetic",
        # Milestone 3 (Hermes observer, schema v1): turn/request correlation,
        # observation provenance/completeness, and retry attempt. All are
        # opaque IDs or bounded enums — no content fields.
        "turn.id",
        "request.id",
        "observation.source",
        "observation.completeness",
        "hermes.attempt",
    }
)

# Bounded safe enums for error metadata. No exception str(e), no tracebacks.
SAFE_ERROR_TYPES = frozenset({"ToolError", "ChildProcessFailure", "LLMError", "Timeout", "Cancelled", "Unknown"})
SAFE_ERROR_MESSAGES = frozenset(
    {
        "Tool failed; details withheld",
        "Process failed; details withheld",
        "LLM call failed; details withheld",
        "Error; details withheld",
    }
)

# Fix Round 1 (G2-P2-02): dynamic string attributes are truncated to
# this many characters so a long value cannot balloon telemetry buffers.
MAX_ATTRIBUTE_CHARS = 256

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def sanitize_attributes(attrs):
    """Allowlist filter for span attributes. Drops anything unsafe.

    - Only keys in SAFE_ATTRIBUTE_KEYS survive.
    - Keys containing secret-ish parts are dropped even if allowlisted.
    - error.message is restricted to SAFE_ERROR_MESSAGES.
    - String values that look like an email or a bearer-style token are
      replaced with [REDACTED].
    """
    clean = {}
    for key, value in (attrs or {}).items():
        # The explicit allowlist IS the control: only enumerated keys
        # survive. (A substring check on key names would false-positive on
        # legitimate keys such as "state.key", so it is deliberately not
        # used here; secret-looking *values* are redacted below instead.)
        if key not in SAFE_ATTRIBUTE_KEYS:
            continue
        if key == "error.type" and value not in SAFE_ERROR_TYPES:
            continue
        if key == "error.message" and value not in SAFE_ERROR_MESSAGES:
            value = "Error; details withheld"
        if isinstance(value, str):
            # Fix Round 1 (G2-P2-02): bound dynamic string attributes.
            if len(value) > MAX_ATTRIBUTE_CHARS:
                value = value[:MAX_ATTRIBUTE_CHARS]
            if _EMAIL_RE.search(value) or value.startswith(("sk-", "Bearer ")):
                value = "[REDACTED]"
        clean[key] = value
    return clean
