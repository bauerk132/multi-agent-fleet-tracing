"""Span mapping for the Claude Code adapter (M7-B step 3, S4 core and S5 attributes).

map_spans(records) takes the span records produced by adapter.flatten_lines
and returns (spans, counters). spans is a list of flat v1 span dicts in the
shape the AgentScope bridge reads; counters is a dict of ints, always with
every key present.

What S4 does:

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
- Attributes (D8): agent.name ("claude-code") and observation.source
  ("otel-export") on every span, plus error.type when the status is an error.
  Nothing else in the constants is ever overwritten by mapped attributes.
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

What S5 adds, for kept spans only (dropped spans count nothing here):

- Attribute rows: for each row (output_key, source_keys, kind) of
  _vocab.ATTRIBUTE_SOURCES[name], the first source key present (present means
  the key is in the dict and its value is not None) is the only one evaluated.
  There is no fallback to a later source. The value is checked by kind
  (uuid: safe_uuid; seq: safe_count, emitted as a decimal string;
  model: safe_model; count: safe_count; tool_name:
  safe_tool_name; text: safe_text). A passing value is emitted (counts become
  ints). A failing value is not emitted and is counted in unsafe_values.
- Every other source attribute key is counted once: stripped_attributes when
  _vocab.is_stripped(key), otherwise unmapped_attributes. A key that is
  shadowed by an earlier source (for example tool_name after a present
  tool_name_safe) and a key whose value is None both count as unmapped. The
  keys in _vocab.FOLD_INPUT_KEYS (success, decision) are never counted.
- Resource attributes (record["resource_attributes"]) are never emitted. Each
  key is counted in stripped_attributes or unmapped_attributes, once per kept
  span, because flatten_lines copies them into every span.

Counters: spans_in (every record), spans_out, unknown_span_types, invalid_ids,
duplicate_spans, invalid_status, events_dropped, stripped_attributes,
unmapped_attributes, unsafe_values.

Never raises on any record shape: every decision is an explicit type check.
Output order is input order of the kept spans, so the result is deterministic.

Later steps add: S6 folding of the two fold types into their parent
claude_code.tool span, and S7 re-parenting of spans whose parent was dropped.
Until S6, the fold types count as unknown.
"""

from __future__ import annotations

import re

from . import _sanitize, _vocab

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
    "stripped_attributes",
    "unmapped_attributes",
    "unsafe_values",
)

_VALID_CODES = (0, 1, 2)

# Returned by _check_value when a source value fails its kind's check.
_UNSAFE = object()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_value(kind, value):
    """Return the output value for a source value of this kind, or _UNSAFE."""
    if kind == "uuid":
        return value if _sanitize.safe_uuid(value) else _UNSAFE
    if kind == "seq":
        # Bounded like counts (0 to 2**63 - 1), so the decimal string always
        # fits the bridge's 256-character text limit.
        count = _sanitize.safe_count(value)
        return _UNSAFE if count is None else str(count)
    if kind == "model":
        return value if _sanitize.safe_model(value) else _UNSAFE
    if kind == "count":
        count = _sanitize.safe_count(value)
        return _UNSAFE if count is None else count
    if kind == "tool_name":
        return value if _sanitize.safe_tool_name(value) else _UNSAFE
    if kind == "text":
        return value if _sanitize.safe_text(value) else _UNSAFE
    return _UNSAFE


def _first_present(attributes, source_keys):
    # Present means the key is in the dict and its value is not None.
    for key in source_keys:
        if attributes.get(key) is not None:
            return key
    return None


def _map_attributes(name, attributes, out_attributes, counters):
    """Apply the attribute rows for span name; count every other source key."""
    evaluated = set()
    for output_key, source_keys, kind in _vocab.ATTRIBUTE_SOURCES.get(name, ()):
        source = _first_present(attributes, source_keys)
        if source is None:
            continue
        evaluated.add(source)
        value = _check_value(kind, attributes[source])
        if value is _UNSAFE:
            counters["unsafe_values"] += 1
        else:
            out_attributes[output_key] = value
    for key in attributes:
        if key in evaluated or key in _vocab.FOLD_INPUT_KEYS:
            continue
        if _vocab.is_stripped(key):
            counters["stripped_attributes"] += 1
        else:
            counters["unmapped_attributes"] += 1


def _count_resource_attributes(resource, counters):
    # Resource attributes are never emitted: each key is only counted.
    if not isinstance(resource, dict):
        return
    for key in resource:
        if _vocab.is_stripped(key):
            counters["stripped_attributes"] += 1
        else:
            counters["unmapped_attributes"] += 1


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
    spans_out, unknown_span_types, invalid_ids, duplicate_spans, invalid_status,
    events_dropped, stripped_attributes, unmapped_attributes and unsafe_values,
    all always present.

    Routing is by record["name"] only (see the module docstring). Status
    messages from the source are never read or copied. For each kept span, the
    attribute rows of _vocab.ATTRIBUTE_SOURCES[name] produce the output
    attributes (S5): a source value that fails its kind's check is not emitted
    and is counted in unsafe_values; every other source key and every resource
    attribute is counted in stripped_attributes or unmapped_attributes, and
    nothing from the resource is emitted. Folding of the tool.execution and
    blocked_on_user spans and re-parenting are later steps (S6 and S7), so in
    this step those names count as unknown.
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
        _map_attributes(name, attributes, out_attributes, counters)
        _count_resource_attributes(record.get("resource_attributes"), counters)

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
