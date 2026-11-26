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
_TIME_TEXT = re.compile(r"[0-9]{1,20}")
_UINT64_MAX = 2 ** 64 - 1

# Resource bounds: overflow is counted and skipped, never raised. Read as
# module globals at call time so tests can patch them. The first two values
# copy agentscope_importer/contract.py (MAX_BYTES_PER_IMPORT, MAX_SPANS_PER_IMPORT)
# by value; this module does not import them.
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_SPANS = 10_000
MAX_ATTRIBUTES_PER_SPAN = 128


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
    dropped and counted once in ``dropped_count``. Only the first
    ``MAX_ATTRIBUTES_PER_SPAN`` items are decoded; each item past that adds one
    to ``dropped_count`` and is not iterated. A duplicate key keeps the last
    value. A non-list ``attrs`` yields ``({}, 0)``; the caller counts that
    shape as a skipped item.
    """
    out = {}
    dropped = 0
    if not isinstance(attrs, list):
        return out, 0
    limit = MAX_ATTRIBUTES_PER_SPAN
    if len(attrs) > limit:
        dropped += len(attrs) - limit
    for item in attrs[:limit]:
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


def _read_time(value, counters):
    """Normalize a span time to a non-negative int at most 2**64 - 1.

    Accepts a real int (not a bool) or an ASCII digit string. A missing or
    ``None`` value gives ``None`` and is not counted. Any other present value
    gives ``None`` and adds one to ``invalid_timestamps``.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and _TIME_TEXT.fullmatch(value):
        number = int(value)
    else:
        number = None
    if number is None or not 0 <= number <= _UINT64_MAX:
        counters["invalid_timestamps"] += 1
        return None
    return number


def _text_oversize(text):
    """True if the UTF-8 size of ``text`` is over ``MAX_LINE_BYTES``.

    A str that cannot be encoded as UTF-8 (a lone surrogate) is treated as
    oversize, so it is counted as malformed and never raises.
    """
    if len(text) > MAX_LINE_BYTES:
        return True
    try:
        return len(text.encode("utf-8")) > MAX_LINE_BYTES
    except UnicodeEncodeError:
        return True


def flatten_lines(lines):
    """Return ``(spans, counters)`` for the given receiver JSONL lines.

    ``lines`` must be an iterable of lines. A bare ``str`` or ``bytes`` raises
    ``TypeError`` (caller bug). Otherwise this never raises on malformed
    content; it counts and keeps going.

    Each span record has: ``name``, ``traceId``, ``spanId``, ``parentSpanId``,
    ``startTimeUnixNano`` and ``endTimeUnixNano`` (int or None; the keys the
    agentscope mapper reads), ``attributes`` (dict), ``resource_attributes``
    (dict, one copy per span), ``events_count`` (int: the length of the raw
    events list, or 0 when events is absent or not a list; the raw events are
    not kept) and ``status`` (raw dict or None).

    Times: an int (not a bool) or an ASCII digit string at most 2**64 - 1 is
    normalized to int. A missing or ``None`` time is None and not counted.
    Any other present value (negative, bool, float, malformed string, too
    large, list, dict) is None and counted once in ``invalid_timestamps``.

    Per-item rules: a ``bytes`` item over ``MAX_LINE_BYTES`` bytes is checked
    before decoding. ``bytes`` items are decoded as strict UTF-8 (invalid
    UTF-8 is malformed). Any non-``str`` item after that (None, int, ...)
    counts as read and malformed. A ``str`` item whose UTF-8 size is over
    ``MAX_LINE_BYTES`` is oversize. An oversize line counts as read and
    malformed and is never passed to ``json.loads``; a line exactly at the
    limit is accepted. Blank or whitespace-only text lines are skipped and not
    counted. Parsing is strict JSON: ``NaN``, ``Infinity``,
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
    ``dropped_attributes``, and so does each item past the first
    ``MAX_ATTRIBUTES_PER_SPAN`` in an attributes list (resource or span).
    Resource attributes are decoded once per resource entry, span attributes
    once per span.

    Resource bounds (count and skip, never raise): a line over
    ``MAX_LINE_BYTES`` is malformed (see above). Across the whole stream, only
    the first ``MAX_SPANS`` spans are kept; each later span is skipped and
    counted in ``spans_over_limit``, not in ``skipped_items``. The span cap is
    checked right after the span-is-a-dict check.

    Counters are numbers only, never line contents: ``lines_read``
    (non-blank lines seen, oversize lines included), ``malformed_lines``,
    ``spans``, ``skipped_items``, ``dropped_attributes`` (the sum of all
    dropped attribute items), ``invalid_timestamps`` (present span time values
    that were rejected) and ``spans_over_limit`` (spans skipped by the cap).

    BOM: after the size check (and after bytes are decoded), exactly one
    leading U+FEFF character is removed before the line is stripped and parsed. So a
    line with one BOM parses the same as the same line without it, whether it
    arrives as str or as UTF-8 bytes. A second BOM is left in place and the
    line is malformed. A line that is only a BOM is blank after removal and is
    skipped without being counted.
    """
    if isinstance(lines, (str, bytes)):
        raise TypeError("flatten_lines expects an iterable of lines, not str or bytes")

    spans = []
    counters = {
        "lines_read": 0, "malformed_lines": 0, "spans": 0,
        "skipped_items": 0, "dropped_attributes": 0, "invalid_timestamps": 0,
        "spans_over_limit": 0,
    }

    for raw in lines:
        if isinstance(raw, bytes):
            if len(raw) > MAX_LINE_BYTES:
                counters["lines_read"] += 1
                counters["malformed_lines"] += 1
                continue
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
        if _text_oversize(raw):
            counters["lines_read"] += 1
            counters["malformed_lines"] += 1
            continue
        if raw.startswith("\ufeff"):
            raw = raw[1:]
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
                    if counters["spans"] >= MAX_SPANS:
                        counters["spans_over_limit"] += 1
                        continue
                    identity = (
                        span.get("name"), span.get("traceId"),
                        span.get("spanId"), span.get("parentSpanId"),
                    )
                    if any(value is not None and not isinstance(value, str) for value in identity):
                        counters["skipped_items"] += 1
                        continue
                    span_attrs = _read_attrs(span.get("attributes"), counters)
                    start_ns = _read_time(span.get("startTimeUnixNano"), counters)
                    end_ns = _read_time(span.get("endTimeUnixNano"), counters)
                    spans.append({
                        "name": span.get("name"),
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "parentSpanId": span.get("parentSpanId"),
                        "startTimeUnixNano": start_ns,
                        "endTimeUnixNano": end_ns,
                        "attributes": span_attrs,
                        "resource_attributes": dict(resource_attrs),
                        "events_count": len(span["events"]) if isinstance(span.get("events"), list) else 0,
                        "status": span.get("status") if isinstance(span.get("status"), dict) else None,
                    })
                    counters["spans"] += 1

    return spans, counters
