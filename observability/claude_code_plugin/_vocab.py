"""Vocabulary for the Claude Code adapter (M7-B step 1).

Pure constants: the source-to-fleet span-type map and the output
attribute allowlist. Shared code is copied, not imported, so this module
does not depend on another package folder. Tests check it against the
AgentScope bridge's own allowlists (tests/test_claude_code_plugin.py).

Span shapes are the documented Claude Code names only. Real attribute
names are unconfirmed until the first real run.

M7-B step 3 (S2) adds read-only structures: ATTRIBUTE_SOURCES (per span type,
the output attribute rows and their source keys), ATTRIBUTE_KINDS, FOLD_TYPES
and FOLD_INPUT_KEYS (read by the status/fold logic, never emitted), the strip
lists STRIP_KEYS / STRIP_PREFIXES / STRIP_SUFFIXES with CONTENT_KEYS, the
is_stripped() helper, and the constants AGENT_NAME, OBSERVATION_SOURCE,
PRODUCER and REDACTED_PROMPT. Every Claude Code attribute name here is
documented Beta (source: code.claude.com/docs/en/monitoring-usage) and is
unconfirmed until the first live run.
"""

from __future__ import annotations

from types import MappingProxyType

# Documented Claude Code span types -> fleet v1 span names. Read-only: a
# read-only view, so assignment, deletion and update raise TypeError.
# tool.execution and tool.blocked_on_user are folded into tool.exec
# (decision f2), so they have no entry here.
SPAN_TYPE_TO_V1 = MappingProxyType({
    "claude_code.interaction": "agent.run",
    "claude_code.llm_request": "llm.call",
    "claude_code.tool": "tool.exec",
    "claude_code.hook": "hook.exec",
})

# Attribute keys the adapter may emit. Each one must also be in the
# AgentScope bridge's ATTRIBUTE_ALLOWLIST (enforced by a test). Anything
# else is dropped and counted. Token keys use the bridge's full names.
OUTPUT_ATTRIBUTE_KEYS = frozenset({
    "agent.name",
    "agent.role",
    "session.id",
    "turn.id",
    "observation.source",
    "observation.completeness",
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.tool.name",
    "gen_ai.tool.call.id",
    "error.type",
    "fleet.span",
    "fleet.trace",
})

# Per span type, the output attribute rows. Each row is
# (output_key, source_keys, kind): output_key is emitted only if a source key
# is present and passes the kind's check; source_keys is ordered (first
# present wins). Every row below is DOC-BETA, unconfirmed until live run.
# Read-only: a mapping proxy over tuples of tuples.
ATTRIBUTE_SOURCES = MappingProxyType({
    # DOC-BETA, unconfirmed until live run
    "claude_code.interaction": (
        ("session.id", ("session.id",), "uuid"),
        ("turn.id", ("interaction.sequence",), "seq"),
    ),
    # DOC-BETA, unconfirmed until live run
    "claude_code.llm_request": (
        ("session.id", ("session.id",), "uuid"),
        ("gen_ai.request.model", ("gen_ai.request.model", "model"), "model"),
        ("gen_ai.usage.input_tokens", ("input_tokens",), "count"),
        ("gen_ai.usage.output_tokens", ("output_tokens",), "count"),
        ("gen_ai.usage.cache_read.input_tokens", ("cache_read_tokens",), "count"),
        ("gen_ai.usage.cache_creation.input_tokens", ("cache_creation_tokens",), "count"),
    ),
    # DOC-BETA, unconfirmed until live run
    "claude_code.tool": (
        ("session.id", ("session.id",), "uuid"),
        ("gen_ai.tool.name", ("tool_name_safe", "tool_name"), "tool_name"),
        ("gen_ai.tool.call.id", ("gen_ai.tool.call.id", "tool_use_id"), "text"),
    ),
    # DOC-BETA, unconfirmed until live run
    "claude_code.hook": (
        ("session.id", ("session.id",), "uuid"),
    ),
})

# Kinds of value check applied to a source key (see _sanitize.py).
ATTRIBUTE_KINDS = frozenset({"uuid", "seq", "model", "count", "tool_name", "text"})

# Tool-level folds: these span types are merged into their parent claude_code.tool
# span (see the fold step). They are never emitted as spans of their own.
FOLD_TYPES = frozenset({
    "claude_code.tool.execution",
    "claude_code.tool.blocked_on_user",
})

# Source keys read only by the status and fold logic. Never emitted.
FOLD_INPUT_KEYS = frozenset({"success", "decision"})

# Keys never emitted. Identity, host, terminal, workflow, VCS and content
# keys. DOC-BETA, unconfirmed until live run.
STRIP_KEYS = frozenset({
    "user.email",
    "user.id",
    "user.account_uuid",
    "user.account_id",
    "organization.id",
    "ccr.session.id",
    "host.name",
    "host.id",
    "terminal.type",
    "user_prompt",
    "tool_input",
    "full_command",
    "file_path",
    "new_context",
    "bash_command",
    "bash_argv0",
    "hook_definitions",
    "error",
    "error_class",
})

# Key prefixes never emitted. DOC-BETA, unconfirmed until live run.
STRIP_PREFIXES = ("user.", "organization.", "host.", "vcs.", "workflow.")

# Key suffixes never emitted (truncation markers and original lengths).
# DOC-BETA, unconfirmed until live run.
STRIP_SUFFIXES = ("_truncated", "_original_length")

# Content keys: they carry prompts, commands, paths or hook bodies. Every
# member is also in STRIP_KEYS (a test enforces this).
CONTENT_KEYS = frozenset({
    "tool_input",
    "full_command",
    "file_path",
    "new_context",
    "bash_command",
    "hook_definitions",
})


def is_stripped(key):
    """True when key is on the strip lists. False for any non-str key."""
    if not isinstance(key, str):
        return False
    return (
        key in STRIP_KEYS
        or key.startswith(STRIP_PREFIXES)
        or key.endswith(STRIP_SUFFIXES)
    )


# Constants written onto every output span and checked by tests.
AGENT_NAME = "claude-code"
OBSERVATION_SOURCE = "otel-export"
PRODUCER = "claude-code"
# Documented default value of user_prompt. The D10 tripwire counts any other value.
REDACTED_PROMPT = "<REDACTED>"
