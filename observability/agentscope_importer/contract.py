"""M5 import contract — constants (fleet schema v1 -> AgentScope canonical 0.1).

See CONTRACT.md for the full definition. The values here are the
contract: change them only with a contract revision, never silently.
"""

from __future__ import annotations

# AgentScope canonical trace schema version (contracts/versions.py).
SCHEMA_VERSION = "0.1"

# Fleet schema version (M1/M3/M4 observers).
FLEET_SCHEMA_VERSION = "1"

# Feed / source identity carried through the import.
FEED_NAME = "fleet-tracing"

# The source block stamped on every import document.
SOURCE = {
    "adapter": "canonical",
    "framework": "fleet-tracing",
    "framework_version": FLEET_SCHEMA_VERSION,
    "capabilities": {
        "supports_spans": True,
        # D-F1 tree correction (ADR-005): the fleet graph is a span
        # hierarchy, not a framework topology, so the mapper emits no
        # `graph` section and this flag is False. Parentage stays
        # recorded on spans; AgentScope renders the run as a tree.
        "supports_graph_topology": False,
        "supports_node_execution_mapping": False,
        "supports_state_snapshots": False,
        "supports_checkpoints": False,
        "supports_interrupts": False,
        "supports_token_usage": True,
        "supports_cost": False,
    },
}

# Graph derivation label (D-F1 tree correction, ADR-005): recorded in the
# import document's run metadata and in the receipt provenance. It states
# that the graph is rendered as a tree from recorded parentage — not a
# framework topology. See CONTRACT.md §3 and §5.
GRAPH_DERIVATION = "tree-from-recorded-parentage"

# Fleet span name -> canonical SpanKind.
KIND_MAP = {
    "agent.run": "run",
    "llm.call": "llm",
    "tool.exec": "tool",
}

# Default kind for fleet span names with no explicit mapping
# (hook.exec, agent.handoff, state.write, ...). The original name is
# preserved in attributes["fleet.span"]; nothing is renamed away.
DEFAULT_KIND = "other"

# Attribute keys the fleet observers already strip (M3/M4 sanitizers).
# The mapper drops these again (defense in depth) and counts each drop
# in the receipt as "sanitized_dropped" — visible, never silent.
# NOTE: gen_ai.usage.* token keys are NOT in this set; tokens are
# carried, not stripped.
STRIPPED_KEYS = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.definitions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "github.copilot.tool.parameters.command",
        "github.copilot.tool.parameters.file_path",
        "github.copilot.tool.parameters.mcp_server_name",
        "copilot_chat.hook_input",
        "copilot_chat.hook_output",
        "copilot_chat.user_request",
        "user.name",
        "process.user.name",
        "host.name",
        "server.address",
        "authorization",
        "api_key",
    }
)

# Fleet-side bounds (inside the real importer's limits: 64 MiB upload,
# 100,000 spans per record, 100,000 runs).
MAX_SPANS_PER_IMPORT = 10_000
MAX_BYTES_PER_IMPORT = 16 * 1024 * 1024

# Retention policy (explicit; see CONTRACT.md §4).
MAX_IMPORTS = 100
MAX_AGE_DAYS = 30

# Receipt stats keys — exactly the real importer's 11 STATS_KEYS.
STATS_KEYS = (
    "runs",
    "spans",
    "node_executions",
    "events",
    "checkpoints",
    "payloads",
    "findings",
    "warnings",
    "dropped",
    "unmapped",
    "detection_skipped_lines",
)

# OTLP content types.
JSON_CONTENT_TYPE = "application/json"
PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
