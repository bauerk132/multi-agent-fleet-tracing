"""Sanitization helpers for the Claude Code plugin (M7-B step 3, S1).

The regexes, the text rule and the error enums below are COPIED BY VALUE from
observability/agentscope_importer/bridge.py (EMAIL_RE, SECRET_VALUE_RE,
ABSOLUTE_PATH_RE, SAFE_METADATA_RE, MAX_SAFE_STRING_CHARS and the _safe_text
rules, non-relative-branch variant) and from copilot_plugin/_sanitize.py
(SAFE_ERROR_TYPES, SAFE_ERROR_MESSAGES, which the bridge imports). Do not
import agentscope_importer, copilot_plugin or hermes_plugin from here: the
copies are guarded by drift tests in observability/tests/test_claude_code_plugin.py
that compare every value against the bridge.

Every public function is pure and never raises; any input that cannot be
judged returns False (or None for safe_count).
"""
from __future__ import annotations

import math
import re

# Copied from agentscope_importer/bridge.py (line 43).
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Copied from agentscope_importer/bridge.py (line 44).
SAFE_METADATA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}\Z")
# Copied from agentscope_importer/bridge.py (lines 46-54).
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}"
    r"|glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})\b"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
# Copied from agentscope_importer/bridge.py (line 55).
ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2})")
# Copied from agentscope_importer/bridge.py (line 38).
MAX_SAFE_STRING_CHARS = 256

# Copied by value from copilot_plugin/_sanitize.py (lines 94-102), which the
# bridge imports as its own SAFE_ERROR_TYPES / SAFE_ERROR_MESSAGES.
SAFE_ERROR_TYPES = frozenset({"ToolError", "ChildProcessFailure", "LLMError", "Timeout", "Cancelled", "Unknown"})
SAFE_ERROR_MESSAGES = frozenset(
    {
        "Tool failed; details withheld",
        "Process failed; details withheld",
        "LLM call failed; details withheld",
        "Error; details withheld",
    }
)

MAX_SAFE_COUNT = 2 ** 63 - 1
# Local to this module (new): canonical 8-4-4-4-12 UUID, either hex case.
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def safe_text(value):
    """True exactly when bridge._safe_text accepts value (non-relative variant)."""
    try:
        if not isinstance(value, str):
            return False
        if not value or len(value) > MAX_SAFE_STRING_CHARS:
            return False
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return False
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            return False
        if EMAIL_RE.search(value):
            return False
        if value.lower().startswith(("bearer ", "sk-")):
            return False
        if SECRET_VALUE_RE.search(value) or ABSOLUTE_PATH_RE.search(value):
            return False
        return SAFE_METADATA_RE.fullmatch(value) is not None
    except Exception:
        return False


def safe_count(value):
    """Non-negative int up to 2**63-1, or None. Integral finite floats become int."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if 0 <= value <= MAX_SAFE_COUNT else None
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                return None
            if value < 0 or value > MAX_SAFE_COUNT:
                return None
            return int(value)
        return None
    except Exception:
        return None


def safe_uuid(value):
    """True only for a str that fully matches a canonical 8-4-4-4-12 hex UUID."""
    try:
        return isinstance(value, str) and UUID_RE.fullmatch(value) is not None
    except Exception:
        return False


def safe_model(value):
    """safe_text and not an ARN (a value starting with "arn:", any case)."""
    try:
        return safe_text(value) and not value.lower().startswith("arn:")
    except Exception:
        return False


def safe_tool_name(value):
    """safe_text and not an MCP tool name (a value starting with "mcp__")."""
    try:
        return safe_text(value) and not value.startswith("mcp__")
    except Exception:
        return False
