"""Vocabulary for the Claude Code adapter (M7-B step 1).

Pure constants: the source-to-fleet span-type map and the output
attribute allowlist. Shared code is copied, not imported, so this module
does not depend on another package folder. Tests check it against the
AgentScope bridge's own allowlists (tests/test_claude_code_plugin.py).

Span shapes are the documented Claude Code names only. Real attribute
names are unconfirmed until the first real run.
"""

from __future__ import annotations

# Documented Claude Code span types -> fleet v1 span names.
# tool.execution and tool.blocked_on_user are folded into tool.exec
# (decision f2), so they have no entry here.
SPAN_TYPE_TO_V1 = {
    "claude_code.interaction": "agent.run",
    "claude_code.llm_request": "llm.call",
    "claude_code.tool": "tool.exec",
    "claude_code.hook": "hook.exec",
}

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
