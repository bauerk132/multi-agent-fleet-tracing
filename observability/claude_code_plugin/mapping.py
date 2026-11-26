"""Span mapping for the Claude Code adapter (M7-B step 3, S4: core).

map_spans(records) takes the span records produced by adapter.flatten_lines
and returns (spans, counters). spans is a list of flat v1 span dicts in the
shape the AgentScope bridge reads; counters is a dict of ints, always with
every key present.

What this step does (S4):

- Routing: by record["name"] only. A name in _vocab.SPAN_TYPE_TO_V1 maps to
  its v1 name. Anything else is dropped and counted in unknown_span_types:
  the fold types (claude_code.tool.execution, claude_code.tool.blocked_on_user),
  wrong case, a trailing space, None, and any non-str value. The span.type
  attribute is never read.
- Identity: traceId must be a str of 32 hex digits, spanId a str of 16 hex
  digits, and parentSpanId None, "" or a str of 16 hex digits. A span with a
  bad identity is dropped and counted in invalid_ids. A record that is not a
  dict is also counted in invalid_ids and dropped (it has no identity).
- Duplicates: the first kept (traceId, spanId) wins. Later ones are dropped and
  counted in duplicate_spans. A dropped span never claims its identity.
- Projection (D1): each output span has only traceId, spanId, parentSpanId
  (omitted when None or ""), name, startTimeUnixNano and endTimeUnixNano
  (each omitted when None or not an int), attributes and status. Nothing else is
  ever emitted: no resource, scope, events, kind or resource attributes.
- Attributes (D8): only agent.name ("claude-code") and observation.source
  ("otel-export"), plus error.type when the status is an error. No source
  attribute is mapped in this step.
- Status (D3, D3b): the kind is "llm" for llm.call, "tool" for tool.exec and
  "other" for agent.run and hook.exec. Only status["code"] is read, and only
  when status is a dict; status["message"] is never read or copied. A valid
  code is an int (not a bool) in {0, 1, 2}. Code 2 gives the error status
  {"code": 2, "message": SAFE_MSG[kind]} and error.type ERROR_TYPE[kind]. Code 1
  gives "ok". Code 0, absent or None is unset. For llm and tool only, the
  success attribute then decides: success False gives error (code 2 always
  wins), and success True gives "ok" when the status is otherwise unset or code 1.
  Otherwise the status is "unknown". A status that is present, not None and not
  a dict, or a dict whose code is present and not a valid code, is counted once
  in invalid_status and treated as unset (the success rule may still apply).
  Only spans that are kept are checked.
- events_dropped: the sum of events_count (an int, not a bool, and >= 0) over
  the kept spans. Other values add nothing.

Counters: spans_in (every record), spans_out, unknown_span_types, invalid_ids,
duplicate_spans, invalid_status, events_dropped.

Never raises on any record shape: every decision is an explicit type check.
Output order is input order of the kept spans, so the result is deterministic.

Later steps add: S5 attribute mapping and sanitization (the attribute rows of
_vocab.ATTRIBUTE_SOURCES), S6 folding of the two fold types into their parent
claude_code.tool span, and S7 re-parenting of spans whose parent was dropped.
Until S6, the fold types count as unknown.
"""

from __future__ import annotations

import re

from . import _vocab

# Copied by value from agentscope_importer/bridge.py (TRACE_ID_RE, SPAN_ID_RE);
# drift tests in tests/test_claude_code_plugin.py compare pattern and flags.
TRACE_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
SPAN_ID_RE = re.compile(r"[0-9a-fA-F]{16}\Z")

# Status messages by kind. Copied from the bridge's real-exporter messages
# (bridge._real_exporter_error_message); a test asserts they are members of
# _sanitize.SAFE_ERROR_MESSAGES. The status message from the source is never used.
SAFE_MSG = {
    "llm": "LLM call failed; details withheld",
    "tool": "Tool failed; details withheld",
    "other": "Error; details withheld",
}

# error.type by kind. Each value is a member of _sanitize.SAFE_ERROR_TYPES
# (a test asserts this).
ERROR_TYPE = {
    "llm": "LLMError",
    "tool": "ToolError",
    "other": "Unknown",
}

KIND_BY_NAME = {
    "llm.call": "llm",
    "tool.exec": "tool",
}

COUNTER_KEYS = (
    "spans_in",
    "spans_out",
    "unknown_span_types",
    "invalid_ids",
    "duplicate_spans",
    "invalid_status",
    "events_dropped",
)

_VALID_CODES = (0, 1, 2)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_id(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_parent(value):
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    return _valid_id(value, SPAN_ID_RE)


def _status_for(kind, record_status, attributes):
    """Return (status_value, error_type_or_None, invalid_status_flag)."""
    code = None
    invalid = False
    if record_status is not None:
        if isinstance(record_status, dict):
            raw_code = record_status.get("code")
            if raw_code is not None:
                if _is_int(raw_code) and raw_code in _VALID_CODES:
                    code = raw_code
                else:
                    invalid = True
        else:
            invalid = True

    success = attributes.get("success") if kind in ("llm", "tool") else None
    if code == 2 or success is False:
        return {"code": 2, "message": SAFE_MSG[kind]}, ERROR_TYPE[kind], invalid
    if code == 1 or success is True:
        return "ok", None, invalid
    return "unknown", None, invalid


def _events_count(record):
    value = record.get("events_count")
    if _is_int(value) and value >= 0:
        return value
    return 0


def map_spans(records):
    """Map flatten_lines span records to flat v1 spans. Returns (spans, counters).

    ``records`` is an iterable of span records (dicts from
    adapter.flatten_lines, or any other value; anything that is not a dict is
    counted in invalid_ids and dropped). ``spans`` is a list of dicts with only
    the keys traceId, spanId, parentSpanId (omitted for a root), name,
    startTimeUnixNano and endTimeUnixNano (each omitted when not an int),
    attributes and status. ``counters`` is a dict with the int keys spans_in,
    spans_out, unknown_span_types, invalid_ids, duplicate_spans, invalid_status
    and events_dropped, all always present.

    Routing is by record["name"] only (see the module docstring). Status
    messages from the source are never read or copied. Folding of the
    tool.execution and blocked_on_user spans and re-parenting are later steps
    (S6 and S7), so in this step those names count as unknown.
    """
    spans = []
    counters = {key: 0 for key in COUNTER_KEYS}
    seen = set()
    for record in records:
        counters["spans_in"] += 1
        if not isinstance(record, dict):
            counters["invalid_ids"] += 1
            continue

        name = record.get("name")
        v1_name = _vocab.SPAN_TYPE_TO_V1.get(name) if isinstance(name, str) else None
        if v1_name is None:
            counters["unknown_span_types"] += 1
            continue

        trace_id = record.get("traceId")
        span_id = record.get("spanId")
        parent_id = record.get("parentSpanId")
        if (
            not _valid_id(trace_id, TRACE_ID_RE)
            or not _valid_id(span_id, SPAN_ID_RE)
            or not _valid_parent(parent_id)
        ):
            counters["invalid_ids"] += 1
            continue

        key = (trace_id, span_id)
        if key in seen:
            counters["duplicate_spans"] += 1
            continue
        seen.add(key)

        kind = KIND_BY_NAME.get(v1_name, "other")
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        status, error_type, invalid = _status_for(kind, record.get("status"), attributes)
        if invalid:
            counters["invalid_status"] += 1

        out_attributes = {
            "agent.name": _vocab.AGENT_NAME,
            "observation.source": _vocab.OBSERVATION_SOURCE,
        }
        if error_type is not None:
            out_attributes["error.type"] = error_type

        out = {"traceId": trace_id, "spanId": span_id}
        if isinstance(parent_id, str) and parent_id:
            out["parentSpanId"] = parent_id
        out["name"] = v1_name
        start = record.get("startTimeUnixNano")
        if _is_int(start):
            out["startTimeUnixNano"] = start
        end = record.get("endTimeUnixNano")
        if _is_int(end):
            out["endTimeUnixNano"] = end
        out["attributes"] = out_attributes
        out["status"] = status
        spans.append(out)

        counters["events_dropped"] += _events_count(record)

    counters["spans_out"] = len(spans)
    return spans, counters
