"""Flatten receiver JSONL into a span list (M7-B step 1, sub-step 2).

Input: an iterable of text lines, each a raw OTLP/JSON payload as written
by observability/receiver.py (one ``{"resourceSpans": [...]}`` object per
line). Output: a flat list of span records plus counters.

Pure and stdlib only. No mapping, stripping, or network access happens
here. Counters carry numbers only, never line contents.
"""

from __future__ import annotations

import json
import math
import re

_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1
_INT_TEXT = re.compile(r"-?[0-9]{1,19}")


def _reject_constant(name):
    # NaN, Infinity and -Infinity are not JSON: the whole line is malformed.
    raise ValueError("non-JSON constant: " + name)


def _strict_float(text):
    # Values such as 1e1000 parse to inf: the whole line is malformed.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _decode_any_value(value):
    """Decode one OTLP/JSON AnyValue. Return ``(True, decoded)`` or ``(False, None)``.

    The value must be a dict with exactly one key. Accepted kinds:
    ``stringValue`` (str); ``intValue`` (a real int that is not a bool, or a
    str of ASCII digits with an optional leading ``-``, at most 19 digits,
    decoded to int, within int64); ``doubleValue`` (an int or float that is
    not a bool and is finite, decoded to float); ``boolValue`` (a real bool).
    Anything else (arrayValue, kvlistValue, bytesValue, unknown kinds, empty
    or multi-key dicts) is rejected.
    """
    if not isinstance(value, dict) or len(value) != 1:
        return False, None
    kind, item = next(iter(value.items()))
    if kind == "stringValue":
        return (True, item) if isinstance(item, str) else (False, None)
    if kind == "intValue":
        if isinstance(item, bool):
            return False, None
        if isinstance(item, int):
            number = item
        elif isinstance(item, str) and _INT_TEXT.fullmatch(item):
            number = int(item)
        else:
            return False, None
        if not _INT64_MIN <= number <= _INT64_MAX:
            return False, None
        return True, number
    if kind == "doubleValue":
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return False, None
        try:
            number = float(item)
        except OverflowError:
            return False, None
        return (True, number) if math.isfinite(number) else (False, None)
    if kind == "boolValue":
        return (True, item) if isinstance(item, bool) else (False, None)
    return False, None


def _attrs_to_dict(attrs):
    """Convert an OTLP KeyValue list to ``(dict, dropped_count)``.

    A KeyValue item is kept only if it is a dict with a str ``key`` and a
    ``value`` that decodes under ``_decode_any_value``. Every other item is
    dropped and counted once in ``dropped_count``. A duplicate key keeps the
    last value. A non-list ``attrs`` yields ``({}, 0)``; the caller counts that
    shape as a skipped item.
    """
    out = {}
    dropped = 0
    if not isinstance(attrs, list):
        return out, 0
    for item in attrs:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            dropped += 1
            continue
        ok, value = _decode_any_value(item.get("value"))
        if not ok:
            dropped += 1
            continue
        out[item["key"]] = value
    return out, dropped


def _read_attrs(attrs, counters):
    """Decode an ``attributes`` field; a present non-list value is skipped and counted."""
    if attrs is not None and not isinstance(attrs, list):
        counters["skipped_items"] += 1
        return {}
    decoded, dropped = _attrs_to_dict(attrs)
    counters["dropped_attributes"] += dropped
    return decoded


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
    skipped and not counted. Parsing is strict JSON: ``NaN``, ``Infinity``,
    ``-Infinity`` and any float that is not finite (such as ``1e1000``) make
    the line malformed. ``json.loads`` failures (``ValueError``,
    ``TypeError``, ``RecursionError``) are malformed. A line that is not a
    JSON object, or has no ``resourceSpans`` list, is malformed.

    Inner shapes: a missing or ``None`` value is treated as absent and not
    counted. A present value of the wrong type is skipped and counted once
    in ``skipped_items``, and the rest of the line keeps flattening. This
    covers a non-dict ``resourceSpans`` entry, a non-dict ``resource`` (its
    scopeSpans still flatten with empty resource attributes), a non-list
    ``scopeSpans`` or ``spans``, a non-list ``attributes`` on a resource or
    span (treated as empty), and a non-dict scope or span entry. A span whose
    ``name``, ``traceId``, ``spanId`` or ``parentSpanId`` is present and not a
    ``str`` (``None`` or absent is fine) is skipped and counted once.

    Attribute values follow the OTLP/JSON AnyValue rules in
    ``_attrs_to_dict``. Each rejected KeyValue item adds one to
    ``dropped_attributes``. Resource attributes are decoded once per resource
    entry, span attributes once per span.

    Counters are numbers only, never line contents: ``lines_read``
    (non-blank lines seen), ``malformed_lines``, ``spans``, ``skipped_items``
    and ``dropped_attributes`` (the sum of all dropped attribute items). No
    BOM handling happens here (a later step).
    """
    if isinstance(lines, (str, bytes)):
        raise TypeError("flatten_lines expects an iterable of lines, not str or bytes")

    spans = []
    counters = {
        "lines_read": 0, "malformed_lines": 0, "spans": 0,
        "skipped_items": 0, "dropped_attributes": 0,
    }

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
            payload = json.loads(
                text, parse_constant=_reject_constant, parse_float=_strict_float,
            )
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
                resource_attrs = _read_attrs(resource.get("attributes"), counters)
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
                    span_attrs = _read_attrs(span.get("attributes"), counters)
                    spans.append({
                        "name": span.get("name"),
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "parentSpanId": span.get("parentSpanId"),
                        "attributes": span_attrs,
                        "resource_attributes": resource_attrs,
                        "events": span.get("events") if isinstance(span.get("events"), list) else [],
                        "status": span.get("status") if isinstance(span.get("status"), dict) else None,
                    })
                    counters["spans"] += 1

    return spans, counters
