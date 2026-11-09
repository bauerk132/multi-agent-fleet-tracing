"""Span-attribute sanitizer — vendored copy (Milestone 4).

Same sanitize logic as observability/fleet_trace.py
(``SAFE_ATTRIBUTE_KEYS``, ``SAFE_ERROR_TYPES``, ``SAFE_ERROR_MESSAGES``,
``MAX_ATTRIBUTE_CHARS``, ``sanitize_attributes``), kept inside
copilot_plugin so the package is self-contained when staged alone
(relative imports only; the parent observability/ package is never on
sys.path there).

The key set is the M3 set PLUS the Copilot-safe keys (``copilot.*``,
``gen_ai.response.model``, extra ``gen_ai.usage.*`` token counters).
The same rule holds: only opaque IDs, bounded enums, and numeric
metadata survive. Content-capture attributes (prompts, responses,
tool arguments/results, file paths, shell commands, hook payloads),
identity attributes (user.name, process.user.name, host.name), and
identifying repo attributes (repository URL, GitHub org) are NOT in
the allowlist and are dropped before emission — even if Copilot was
misconfigured to emit them upstream. If the vendored sources change,
copy them here again — do not import across the package boundary.
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
        # M7-B real exporter: operation provenance for model-suffixed
        # renames ("chat <model>", "embeddings <model>" -> llm.call).
        # A bounded operation enum (chat/embeddings/...) with no content
        # fields; without it a chat call and an embeddings call would be
        # indistinguishable once both map to llm.call.
        "gen_ai.operation.name",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.reasoning.output_tokens",
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
        # Milestone 4 (Copilot observer): Copilot-native metadata. All
        # are opaque IDs, bounded enums, or numeric metadata — no
        # content fields. Deliberately ABSENT: repository URL
        # (github.copilot.git.repository), GitHub org
        # (github.copilot.github.org), server.address (hostname),
        # content-capture attributes, and identity attributes.
        "copilot.agent.type",
        "copilot.turn_count",
        "copilot.time_to_first_token_ms",
        "copilot.tool.type",
        "copilot.mcp_server_name_hash",
        "copilot.mcp_tool_name",
        "copilot.skill_name",
        "copilot.edit_type",
        "copilot.hook.decision",
        "copilot.hook.duration_s",
        "copilot.hook.type",
        "copilot.hook.result",
        "copilot.git.branch",
        "copilot.git.commit_sha",
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
