"""Flatten receiver JSONL into a span list (M7-B step 1, sub-step 2).

Input: an iterable of text lines, each a raw OTLP/JSON payload as written
by observability/receiver.py (one ``{"resourceSpans": [...]}`` object per
line). Output: a flat list of span records plus counters.

Pure and stdlib only. No mapping, stripping, or network access happens
here. Counters carry numbers only, never line contents.
"""

from __future__ import annotations

import json


def _attrs_to_dict(attrs):
    """Convert an OTLP KeyValue list to a plain dict. Unknown shapes are skipped."""
    out = {}
    if not isinstance(attrs, list):
        return out
    for item in attrs:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in value:
                out[key] = value[kind]
                break
    return out


def flatten_lines(lines):
    """Return ``(spans, counters)`` for the given receiver JSONL lines.

    Each span record has: ``name``, ``traceId``, ``spanId``, ``parentSpanId``,
    ``attributes`` (dict), ``resource_attributes`` (dict), ``events`` (list,
    raw; the later mapping step drops it) and ``status`` (raw dict or None).

    Counters: ``lines_read`` (non-blank lines seen), ``malformed_lines``
    (not JSON, not an object, or no ``resourceSpans`` list), ``spans``.
    """
    spans = []
    counters = {"lines_read": 0, "malformed_lines": 0, "spans": 0}

    for line in lines:
        text = line.strip()
        if not text:
            continue
        counters["lines_read"] += 1
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            counters["malformed_lines"] += 1
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("resourceSpans"), list):
            counters["malformed_lines"] += 1
            continue

        for resource_span in payload["resourceSpans"]:
            if not isinstance(resource_span, dict):
                continue
            resource_attrs = _attrs_to_dict(
                (resource_span.get("resource") or {}).get("attributes")
            )
            for scope_span in resource_span.get("scopeSpans") or []:
                if not isinstance(scope_span, dict):
                    continue
                for span in scope_span.get("spans") or []:
                    if not isinstance(span, dict):
                        continue
                    spans.append({
                        "name": span.get("name"),
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "parentSpanId": span.get("parentSpanId"),
                        "attributes": _attrs_to_dict(span.get("attributes")),
                        "resource_attributes": resource_attrs,
                        "events": span.get("events") if isinstance(span.get("events"), list) else [],
                        "status": span.get("status") if isinstance(span.get("status"), dict) else None,
                    })
                    counters["spans"] += 1

    return spans, counters
