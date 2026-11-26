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

    ``lines`` must be an iterable of lines. A bare ``str`` or ``bytes`` raises
    ``TypeError`` (caller bug). Otherwise this never raises on malformed
    content; it counts and keeps going.

    Each span record has: ``name``, ``traceId``, ``spanId``, ``parentSpanId``,
    ``attributes`` (dict), ``resource_attributes`` (dict), ``events`` (list,
    raw; the later mapping step drops it) and ``status`` (raw dict or None).

    Per-item rules: ``bytes`` items are decoded as strict UTF-8 (invalid
    UTF-8 is malformed). Any non-``str`` item after that (None, int, ...)
    counts as read and malformed. Blank or whitespace-only text lines are
    skipped and not counted. ``json.loads`` failures (``ValueError``,
    ``TypeError``, ``RecursionError``) are malformed. A line that is not a
    JSON object, or has no ``resourceSpans`` list, is malformed.

    Inner shapes: a missing or ``None`` value is treated as absent and not
    counted. A present value of the wrong type is skipped and counted once
    in ``skipped_items``, and the rest of the line keeps flattening. This
    covers a non-dict ``resourceSpans`` entry, a non-dict ``resource`` (its
    scopeSpans still flatten with empty resource attributes), a non-list
    ``scopeSpans`` or ``spans``, and a non-dict scope or span entry. A span whose
    ``name``, ``traceId``, ``spanId`` or ``parentSpanId`` is present and not a
    ``str`` (``None`` or absent is fine) is skipped and counted once.

    Counters are numbers only, never line contents: ``lines_read``
    (non-blank lines seen), ``malformed_lines``, ``spans`` and
    ``skipped_items``. No BOM handling happens here (a later step).
    """
    if isinstance(lines, (str, bytes)):
        raise TypeError("flatten_lines expects an iterable of lines, not str or bytes")

    spans = []
    counters = {"lines_read": 0, "malformed_lines": 0, "spans": 0, "skipped_items": 0}

    for raw in lines:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                counters["lines_read"] += 1
                counters["malformed_lines"] += 1
                continue
        if not isinstance(raw, str):
            counters["lines_read"] += 1
            counters["malformed_lines"] += 1
            continue
        text = raw.strip()
        if not text:
            continue
        counters["lines_read"] += 1
        try:
            payload = json.loads(text)
        except (ValueError, TypeError, RecursionError):
            counters["malformed_lines"] += 1
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("resourceSpans"), list):
            counters["malformed_lines"] += 1
            continue

        for resource_span in payload["resourceSpans"]:
            if not isinstance(resource_span, dict):
                counters["skipped_items"] += 1
                continue
            resource = resource_span.get("resource")
            if resource is None:
                resource_attrs = {}
            elif isinstance(resource, dict):
                resource_attrs = _attrs_to_dict(resource.get("attributes"))
            else:
                counters["skipped_items"] += 1
                resource_attrs = {}
            scope_spans = resource_span.get("scopeSpans")
            if scope_spans is None:
                continue
            if not isinstance(scope_spans, list):
                counters["skipped_items"] += 1
                continue
            for scope_span in scope_spans:
                if not isinstance(scope_span, dict):
                    counters["skipped_items"] += 1
                    continue
                span_list = scope_span.get("spans")
                if span_list is None:
                    continue
                if not isinstance(span_list, list):
                    counters["skipped_items"] += 1
                    continue
                for span in span_list:
                    if not isinstance(span, dict):
                        counters["skipped_items"] += 1
                        continue
                    identity = (
                        span.get("name"), span.get("traceId"),
                        span.get("spanId"), span.get("parentSpanId"),
                    )
                    if any(value is not None and not isinstance(value, str) for value in identity):
                        counters["skipped_items"] += 1
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
