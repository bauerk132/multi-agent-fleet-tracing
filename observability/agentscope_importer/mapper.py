"""Map fleet schema-v1 spans to AgentScope canonical 0.1 documents.

The mapping rules are the contract (CONTRACT.md §3). Nothing here
invents data: unknown values are carried as unknown, dangling parents
are nulled with a visible note, duplicate native ids and parent cycles
drop the run (fatal, visible), and state content is never carried.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .contract import (
    DEFAULT_KIND,
    FLEET_SCHEMA_VERSION,
    GRAPH_DERIVATION,
    KIND_MAP,
    SCHEMA_VERSION,
    SOURCE,
    STRIPPED_KEYS,
)


def ns_to_iso(ns):
    """Unix nanoseconds -> aware UTC ISO-8601 with Z, or None."""
    if ns is None:
        return None
    if isinstance(ns, bool) or not isinstance(ns, int):
        return None
    if ns < 0:
        return None
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _to_ns(value):
    """Unix nanoseconds from an int or a digit string (OTLP/HTTP JSON
    encodes times as strings). Returns None when not a valid time."""
    if _is_int(value) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _status_of(span):
    """Fleet status (OTel code or plain string) -> canonical status."""
    status = span.get("status")
    if isinstance(status, dict):
        code = status.get("code", 0)
        if code == 2:
            return "error", status.get("message")
        if code == 1:
            return "ok", None
        return "unknown", None
    if isinstance(status, str) and status in ("ok", "error", "cancelled", "unknown"):
        return status, None
    return "unknown", None


def _jsonable(value):
    """Keep JSON scalars as-is; stringify anything else."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _note(code, message):
    return {"code": code, "message": message}


def _find_cycle(parent_of):
    """Return a span id on a parent cycle, or None.

    parent_of maps span_id -> parent span_id (in-trace parents only).
    """
    visiting = set()
    done = set()
    for start in parent_of:
        node = start
        path = []
        while node is not None and node in parent_of:
            if node in done:
                break
            if node in visiting:
                return node
            visiting.add(node)
            path.append(node)
            node = parent_of.get(node)
        for node in path:
            visiting.discard(node)
            done.add(node)
    return None


def map_trace(trace_id, spans, producer):
    """Map one trace's flattened spans to a canonical document.

    Returns (document, notes, dropped) where document is None when the
    run was dropped (fatal: duplicate native id or parent cycle) and
    dropped is {"code": ..., "message": ...} in that case.
    """
    notes = []
    by_id = {}
    for span in spans:
        span_id = span.get("spanId")
        if not span_id:
            continue
        if span_id in by_id:
            return None, notes, _note(
                "duplicate_native_id",
                "native_id %s repeats an earlier span in this trace; run dropped" % span_id,
            )
        by_id[span_id] = span
    if not by_id:
        return None, notes, _note("empty_trace", "trace %s has no identifiable spans" % trace_id)

    # Resolve parents within the trace; dangling parents become None.
    parent_of = {}
    for span_id, span in by_id.items():
        parent = span.get("parentSpanId")
        if parent and parent not in by_id:
            notes.append(
                _note(
                    "dangling_parent",
                    "span %s names a parent that does not exist; set to null" % span_id,
                )
            )
            parent = None
        parent_of[span_id] = parent or None
    cycle = _find_cycle(parent_of)
    if cycle is not None:
        return None, notes, _note(
            "parent_cycle",
            "span %s is on a parent cycle; run dropped" % cycle,
        )

    # Deterministic seq: start-time order, span id as tiebreak.
    def _sort_key(item):
        span_id, span = item
        start_ns = _to_ns(span.get("startTimeUnixNano"))
        return (start_ns if start_ns is not None else 0, span_id)

    ordered = sorted(by_id.items(), key=_sort_key)
    canonical_spans = []
    events = []
    tokens_in = 0
    tokens_out = 0
    sanitized_dropped = 0
    for seq, (span_id, span) in enumerate(ordered):
        name = span.get("name") or "unknown"
        kind = KIND_MAP.get(name, DEFAULT_KIND)
        status, message = _status_of(span)
        start_ns = _to_ns(span.get("startTimeUnixNano"))
        end_ns = _to_ns(span.get("endTimeUnixNano"))
        duration_ms = None
        if start_ns is not None and end_ns is not None:
            if end_ns >= start_ns:
                duration_ms = (end_ns - start_ns) / 1_000_000
            else:
                notes.append(
                    _note(
                        "end_before_start",
                        "span %s ends before it starts; duration treated as unknown" % span_id,
                    )
                )
        attrs = dict(span.get("attributes") or {})
        for key in list(attrs):
            if key in STRIPPED_KEYS:
                del attrs[key]
                sanitized_dropped += 1
        attrs = {k: _jsonable(v) for k, v in attrs.items()}
        attrs["fleet.span"] = name
        attrs["fleet.trace"] = trace_id
        canonical = {
            "native_id": span_id,
            "parent_native_id": parent_of[span_id],
            "seq": seq,
            "kind": kind,
            "name": name,
            "status": status,
            "started_at": ns_to_iso(start_ns),
            "ended_at": ns_to_iso(end_ns),
            "duration_ms": duration_ms,
            "attributes": attrs,
        }
        if kind == "tool":
            tool_name = attrs.get("gen_ai.tool.name")
            if isinstance(tool_name, str) and tool_name:
                canonical["node_name"] = tool_name
                tool = {"name": tool_name}
                call_id = attrs.get("gen_ai.tool.call.id")
                if isinstance(call_id, str) and call_id:
                    tool["call_id"] = call_id
                canonical["tool"] = tool
            else:
                notes.append(
                    _note(
                        "tool_name_missing",
                        "tool span %s has no tool name; carried without a tool block" % span_id,
                    )
                )
        if kind == "llm":
            llm = {}
            model = attrs.get("gen_ai.request.model")
            if isinstance(model, str) and model:
                llm["model"] = model
            in_tok = attrs.get("gen_ai.usage.input_tokens")
            out_tok = attrs.get("gen_ai.usage.output_tokens")
            if _is_int(in_tok):
                llm["input_tokens"] = in_tok
                tokens_in += in_tok
            if _is_int(out_tok):
                llm["output_tokens"] = out_tok
                tokens_out += out_tok
            reasoning = attrs.get("gen_ai.usage.reasoning_tokens")
            if _is_int(reasoning):
                llm["reasoning_tokens"] = reasoning
            if llm:
                canonical["llm"] = llm
        if status == "error" and isinstance(message, str) and message:
            canonical["error"] = {"message": message}
        canonical_spans.append(canonical)
        # Span events (present only on raw OTLP-payload input; the
        # receiver's flattened form drops them).
        for index, event in enumerate(span.get("events") or []):
            if not isinstance(event, dict) or not isinstance(event.get("name"), str):
                notes.append(
                    _note(
                        "invalid_event",
                        "event on span %s is not an object with a name; no event made" % span_id,
                    )
                )
                continue
            ev_time = event.get("timeUnixNano", event.get("time"))
            ev_ns = _to_ns(ev_time)
            ev_attrs = event.get("attributes")
            if isinstance(ev_attrs, list):
                data = {}
                for attr in ev_attrs:
                    if isinstance(attr, dict) and "key" in attr:
                        value = attr.get("value") or {}
                        data[attr["key"]] = (
                            value.get("stringValue", value.get("intValue", value.get("boolValue")))
                            if isinstance(value, dict)
                            else value
                        )
            elif isinstance(ev_attrs, dict):
                data = {k: _jsonable(v) for k, v in ev_attrs.items()}
            else:
                data = {}
            events.append(
                {
                    "native_id": "%s:%s:ev/%d" % (trace_id, span_id, index),
                    "seq": index,
                    "at": ns_to_iso(ev_ns),
                    "type": event["name"],
                    "span_native_id": span_id,
                    "data": data,
                }
            )
    if sanitized_dropped:
        notes.append(
            _note(
                "sanitized_dropped",
                "%d stripped attribute(s) dropped by the mapper" % sanitized_dropped,
            )
        )

    # D-F1 tree correction (ADR-005): no `graph` section is emitted.
    # The fleet graph would be a span hierarchy (one node per span,
    # edges = recorded parent links), not a framework topology, so
    # labelling it `native` would overstate what is known. Parentage
    # stays recorded on spans (parent_native_id); AgentScope renders
    # the run as a tree (compute_graph -> tree) and reports quality
    # `partial` on its own ladder. See CONTRACT.md §3 and §5.

    statuses = [s["status"] for s in canonical_spans]
    if "error" in statuses:
        run_status = "error"
    elif statuses and all(s == "ok" for s in statuses):
        run_status = "ok"
    else:
        run_status = "unknown"
    starts = [_to_ns(span.get("startTimeUnixNano")) for _, span in ordered]
    ends = [_to_ns(span.get("endTimeUnixNano")) for _, span in ordered]
    starts = [s for s in starts if s is not None]
    ends = [s for s in ends if s is not None]
    document = {
        "schema_version": SCHEMA_VERSION,
        "source": json.loads(json.dumps(SOURCE)),
        "run": {
            "native_id": trace_id,
            "status": run_status,
            "started_at": ns_to_iso(min(starts)) if starts else None,
            "ended_at": ns_to_iso(max(ends)) if ends else None,
            "metadata": {
                "fleet_schema_version": FLEET_SCHEMA_VERSION,
                "producer": producer,
                "trace_id": trace_id,
                "graph": GRAPH_DERIVATION,
            },
        },
        "spans": canonical_spans,
    }
    if events:
        document["events"] = events
    return document, notes, None


def map_spans(spans, producer):
    """Map a batch of flattened spans (any traces) to canonical documents.

    Returns {"documents": [(trace_id, document)], "notes": [...],
    "dropped": [...], "unreadable": int, "tokens_in": int,
    "tokens_out": int}. Notes carry the trace id they belong to.
    """
    by_trace = {}
    unreadable = 0
    for span in spans:
        if not isinstance(span, dict):
            unreadable += 1
            continue
        trace_id = span.get("traceId")
        span_id = span.get("spanId")
        if not isinstance(trace_id, str) or not trace_id or not isinstance(span_id, str) or not span_id:
            unreadable += 1
            continue
        by_trace.setdefault(trace_id, []).append(span)
    documents = []
    notes = []
    dropped = []
    tokens_in = 0
    tokens_out = 0
    for trace_id in sorted(by_trace):
        document, trace_notes, drop = map_trace(trace_id, by_trace[trace_id], producer)
        for note in trace_notes:
            note = dict(note)
            note["trace_id"] = trace_id
            notes.append(note)
        if drop is not None:
            drop = dict(drop)
            drop["trace_id"] = trace_id
            dropped.append(drop)
            continue
        for span in document["spans"]:
            llm = span.get("llm") or {}
            tokens_in += llm.get("input_tokens") or 0
            tokens_out += llm.get("output_tokens") or 0
        documents.append((trace_id, document))
    return {
        "documents": documents,
        "notes": notes,
        "dropped": dropped,
        "unreadable": unreadable,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def canonical_bytes(document):
    """Deterministic bytes for a canonical document (hashing)."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def import_id_for(content_sha256):
    return "imp_" + content_sha256[:16]


def run_id_for(content_sha256, trace_id):
    digest = hashlib.sha256(
        ("%s|run|%s" % (content_sha256, trace_id)).encode("utf-8")
    ).hexdigest()
    return "run_" + digest[:16]
