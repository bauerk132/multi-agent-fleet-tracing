"""Span mapping for the Claude Code adapter (M7-B step 3, S4-S7).

map_spans(records) takes the span records produced by adapter.flatten_lines
and returns (spans, counters). spans is a list of flat v1 span dicts in the
shape the AgentScope bridge reads; counters is a dict of ints, always with
every key present.

What S4 does:

- Routing: by record["name"] only. A name in _vocab.SPAN_TYPE_TO_V1 maps to
  its v1 name. A name in _vocab.FOLD_TYPES is a fold record (S6, see below).
  Anything else is dropped and counted in unknown_span_types: wrong case, a
  trailing space, None, and any non-str value. The span.type attribute is
  never read.
- Identity: traceId must be a str of 32 hex digits, spanId a str of 16 hex
  digits, and parentSpanId None, "" or a str of 16 hex digits. A span with a
  bad identity is dropped and counted in invalid_ids. A record that is not a
  dict is also counted in invalid_ids and dropped (it has no identity).
- Duplicates: the first kept (traceId, spanId) wins. Later ones are dropped and
  counted in duplicate_spans. A dropped span never claims its identity.
- Projection (D1): each output span has only traceId, spanId, parentSpanId
  (omitted when None or ""), name, startTimeUnixNano and endTimeUnixNano
  (each omitted when None, not an int, or outside 0..MAX_TIME_NS; an int
  outside that range is counted in out_of_range_timestamps, S11), attributes
  and status. Nothing else is
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

What S6 adds (decision D4): the two fold types are folded into their parent
claude_code.tool span. The map is two passes over the input, so the order of
the records does not change the result (duplicates aside, see below).

- Pass 1 classifies every record: routing, identity checks (a fold record with
  bad ids counts invalid_ids), and the (traceId, spanId) duplicate check (a
  duplicate fold record counts duplicate_spans). Kept spans are built as in S4
  and S5. Fold records are kept as candidates and are not emitted. Duplicates
  are first-wins in input order, shared by spans and fold records.
- Pass 2 applies the folds. A fold record's parent is the KEPT claude_code.tool
  span with the same traceId and a spanId equal to the record's parentSpanId.
  If there is none (no parentSpanId, parent absent, parent in another trace, or
  parent not a kept tool span, for example an llm_request), the record is
  dropped and counted in orphan_folds. Otherwise folded_spans is incremented
  and the record gives an outcome to its parent:
  - claude_code.tool.execution: "error" when its status code is 2 (a valid
    code per S4; an invalid code counts invalid_status) or its success
    attribute is False; "ok" when its code is 1 or success is True; otherwise
    no outcome.
  - claude_code.tool.blocked_on_user: "cancelled" when its decision attribute
    is the str "reject" or "cancelled"; otherwise no outcome.
  The outcomes for one parent are combined with the parent's own S4 status
  (error when the parent is an error, ok when it is ok, otherwise unknown)
  by precedence error > cancelled > ok > unknown. error gives status
  {"code": 2, "message": SAFE_MSG["tool"]} and error.type "ToolError";
  cancelled gives status "cancelled" and error.type "Cancelled"; ok gives
  "ok" with no error.type; unknown gives "unknown". error.type is set only
  for error and cancelled, so a stale value is always removed.
- Nothing else from a fold record is copied: not duration_ms, error,
  error_class, tool_use_id, decision, source, or any attribute. Fold record
  attributes are not counted in stripped_attributes, unmapped_attributes or
  unsafe_values, and their resource attributes are not counted either. Their
  events_count is NOT added to events_dropped. Fold records are discarded
  wholesale, and a fold record is never counted in unknown_span_types.
- Output order is still the input order of the kept spans. Folded records are
  not spans, so they leave no gap in that order.

What S7 adds (decision D5): re-parenting after the folds. A kept span whose
parent was dropped (unknown type, orphan fold, or a folded record) is moved up
to the nearest kept ancestor, so the output tree stays connected.

- The parent map is built in pass 1 from EVERY record with valid ids (valid
  traceId, spanId and parentSpanId), including unknown-type records, folded
  records and orphan folds. Records with bad ids are not in the map. A
  duplicate does not replace an earlier entry: the first record with a
  (traceId, spanId) wins, in input order. Unknown-type records still count only
  in unknown_span_types, and an unknown record with bad ids still counts only
  in unknown_span_types (not invalid_ids).
- kept is the set of (traceId, spanId) of the emitted spans. For each emitted
  span with a parentSpanId P in trace T:
  - (T, P) in kept: unchanged.
  - (T, P) not in the map (P never appeared in the input, for example an
    external TRACEPARENT parent): unchanged, not counted.
  - Otherwise the walk goes up: P becomes the parent of (T, P) in the map, and
    so on. The walk ends at a kept span (its spanId becomes the new parent), at
    an external id (it becomes the new parent, kept as-is), or at a map entry
    that is None or "" (the span becomes a root). A cycle, or a walk longer
    than len(map) + 1 steps, also makes the span a root. Results are memoized
    per (traceId, spanId), so the walk is linear and never recursive.
  - If the new parent would be the span's own spanId, the span becomes a root
    instead. A span is never its own parent.
  - A span whose parentSpanId changed in any way (including removed) is counted
    once in reparented_spans. A root is represented by the absence of the
    parentSpanId key.
- Output order is unchanged, and every span is still a flat v1 span.

What S8 adds: convert_lines(lines) runs adapter.flatten_lines and then map_spans,
and returns one merged counters dict (the flatten counters, the mapping counters
and two tripwires, see below). serialize(spans) gives the canonical bytes of the
output (D1). The tripwires (D10) are computed over every flatten record, kept or
not, and are numbers only: hook_spans counts claude_code.hook records, and
content_attributes_seen counts the span and resource attribute keys that are
content keys or end with a strip suffix, plus one for each user_prompt whose
value is not exactly "<REDACTED>".

Counters: spans_in (every record), spans_out, unknown_span_types, invalid_ids,
duplicate_spans, invalid_status, events_dropped, stripped_attributes,
unmapped_attributes, unsafe_values, folded_spans, orphan_folds, reparented_spans.

Never raises on any record shape: every decision is an explicit type check.
"""

from __future__ import annotations

import json
import re

from . import _sanitize, _vocab
from .adapter import flatten_lines

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

# Largest start/end time kept (S11). 2**63 - 1_000_000 ns is about year 2262;
# the 1 ms margin keeps the importer's float-to-microsecond round-trip inside
# SQLite int64 (2**63 - 307 and above overflow there).
MAX_TIME_NS = 2 ** 63 - 1_000_000

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
    "folded_spans",
    "orphan_folds",
    "reparented_spans",
    "out_of_range_timestamps",
)

# Rank of each outcome when a parent has several (S6): the highest wins.
_PRECEDENCE = {"unknown": 0, "ok": 1, "cancelled": 2, "error": 3}

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


def _status_code(record_status):
    """Return (code, invalid_flag) for a raw status value, using the S4 rules.

    code is 0, 1 or 2 when the status is a dict with a valid code, else None.
    invalid_flag is True for a present status that is not a dict, and for a
    dict whose code is present and not a valid code (an int, not a bool, in
    {0, 1, 2}).
    """
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
    return code, invalid


def _status_for(kind, record_status, attributes):
    """Return (status_value, error_type_or_None, invalid_status_flag)."""
    code, invalid = _status_code(record_status)
    success = attributes.get("success") if kind in ("llm", "tool") else None
    if code == 2 or success is False:
        return {"code": 2, "message": SAFE_MSG[kind]}, ERROR_TYPE[kind], invalid
    if code == 1 or success is True:
        return "ok", None, invalid
    return "unknown", None, invalid


def _own_outcome(status_value, error_type):
    """The S4 status of a tool span, as an outcome for the S6 combination."""
    if error_type is not None:
        return "error"
    if status_value == "ok":
        return "ok"
    return "unknown"


def _fold_outcome(name, record):
    """Return (outcome_or_None, invalid_status_flag) for a kept fold record (S6).

    Only the status code, success and decision are read. Nothing else from
    the record is used.
    """
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    if name == "claude_code.tool.execution":
        code, invalid = _status_code(record.get("status"))
        success = attributes.get("success")
        if code == 2 or success is False:
            return "error", invalid
        if code == 1 or success is True:
            return "ok", invalid
        return None, invalid
    decision = attributes.get("decision")
    if isinstance(decision, str) and decision in ("reject", "cancelled"):
        return "cancelled", False
    return None, False


def _apply_final(out, final):
    """Set the status and error.type of a kept tool span to its final outcome (S6)."""
    attributes = out["attributes"]
    attributes.pop("error.type", None)
    if final == "error":
        out["status"] = {"code": 2, "message": SAFE_MSG["tool"]}
        attributes["error.type"] = ERROR_TYPE["tool"]
    elif final == "cancelled":
        out["status"] = "cancelled"
        attributes["error.type"] = "Cancelled"
    elif final == "ok":
        out["status"] = "ok"
    else:
        out["status"] = "unknown"


def _events_count(record):
    value = record.get("events_count")
    if _is_int(value) and value >= 0:
        return value
    return 0


def _resolve_parent(start, parent_map, kept, memo):
    """Walk up from the (traceId, parentSpanId) key start (S7, D5).

    Returns the new parentSpanId for a span whose parent is start, or None when
    the span becomes a root. The walk ends at a kept span (its spanId), an
    external id (never in the input, kept as-is) or a null map entry (root).
    A cycle, or a walk longer than len(parent_map) + 1 steps, gives a root.
    Results are memoized per key; the walk is iterative.
    """
    limit = len(parent_map) + 1
    path = []
    on_path = set()
    key = start
    while True:
        if key in memo:
            result = memo[key]
            break
        if key in kept:
            result = key[1]
            break
        if key not in parent_map:
            result = key[1]
            break
        if key in on_path or len(path) >= limit:
            result = None
            break
        path.append(key)
        on_path.add(key)
        nxt = parent_map[key]
        if not nxt:
            result = None
            break
        key = (key[0], nxt)
    for node in path:
        memo[node] = result
    return result


def _reparent(spans, parent_map, counters):
    """Re-parent kept spans whose parent was dropped or folded (S7, D5), in place.

    A kept parent or an id never seen in the input is left alone (not counted).
    A span whose parentSpanId changes in any way is counted once in
    reparented_spans. A span is never made its own parent: that case becomes a
    root.
    """
    kept = {(span["traceId"], span["spanId"]) for span in spans}
    memo = {}
    for span in spans:
        parent = span.get("parentSpanId")
        if parent is None:
            continue
        trace_id = span["traceId"]
        own = span["spanId"]
        if parent == own:
            new_parent = None
        else:
            key = (trace_id, parent)
            if key in kept or key not in parent_map:
                continue
            new_parent = _resolve_parent(key, parent_map, kept, memo)
            if new_parent == own:
                new_parent = None
        if new_parent is None:
            del span["parentSpanId"]
        else:
            span["parentSpanId"] = new_parent
        counters["reparented_spans"] += 1


def map_spans(records):
    """Map flatten_lines span records to flat v1 spans. Returns (spans, counters).

    ``records`` is an iterable of span records (dicts from
    adapter.flatten_lines, or any other value; anything that is not a dict is
    counted in invalid_ids and dropped). ``spans`` is a list of dicts with only
    the keys traceId, spanId, parentSpanId (omitted for a root), name,
    startTimeUnixNano and endTimeUnixNano (each omitted when not an int in
    0..MAX_TIME_NS, S11), attributes and status. ``counters`` is a dict with the
    int keys spans_in, spans_out, unknown_span_types, invalid_ids,
    duplicate_spans, invalid_status, events_dropped, stripped_attributes,
    unmapped_attributes, unsafe_values, folded_spans, orphan_folds,
    reparented_spans and out_of_range_timestamps, all always present. An int
    time outside 0..MAX_TIME_NS is counted once per field in
    out_of_range_timestamps; a missing or non-int time is not counted.

    Routing is by record["name"] only (see the module docstring). Status
    messages from the source are never read or copied. For each kept span, the
    attribute rows of _vocab.ATTRIBUTE_SOURCES[name] produce the output
    attributes (S5): a source value that fails its kind's check is not emitted
    and is counted in unsafe_values; every other source key and every resource
    attribute is counted in stripped_attributes or unmapped_attributes, and
    nothing from the resource is emitted.

    Two passes over the records (S6). Pass 1 classifies every record and builds
    the kept spans; fold records (tool.execution, blocked_on_user) are kept as
    candidates only. Pass 2 folds each candidate into its kept parent tool span,
    or drops it as an orphan. The result does not depend on record order, except
    that duplicates are first-wins in input order.
    """
    records = list(records)
    spans = []
    counters = {key: 0 for key in COUNTER_KEYS}
    seen = set()
    # (traceId, spanId) -> parentSpanId (None for a root), first record wins (S7).
    parent_map = {}
    # (traceId, spanId) -> index into tool_entries, for kept claude_code.tool spans.
    tool_index = {}
    # [out_span, own_outcome] for each kept tool span, in input order.
    tool_entries = []
    # Kept fold records, in input order (the input of pass 2).
    fold_candidates = []

    # Pass 1: classify every record and build the kept non-fold spans.
    for record in records:
        counters["spans_in"] += 1
        if not isinstance(record, dict):
            counters["invalid_ids"] += 1
            continue

        name = record.get("name")
        is_fold = isinstance(name, str) and name in _vocab.FOLD_TYPES
        v1_name = _vocab.SPAN_TYPE_TO_V1.get(name) if isinstance(name, str) else None
        is_unknown = v1_name is None and not is_fold

        trace_id = record.get("traceId")
        span_id = record.get("spanId")
        parent_id = record.get("parentSpanId")
        if (
            not _valid_id(trace_id, TRACE_ID_RE)
            or not _valid_id(span_id, SPAN_ID_RE)
            or not _valid_parent(parent_id)
        ):
            # An unknown-type record with bad ids counts only as unknown.
            if is_unknown:
                counters["unknown_span_types"] += 1
            else:
                counters["invalid_ids"] += 1
            continue

        # S7: every record with valid ids feeds the parent map, first wins.
        # Unknown-type records included, but they are never kept.
        parent_map.setdefault((trace_id, span_id), parent_id or None)

        if is_unknown:
            counters["unknown_span_types"] += 1
            continue

        key = (trace_id, span_id)
        if key in seen:
            counters["duplicate_spans"] += 1
            continue
        seen.add(key)

        if is_fold:
            fold_candidates.append(record)
            continue

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
        for field in ("startTimeUnixNano", "endTimeUnixNano"):
            value = record.get(field)
            if not _is_int(value):
                continue
            if 0 <= value <= MAX_TIME_NS:
                out[field] = value
            else:
                counters["out_of_range_timestamps"] += 1
        out["attributes"] = out_attributes
        out["status"] = status
        spans.append(out)

        if name == "claude_code.tool":
            tool_index[key] = len(tool_entries)
            tool_entries.append((out, _own_outcome(status, error_type)))

        counters["events_dropped"] += _events_count(record)

    # Pass 2: fold each kept fold record into its parent tool span, or drop it.
    outcomes = {}
    for record in fold_candidates:
        parent_id = record.get("parentSpanId")
        index = None
        if isinstance(parent_id, str) and parent_id:
            index = tool_index.get((record.get("traceId"), parent_id))
        if index is None:
            counters["orphan_folds"] += 1
            continue
        counters["folded_spans"] += 1
        outcome, invalid = _fold_outcome(record.get("name"), record)
        if invalid:
            counters["invalid_status"] += 1
        if outcome is not None:
            outcomes.setdefault(index, []).append(outcome)

    for index, parent_outcomes in outcomes.items():
        out, own = tool_entries[index]
        final = max([own] + parent_outcomes, key=_PRECEDENCE.__getitem__)
        _apply_final(out, final)

    _reparent(spans, parent_map, counters)
    counters["spans_out"] = len(spans)
    return spans, counters


def _tripwires(records):
    """Return (hook_spans, content_attributes_seen) over every flatten record (D10).

    hook_spans counts the records named claude_code.hook, whatever their ids or
    kept state. content_attributes_seen counts, per record, each span attribute
    key and each resource attribute key that is in _vocab.CONTENT_KEYS or ends
    with one of _vocab.STRIP_SUFFIXES, plus one for each user_prompt key (span
    or resource) whose value is not exactly _vocab.REDACTED_PROMPT. Absent
    user_prompt counts nothing. Only numbers are returned.
    """
    hook_spans = 0
    content_seen = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("name") == "claude_code.hook":
            hook_spans += 1
        for source in (record.get("attributes"), record.get("resource_attributes")):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if not isinstance(key, str):
                    continue
                if key in _vocab.CONTENT_KEYS or key.endswith(_vocab.STRIP_SUFFIXES):
                    content_seen += 1
                elif key == "user_prompt" and value != _vocab.REDACTED_PROMPT:
                    content_seen += 1
    return hook_spans, content_seen


def convert_lines(lines):
    """Flatten receiver JSONL lines and map them. Returns (spans, counters).

    ``lines`` is passed to adapter.flatten_lines unchanged (a bare str or bytes
    raises TypeError there). The flatten records then go through map_spans. The
    returned counters dict is the merge of the flatten counters and the mapping
    counters, plus the two D10 tripwires hook_spans and content_attributes_seen.
    The flatten and mapping counter names are disjoint (a test checks this), so
    nothing is overwritten. Every value is an int. Output spans are the same
    flat v1 spans as map_spans returns.
    """
    records, flatten_counters = flatten_lines(lines)
    spans, mapping_counters = map_spans(records)
    hook_spans, content_seen = _tripwires(records)
    counters = dict(flatten_counters)
    counters.update(mapping_counters)
    counters["hook_spans"] = hook_spans
    counters["content_attributes_seen"] = content_seen
    return spans, counters


def serialize(spans):
    """Return the canonical bytes of a list of flat v1 spans (D1).

    json.dumps with sort_keys=True, separators (",", ":") and ensure_ascii=True,
    encoded as ASCII. The same spans always give byte-identical output, and the
    output is a JSON array the bridge accepts as a "spans" source.
    """
    return json.dumps(
        spans, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
