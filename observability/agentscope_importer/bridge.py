"""One-shot, metadata-only Fleet delivery to a literal loopback AgentScope."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from copilot_plugin._sanitize import SAFE_ATTRIBUTE_KEYS, SAFE_ERROR_MESSAGES, SAFE_ERROR_TYPES

from .contract import (
    DEFAULT_KIND,
    FLEET_SCHEMA_VERSION,
    GRAPH_DERIVATION,
    KIND_MAP,
    MAX_BYTES_PER_IMPORT,
    MAX_SPANS_PER_IMPORT,
    SCHEMA_VERSION,
    SOURCE,
    STATS_KEYS,
    STRIPPED_KEYS,
)
from .importer import ImportRefused, ImportSettings, flatten_otlp_payload, import_bytes
from .mapper import canonical_bytes, map_spans, run_id_for

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_READBACK_PAGES = 100
RUN_PAGE_SIZE = 200
SPAN_PAGE_SIZE = 200
MAX_SAFE_STRING_CHARS = 256
PRODUCER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TRACE_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
SPAN_ID_RE = re.compile(r"[0-9a-fA-F]{16}\Z")
SAFE_EVENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SAFE_METADATA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}\Z")
SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}"
    r"|glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})\b"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2})")

# This is the M4 approved span-attribute allowlist plus the mapper's two
# provenance fields and its opaque tool-call correlation id. Unknown keys
# fail closed; STRIPPED_KEYS are intentionally not accepted at this boundary.
ATTRIBUTE_ALLOWLIST = frozenset(SAFE_ATTRIBUTE_KEYS) | frozenset({
    "fleet.span",
    "fleet.trace",
    "gen_ai.tool.call.id",
})
TOP_LEVEL_KEYS = frozenset({"schema_version", "source", "run", "spans", "events"})
RUN_KEYS = frozenset({"native_id", "status", "started_at", "ended_at", "metadata"})
SPAN_KEYS = frozenset({
    "native_id", "parent_native_id", "seq", "kind", "name", "node_name",
    "status", "started_at", "ended_at", "duration_ms", "error", "llm",
    "tool", "attributes",
})
SPAN_NAMES = frozenset({
    "agent.run", "agent.handoff", "subagent.spawn", "llm.call", "tool.exec",
    "hook.exec", "state.write", "approval.request",
})
SPAN_KINDS = frozenset({"run", "llm", "tool", "other"})
SPAN_STATUSES = frozenset({"ok", "error", "cancelled", "unknown"})
RAW_SPAN_KEYS = frozenset({
    "traceId", "spanId", "parentSpanId", "name", "startTimeUnixNano",
    "endTimeUnixNano", "attributes", "status", "events", "resource", "scope", "kind",
})
OTLP_SPAN_KEYS = frozenset({
    "traceId", "spanId", "parentSpanId", "name", "kind", "startTimeUnixNano",
    "endTimeUnixNano", "attributes", "events", "status", "traceState", "flags",
    "droppedAttributesCount", "droppedEventsCount", "droppedLinksCount", "links",
})


class BridgeError(Exception):
    """Safe, printable refusal or delivery failure with a machine outcome."""

    def __init__(self, message, outcome="rejected"):
        super().__init__(message)
        self.outcome = outcome


def _require_synthetic_only(value):
    if value is not True:
        raise BridgeError("delivery requires explicit synthetic-only acknowledgment")


def _require_live_acknowledgment(value):
    if value is not True:
        raise BridgeError("delivery requires explicit live acknowledgment")


# Copilot OTel exporter span names (observed exporter shape: invoke_agent
# root, chat, execute_tool children; service.name copilot-chat) mapped to
# the canonical SPAN_NAMES vocabulary. Live mode renames only these three;
# any other non-canonical name is refused by validation (fail closed).
COPILOT_SPAN_NAMES = {
    "invoke_agent": "agent.run",
    "chat": "llm.call",
    "execute_tool": "tool.exec",
}

# Real Copilot OTel file-exporter shape (observed 2026-11-09,
# copilot-otel.jsonl): JSONL of mixed span + metric records. Span
# records carry [seconds, nanoseconds] time pairs, an
# instrumentationScope, and SDK fields the flat-span contract does not
# use. Only the keys below are recognized on a span record; anything
# else rejects that record (fail closed, counted in the normalization
# report — never silent). Metric records carry scopeMetrics and are
# counted and excluded from the span mapper; they are not imported.
EXPORTER_SPAN_KEYS = frozenset({
    "traceId", "spanId", "parentSpanId", "name", "startTime", "endTime",
    "duration", "ended", "events", "attributes", "status", "resource",
    "instrumentationScope", "kind", "traceFlags",
    "droppedAttributesCount", "droppedEventsCount", "droppedLinksCount",
    "links",
})
# A span record carrying any of these keys is a real-exporter record,
# not an ordinary flat span. Ordinary flat spans (startTimeUnixNano,
# canonical names) keep the existing strict path.
EXPORTER_SPAN_MARKERS = frozenset({
    "startTime", "endTime", "duration", "instrumentationScope",
    "traceFlags", "ended", "droppedAttributesCount", "droppedEventsCount",
    "droppedLinksCount", "links",
})
EXPORTER_SCOPE_KEYS = frozenset(
    {"name", "version", "attributes", "droppedAttributesCount"}
)
EXPORTER_RESOURCE_KEYS = frozenset({"attributes", "droppedAttributesCount"})
EXPORTER_METRIC_KEYS = frozenset({"resource", "scopeMetrics"})
# OTLP log records (observed 2026-11-09, copilot-otelrun2.jsonl): flat
# log shape, no scopeLogs wrapper. Counted and excluded from the span
# mapper; never imported. Only the keys below are recognized; anything
# else rejects that record (fail closed, counted in the report).
EXPORTER_LOG_KEYS = frozenset({
    "attributes", "body", "droppedAttributesCount", "hrTime",
    "hrTimeObserved", "instrumentationScope", "resource",
})
# Largest [seconds, nanos] pair convertible to an ISO timestamp: the
# mapper's ns_to_iso uses datetime.fromtimestamp, which overflows past
# year 9999. Bounding seconds here keeps conversion total.
MAX_HR_SECONDS = 253402300799  # 9999-12-31T23:59:59Z
# The exporter's unclassified error.type value. It is mapped to a
# bounded SAFE_ERROR_TYPES class (never carried verbatim, never used
# to invent a more specific failure than the span kind supports).
REAL_EXPORTER_ERROR_TYPE = "Error"


class _RealExporterReject(Exception):
    """One real-exporter record is malformed or unsupported.

    Carries only a short machine reason (no raw values, no error
    strings); the classifier records the reason and the record's
    trace/span ids in the normalization report and moves on.
    """


def _normalize_live_attributes(attrs, allowed=ATTRIBUTE_ALLOWLIST):
    """Drop attribute entries whose key is outside `allowed`.

    Dict-form attributes (flat spans) lose the key; list-form OTLP
    attributes lose the entry. Returns the number of dropped entries.
    Values are still validated by the existing allowlist checks, so an
    allowlisted key with an unsafe value still refuses the delivery.
    """
    dropped = 0
    if isinstance(attrs, dict):
        for key in list(attrs):
            if key not in allowed:
                del attrs[key]
                dropped += 1
        return dropped
    if isinstance(attrs, list):
        kept = []
        for entry in attrs:
            if isinstance(entry, dict) and entry.get("key") in allowed:
                kept.append(entry)
            else:
                dropped += 1
        attrs[:] = kept
        return dropped
    return 0


def _normalize_live_span(span):
    """Rename one raw span to the canonical vocabulary (live mode only).

    Returns (renamed, dropped_attributes). A Copilot exporter name is
    renamed to its canonical name; any other non-canonical name is left
    alone so validation refuses it with the existing error.
    """
    renamed = 0
    name = span.get("name") if isinstance(span, dict) else None
    if isinstance(name, str) and name in COPILOT_SPAN_NAMES:
        span["name"] = COPILOT_SPAN_NAMES[name]
        renamed = 1
    dropped = 0
    if isinstance(span, dict) and "attributes" in span:
        dropped += _normalize_live_attributes(span["attributes"])
    return renamed, dropped


def _normalize_live_otlp(payload):
    """Normalize a raw OTLP/HTTP JSON payload for live delivery.

    Renames Copilot exporter span names and drops unapproved attribute
    entries (resource, scope, and span level). Returns
    (renamed_spans, dropped_attributes).
    """
    renamed = 0
    dropped = 0
    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list):
        return renamed, dropped
    for resource_span in resource_spans:
        if not isinstance(resource_span, dict):
            continue
        resource = resource_span.get("resource")
        if isinstance(resource, dict) and "attributes" in resource:
            dropped += _normalize_live_attributes(
                resource["attributes"], allowed={"service.name", "service.version"}
            )
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            scope = scope_span.get("scope")
            if isinstance(scope, dict) and "attributes" in scope:
                dropped += _normalize_live_attributes(scope["attributes"])
            for span in scope_span.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                count_renamed, count_dropped = _normalize_live_span(span)
                renamed += count_renamed
                dropped += count_dropped
    return renamed, dropped


def _hr_time_to_ns(value, where):
    """[seconds, nanoseconds] pair -> Unix nanoseconds (validated).

    Both elements must be plain ints; seconds must be in range and
    nanoseconds in 0..999,999,999. Anything else rejects the record.
    """
    if (
        not isinstance(value, (list, tuple)) or len(value) != 2
        or isinstance(value[0], bool) or not isinstance(value[0], int)
        or isinstance(value[1], bool) or not isinstance(value[1], int)
        or value[0] < 0 or value[0] > MAX_HR_SECONDS
        or value[1] < 0 or value[1] > 999_999_999
    ):
        raise _RealExporterReject("malformed_timestamp")
    return value[0] * 1_000_000_000 + value[1]


def _real_exporter_error_message(kind):
    """Bounded safe error message for a failed real-exporter span.

    The exporter's raw status message is never carried; the failure
    fact survives as status error plus this approved message.
    """
    if kind == "llm":
        return "LLM call failed; details withheld"
    if kind == "tool":
        return "Tool failed; details withheld"
    return "Error; details withheld"


def _real_exporter_error_type(kind):
    """Bounded safe error.type for the exporter's unclassified "Error"."""
    return "LLMError" if kind == "llm" else "Unknown"


def _normalize_real_exporter_span(span, report):
    """Convert one real-exporter span record to the approved flat shape.

    Returns a flat span dict whose keys are a subset of RAW_SPAN_KEYS,
    ready for the existing _validate_flat_spans + map_spans path.
    Raises _RealExporterReject with a short machine reason when the
    record is malformed or unsupported; the caller records the reason
    and skips the record. Nothing raw is carried forward: error
    strings are replaced by bounded classes, exception events (and
    their stack traces) are dropped and counted, and non-allowlisted
    attributes are dropped and counted.
    """
    if not isinstance(span, dict):
        raise _RealExporterReject("not_an_object")
    if set(span) - EXPORTER_SPAN_KEYS:
        raise _RealExporterReject("unapproved_span_field")
    attrs = span.get("attributes")
    if not isinstance(attrs, dict):
        raise _RealExporterReject("malformed_attributes")

    # Gap A — narrow model-suffixed chat name. The name must be exactly
    # "chat <model>" with the model taken from the span's own
    # gen_ai.request.model attribute and gen_ai.operation.name == "chat".
    # No wildcard rename: any other non-canonical name rejects the record.
    # Gap A (run2) — narrow model-suffixed embeddings name, same shape:
    # "embeddings <model>" with gen_ai.operation.name == "embeddings"
    # normalizes to llm.call (it is a model invocation). It is NEVER
    # labeled tool.exec; the operation name survives in the carried
    # allowlisted attributes, so provenance is kept.
    raw_name = span.get("name")
    operation = attrs.get("gen_ai.operation.name")
    model = attrs.get("gen_ai.request.model")
    # The model value feeds the rename decision, so an unsafe value
    # refuses the whole snapshot here (fail closed, like the flat path)
    # instead of hiding inside a per-record rejection.
    if isinstance(model, str):
        _safe_text(model, "model name")
    if isinstance(raw_name, str) and raw_name in COPILOT_SPAN_NAMES:
        name = COPILOT_SPAN_NAMES[raw_name]
        report["renamed_spans"] += 1
    elif isinstance(raw_name, str) and raw_name in SPAN_NAMES:
        name = raw_name
    elif (
        isinstance(raw_name, str)
        and operation == "chat"
        and isinstance(model, str) and model
        and raw_name == "chat " + model
    ):
        name = "llm.call"
        report["renamed_spans"] += 1
        report["model_suffixed_renames"] += 1
    elif (
        isinstance(raw_name, str)
        and operation == "embeddings"
        and isinstance(model, str) and model
        and raw_name == "embeddings " + model
    ):
        name = "llm.call"
        report["renamed_spans"] += 1
        report["model_suffixed_renames"] += 1
        report["embeddings_renames"] += 1
    else:
        raise _RealExporterReject("unsupported_span_name")
    kind = KIND_MAP.get(name, DEFAULT_KIND)

    # Gap B — [seconds, nanoseconds] pairs to Unix nanoseconds.
    # Never substitute wall-clock ingestion time.
    if "startTime" not in span or "endTime" not in span:
        raise _RealExporterReject("missing_timestamp")
    start_ns = _hr_time_to_ns(span["startTime"], "span start")
    end_ns = _hr_time_to_ns(span["endTime"], "span end")
    if end_ns < start_ns:
        raise _RealExporterReject("end_before_start")
    if "duration" in span and span["duration"] is not None:
        duration_ns = _hr_time_to_ns(span["duration"], "span duration")
        if duration_ns != end_ns - start_ns:
            raise _RealExporterReject("duration_mismatch")

    # Gap D — SDK fields: validated, then dropped (the mapper recomputes
    # duration from start/end and never uses these).
    if span.get("ended") is not True:
        raise _RealExporterReject("span_not_ended")
    for field in ("droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"):
        count = span.get(field, 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _RealExporterReject("malformed_dropped_count")
        if count > 0:
            raise _RealExporterReject("upstream_dropped_data")
    report["dropped_counts_verified_zero"] += 1
    links = span.get("links", [])
    if not isinstance(links, list):
        raise _RealExporterReject("malformed_links")
    if links:
        raise _RealExporterReject("span_links_not_approved")
    flags = span.get("traceFlags", 0)
    if isinstance(flags, bool) or not isinstance(flags, int) or not 0 <= flags <= 255:
        raise _RealExporterReject("malformed_trace_flags")
    sdk_kind = span.get("kind")
    if sdk_kind is not None and (isinstance(sdk_kind, bool) or not isinstance(sdk_kind, int)):
        raise _RealExporterReject("malformed_span_kind")

    # Gap D — instrumentationScope -> flat scope {name, version}.
    # The flat contract carries no scope attributes, so any present are
    # dropped and counted.
    flat_scope = None
    scope = span.get("instrumentationScope")
    if scope is not None:
        if not isinstance(scope, dict) or set(scope) - EXPORTER_SCOPE_KEYS:
            raise _RealExporterReject("malformed_scope")
        scope_dropped = scope.get("droppedAttributesCount", 0)
        if isinstance(scope_dropped, bool) or not isinstance(scope_dropped, int) or scope_dropped < 0:
            raise _RealExporterReject("malformed_dropped_count")
        if scope_dropped > 0:
            raise _RealExporterReject("upstream_dropped_data")
        flat_scope = {}
        for field in ("name", "version"):
            value = scope.get(field)
            if value is not None:
                _safe_text(value, "scope")
                flat_scope[field] = value
        scope_attrs = scope.get("attributes")
        if scope_attrs is not None:
            if isinstance(scope_attrs, dict):
                report["dropped_attributes"] += len(scope_attrs)
            elif isinstance(scope_attrs, list):
                report["dropped_attributes"] += len(scope_attrs)
            else:
                raise _RealExporterReject("malformed_scope")

    # Gap D — resource: keep only service.name / service.version.
    # Session ids and SDK internals are dropped and counted, never
    # promoted to span attributes.
    flat_resource = None
    resource = span.get("resource")
    if resource is not None:
        if not isinstance(resource, dict) or set(resource) - EXPORTER_RESOURCE_KEYS:
            raise _RealExporterReject("malformed_resource")
        resource_dropped = resource.get("droppedAttributesCount", 0)
        if isinstance(resource_dropped, bool) or not isinstance(resource_dropped, int) or resource_dropped < 0:
            raise _RealExporterReject("malformed_dropped_count")
        if resource_dropped > 0:
            raise _RealExporterReject("upstream_dropped_data")
        res_attrs = resource.get("attributes")
        kept_resource = {}
        if res_attrs is not None:
            if isinstance(res_attrs, dict):
                for key, value in res_attrs.items():
                    if key in {"service.name", "service.version"}:
                        _safe_text(value, "resource")
                        kept_resource[key] = value
                    else:
                        report["dropped_attributes"] += 1
            elif isinstance(res_attrs, list):
                for entry in res_attrs:
                    if not isinstance(entry, dict) or set(entry) - {"key", "value"}:
                        raise _RealExporterReject("malformed_resource")
                    key = entry.get("key")
                    if key in {"service.name", "service.version"}:
                        try:
                            value = _decode_otlp_value(entry.get("value"), "resource")
                        except BridgeError:
                            raise _RealExporterReject("malformed_resource") from None
                        _safe_text(value, "resource")
                        kept_resource[key] = value
                    else:
                        report["dropped_attributes"] += 1
            else:
                raise _RealExporterReject("malformed_resource")
        if kept_resource:
            flat_resource = {"attributes": kept_resource}

    # Gaps A/D/E — span attributes: allowlist only, stripped keys dropped.
    # error.type "Error" maps to a bounded class; error.message strings
    # map to a bounded safe message. Anything else unsafe is refused
    # downstream by _validate_flat_spans (fail closed).
    kept_attrs = {}
    for key, value in attrs.items():
        if key in STRIPPED_KEYS or key not in ATTRIBUTE_ALLOWLIST:
            report["dropped_attributes"] += 1
            continue
        if key == "error.type":
            if not isinstance(value, str):
                raise _RealExporterReject("malformed_error_type")
            if value in SAFE_ERROR_TYPES:
                kept_attrs[key] = value
            elif value == REAL_EXPORTER_ERROR_TYPE:
                kept_attrs[key] = _real_exporter_error_type(kind)
                report["error_types_mapped"] += 1
            else:
                raise _RealExporterReject("unapproved_error_type")
            continue
        if key == "error.message":
            if not isinstance(value, str):
                raise _RealExporterReject("malformed_error_message")
            if value in SAFE_ERROR_MESSAGES:
                kept_attrs[key] = value
            else:
                kept_attrs[key] = _real_exporter_error_message(kind)
                report["error_messages_sanitized"] += 1
            continue
        kept_attrs[key] = value

    # Gap E — status: the error code is preserved; a raw message is
    # replaced by the bounded safe message for the span kind.
    flat_status = None
    status = span.get("status")
    if status is not None:
        if not isinstance(status, dict) or set(status) - {"code", "message"}:
            raise _RealExporterReject("malformed_status")
        code = status.get("code", 0)
        if isinstance(code, bool) or code not in (0, 1, 2):
            raise _RealExporterReject("unapproved_status")
        message = status.get("message")
        if message is not None:
            if not isinstance(message, str):
                raise _RealExporterReject("malformed_status")
            if message in SAFE_ERROR_MESSAGES:
                flat_status = {"code": code, "message": message}
            elif code == 2:
                flat_status = {"code": code, "message": _real_exporter_error_message(kind)}
                report["error_messages_sanitized"] += 1
            else:
                raise _RealExporterReject("unapproved_span_error_message")
        else:
            flat_status = {"code": code}

    # Gap E — events: exception events are sanitized away (their stack
    # traces never enter any document) and counted; every other event
    # is dropped and counted. The failure fact survives on the span
    # status and bounded error type. The bridge refuses event-bearing
    # documents (no event readback), so no event is carried forward.
    events = span.get("events", [])
    if not isinstance(events, list):
        raise _RealExporterReject("malformed_events")
    for event in events:
        if not isinstance(event, dict):
            raise _RealExporterReject("malformed_events")
        if event.get("name") == "exception":
            report["exception_events_sanitized"] += 1
        else:
            report["events_dropped"] += 1

    flat = {
        "traceId": span.get("traceId"),
        "spanId": span.get("spanId"),
        "name": name,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": end_ns,
        "attributes": kept_attrs,
    }
    parent = span.get("parentSpanId")
    if parent is not None:
        flat["parentSpanId"] = parent
    if flat_status is not None:
        flat["status"] = flat_status
    if flat_resource is not None:
        flat["resource"] = flat_resource
    if flat_scope is not None:
        flat["scope"] = flat_scope
    return flat


def _validate_real_exporter_log(record):
    """Validate an OTLP log record's shape; the record is counted and skipped.

    Log records are never imported, so no content is carried forward —
    only the shape is checked. Raises _RealExporterReject with a short
    machine reason when the record is malformed.
    """
    if not isinstance(record, dict):
        raise _RealExporterReject("not_an_object")
    if set(record) - EXPORTER_LOG_KEYS:
        raise _RealExporterReject("unapproved_log_field")
    for field in ("hrTime", "hrTimeObserved"):
        value = record.get(field)
        if value is not None:
            _hr_time_to_ns(value, "log time")
    dropped = record.get("droppedAttributesCount", 0)
    if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
        raise _RealExporterReject("malformed_log_record")
    body = record.get("body")
    if body is not None and not isinstance(body, str):
        raise _RealExporterReject("malformed_log_record")
    attrs = record.get("attributes")
    if attrs is not None and not isinstance(attrs, dict):
        raise _RealExporterReject("malformed_log_record")
    scope = record.get("instrumentationScope")
    if scope is not None and (
        not isinstance(scope, dict) or set(scope) - EXPORTER_SCOPE_KEYS
    ):
        raise _RealExporterReject("malformed_log_record")
    resource = record.get("resource")
    if resource is not None and not isinstance(resource, dict):
        raise _RealExporterReject("malformed_log_record")


def _normalize_live_records(records, normalization):
    """Classify live JSONL/list records and normalize them in place.

    Returns the kept flat spans. Metric records are recognized,
    counted, and excluded from the span mapper; log records (hrTime +
    body shape) are recognized, counted, and excluded the same way.
    Exporter-shaped span records are normalized per record; a malformed
    or unsupported one is rejected with its reason in the report (never
    silent) and the run continues with the accepted spans. Ordinary flat
    spans take the existing strict path (unknown names and unsafe values
    still refuse the whole snapshot via _validate_flat_spans). Non-dict
    records are rejected, not treated as spans.
    """
    kept = []
    for record in records:
        if not isinstance(record, dict):
            normalization["spans_rejected"] += 1
            normalization["rejected"].append(
                {"reason": "not_an_object", "trace_id": None, "span_id": None}
            )
            continue
        if (
            "traceId" in record and "spanId" in record
            and set(record) & EXPORTER_SPAN_MARKERS
        ):
            normalization["real_exporter_records"] += 1
            try:
                kept.append(_normalize_real_exporter_span(record, normalization))
                normalization["spans_accepted"] += 1
            except _RealExporterReject as exc:
                normalization["spans_rejected"] += 1
                normalization["rejected"].append({
                    "reason": str(exc),
                    "trace_id": record.get("traceId") if isinstance(record.get("traceId"), str) else None,
                    "span_id": record.get("spanId") if isinstance(record.get("spanId"), str) else None,
                })
            continue
        if "scopeMetrics" in record:
            if set(record) - EXPORTER_METRIC_KEYS:
                normalization["spans_rejected"] += 1
                normalization["rejected"].append(
                    {"reason": "unapproved_metric_field", "trace_id": None, "span_id": None}
                )
                continue
            scope_metrics = record.get("scopeMetrics")
            if not isinstance(scope_metrics, list):
                normalization["spans_rejected"] += 1
                normalization["rejected"].append(
                    {"reason": "malformed_metric_record", "trace_id": None, "span_id": None}
                )
                continue
            normalization["real_exporter_records"] += 1
            normalization["metric_records"] += 1
            normalization["metrics_skipped"] += 1
            for entry in scope_metrics:
                metrics = entry.get("metrics") if isinstance(entry, dict) else None
                if isinstance(metrics, list):
                    normalization["metric_descriptors"] += len(metrics)
            continue
        if "hrTime" in record and "body" in record:
            # Gap C (run2) — OTLP log record: recognized, counted, and
            # excluded from the span mapper. No log content is carried
            # forward; a malformed log is rejected with its reason.
            try:
                _validate_real_exporter_log(record)
            except _RealExporterReject as exc:
                normalization["spans_rejected"] += 1
                normalization["rejected"].append(
                    {"reason": str(exc), "trace_id": None, "span_id": None}
                )
                continue
            normalization["real_exporter_records"] += 1
            normalization["log_records"] += 1
            normalization["logs_skipped"] += 1
            continue
        if "traceId" not in record or "spanId" not in record:
            # Neither a span nor a metric: reject with a reason instead
            # of letting validation refuse the whole snapshot. (Live
            # mode only; non-live input still refuses such records.)
            normalization["spans_rejected"] += 1
            normalization["rejected"].append(
                {"reason": "unrecognized_record", "trace_id": None, "span_id": None}
            )
            continue
        renamed, dropped = _normalize_live_span(record)
        normalization["renamed_spans"] += renamed
        normalization["dropped_attributes"] += dropped
        kept.append(record)
    normalization["records_total"] += len(records)
    return kept


def validate_server(url):
    """Require explicit plain-HTTP loopback IP and reject URL ambiguity."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        address = ipaddress.ip_address(parsed.hostname or "")
    except (TypeError, ValueError):
        raise BridgeError("server must use a literal loopback IP and explicit port") from None
    if (
        parsed.scheme.lower() != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
        or "%" in (parsed.hostname or "")
        or not address.is_loopback
    ):
        raise BridgeError("server must use plain HTTP on a literal loopback IP and explicit port")
    return parsed


def snapshot_source(path, max_bytes=MAX_BYTES_PER_IMPORT):
    """Read one bounded immutable source snapshot; never reopen it later."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise BridgeError("invalid input size limit")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise BridgeError("input file could not be read") from None
    if len(raw) > max_bytes:
        raise BridgeError("input exceeds configured size limit")
    return raw


def _keys(value, allowed, where):
    if not isinstance(value, dict) or set(value) - allowed:
        raise BridgeError("unapproved or malformed fields in " + where)


def _safe_text(value, where, limit=MAX_SAFE_STRING_CHARS, allow_relative_branch=False):
    safe_format = SAFE_BRANCH_RE if allow_relative_branch else SAFE_METADATA_RE
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        or EMAIL_RE.search(value)
        or value.lower().startswith(("bearer ", "sk-"))
        or SECRET_VALUE_RE.search(value)
        or ABSOLUTE_PATH_RE.search(value)
        or not safe_format.fullmatch(value)
        or allow_relative_branch and any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise BridgeError("unsafe or malformed metadata in " + where)
    return value


def _timestamp(value, where):
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BridgeError("malformed timestamp in " + where)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise BridgeError("malformed timestamp in " + where) from None
    if parsed.utcoffset() is None:
        raise BridgeError("malformed timestamp in " + where)
    return parsed


def _attribute_value(key, value, where):
    if key not in ATTRIBUTE_ALLOWLIST:
        raise BridgeError("unapproved attribute key in " + where)
    if key == "error.message":
        if not isinstance(value, str) or value not in SAFE_ERROR_MESSAGES:
            raise BridgeError("unapproved error message")
        return
    if key in {
        "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens",
        "hermes.attempt", "copilot.turn_count",
    }:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BridgeError("malformed numeric metadata in " + where)
        return
    if key in {"copilot.time_to_first_token_ms", "copilot.hook.duration_s"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise BridgeError("malformed numeric metadata in " + where)
        return
    if key in {"handoff.child_parented", "handoff.child_error", "synthetic"}:
        if not isinstance(value, (bool, str)) or (isinstance(value, str) and value not in ("true", "false")):
            raise BridgeError("malformed boolean metadata in " + where)
        return
    if not isinstance(value, str):
        raise BridgeError("malformed string metadata in " + where)
    _safe_text(value, where, allow_relative_branch=(key == "copilot.git.branch"))
    if key == "error.type" and value not in SAFE_ERROR_TYPES:
        raise BridgeError("unapproved error type")


def _validate_attributes(attrs, where):
    if not isinstance(attrs, dict):
        raise BridgeError("malformed attributes in " + where)
    for key, value in attrs.items():
        if not isinstance(key, str):
            raise BridgeError("malformed attribute key in " + where)
        _attribute_value(key, value, where)


def _decode_otlp_value(value, where, attribute_key=None):
    if not isinstance(value, dict) or len(value) != 1:
        raise BridgeError("malformed OTLP attribute value in " + where)
    key, item = next(iter(value.items()))
    if key == "stringValue":
        if (
            attribute_key == "error.message"
            and isinstance(item, str)
            and item in SAFE_ERROR_MESSAGES
        ):
            return item
        return _safe_text(item, where)
    if key == "intValue":
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise BridgeError("malformed OTLP numeric value in " + where)
        if isinstance(item, str) and not re.fullmatch(r"-?[0-9]+", item):
            raise BridgeError("malformed OTLP numeric value in " + where)
        try:
            return int(item)
        except (TypeError, ValueError):
            raise BridgeError("malformed OTLP numeric value in " + where) from None
    if key == "boolValue" and isinstance(item, bool):
        return item
    if key == "doubleValue" and isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
        return item
    raise BridgeError("unsupported OTLP attribute value in " + where)


def _validate_otlp_attrs(items, where, resource=False):
    if not isinstance(items, list):
        raise BridgeError("malformed OTLP attributes in " + where)
    seen = set()
    result = {}
    for entry in items:
        _keys(entry, {"key", "value"}, where)
        key = entry.get("key")
        if not isinstance(key, str) or key in seen:
            raise BridgeError("malformed or duplicate OTLP attribute key in " + where)
        seen.add(key)
        value = _decode_otlp_value(entry.get("value"), where, key)
        if resource:
            if key not in {"service.name", "service.version"}:
                raise BridgeError("unapproved resource metadata")
            _safe_text(value, where)
        else:
            _attribute_value(key, value, where)
        result[key] = value
    return result


def _validate_raw_events(events, where):
    if not isinstance(events, list):
        raise BridgeError("malformed events in " + where)
    for event in events:
        _keys(event, {"name", "timeUnixNano", "time", "attributes"}, "event")
        name = event.get("name")
        if not isinstance(name, str) or not SAFE_EVENT_RE.fullmatch(name):
            raise BridgeError("unapproved event name")
        if "timeUnixNano" in event and event["timeUnixNano"] is not None:
            _raw_time(event["timeUnixNano"], "event")
        if "time" in event and event["time"] is not None:
            _timestamp(event["time"], "event")
        attrs = event.get("attributes", {})
        if isinstance(attrs, list):
            _validate_otlp_attrs(attrs, "event")
        else:
            _validate_attributes(attrs, "event")


def _raw_time(value, where):
    if isinstance(value, bool) or not (
        isinstance(value, int) and value >= 0
        or isinstance(value, str) and value.isdecimal()
    ):
        raise BridgeError("malformed timestamp in " + where)


def _validate_flat_spans(spans):
    if not isinstance(spans, list) or len(spans) > MAX_SPANS_PER_IMPORT:
        raise BridgeError("span input is malformed or exceeds the configured limit")
    for span in spans:
        if not isinstance(span, dict):
            continue  # The M5 importer accounts for unreadable non-object entries.
        _keys(span, RAW_SPAN_KEYS, "span input")
        trace = span.get("traceId")
        native = span.get("spanId")
        if not isinstance(trace, str) or not TRACE_ID_RE.fullmatch(trace):
            raise BridgeError("trace identity is not an approved 32-hex identifier")
        if not isinstance(native, str) or not SPAN_ID_RE.fullmatch(native):
            raise BridgeError("span identity is not an approved 16-hex identifier")
        parent = span.get("parentSpanId")
        if parent not in (None, "") and (not isinstance(parent, str) or not SPAN_ID_RE.fullmatch(parent)):
            raise BridgeError("parent identity is not an approved 16-hex identifier")
        name = span.get("name")
        if not isinstance(name, str) or name not in SPAN_NAMES:
            raise BridgeError("unapproved span name")
        for field in ("startTimeUnixNano", "endTimeUnixNano"):
            if field in span and span[field] is not None:
                _raw_time(span[field], "span")
        attrs = span.get("attributes", {})
        if isinstance(attrs, list):
            _validate_otlp_attrs(attrs, "span")
        else:
            _validate_attributes(attrs, "span")
        status = span.get("status")
        if isinstance(status, dict):
            _keys(status, {"code", "message"}, "span status")
            code = status.get("code", 0)
            if isinstance(code, bool) or code not in (0, 1, 2):
                raise BridgeError("unapproved span status")
            message = status.get("message")
            if message is not None and (
                not isinstance(message, str) or message not in SAFE_ERROR_MESSAGES
            ):
                raise BridgeError("unapproved span error message")
        elif status is not None and status not in SPAN_STATUSES:
            raise BridgeError("unapproved span status")
        if "events" in span:
            _validate_raw_events(span["events"], "span")
        if "resource" in span:
            resource = span["resource"]
            if not isinstance(resource, dict):
                raise BridgeError("malformed resource metadata")
            if "attributes" in resource:
                _keys(resource, {"attributes"}, "resource")
                resource_attrs = resource.get("attributes", {})
                if isinstance(resource_attrs, list):
                    _validate_otlp_attrs(resource_attrs, "resource", resource=True)
                elif isinstance(resource_attrs, dict):
                    for key, value in resource_attrs.items():
                        if key not in {"service.name", "service.version"}:
                            raise BridgeError("unapproved resource metadata")
                        _safe_text(value, "resource")
                else:
                    raise BridgeError("malformed resource metadata")
            else:
                for key, value in resource.items():
                    if key not in {"service.name", "service.version"}:
                        raise BridgeError("unapproved resource metadata")
                    _safe_text(value, "resource")
        if "scope" in span:
            scope = span["scope"]
            _keys(scope, {"name", "version"}, "scope")
            for value in scope.values():
                if value is not None:
                    _safe_text(value, "scope")


def _validate_otlp_payload(payload):
    _keys(payload, {"resourceSpans"}, "OTLP document")
    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list):
        raise BridgeError("malformed OTLP document")
    for resource_span in resource_spans:
        _keys(resource_span, {"resource", "scopeSpans"}, "OTLP resource span")
        resource = resource_span.get("resource", {})
        _keys(resource, {"attributes", "droppedAttributesCount"}, "OTLP resource")
        if "attributes" in resource:
            _validate_otlp_attrs(resource["attributes"], "resource", resource=True)
        scope_spans = resource_span.get("scopeSpans")
        if not isinstance(scope_spans, list):
            raise BridgeError("malformed OTLP scope spans")
        for scope_span in scope_spans:
            _keys(scope_span, {"scope", "spans", "schemaUrl", "schema_url"}, "OTLP scope span")
            scope = scope_span.get("scope", {})
            _keys(scope, {"name", "version", "attributes", "droppedAttributesCount"}, "OTLP scope")
            for field in ("name", "version"):
                if scope.get(field) is not None:
                    _safe_text(scope[field], "scope")
            if "attributes" in scope:
                _validate_otlp_attrs(scope["attributes"], "scope")
            raw_spans = scope_span.get("spans")
            if not isinstance(raw_spans, list) or len(raw_spans) > MAX_SPANS_PER_IMPORT:
                raise BridgeError("malformed or oversized OTLP span list")
            for span in raw_spans:
                _keys(span, OTLP_SPAN_KEYS, "OTLP span")
                if "attributes" in span:
                    _validate_otlp_attrs(span["attributes"], "OTLP span")
                if "events" in span:
                    _validate_raw_events(span["events"], "OTLP span")
                if "links" in span and span["links"]:
                    raise BridgeError("OTLP span links are not approved for delivery")
                if "status" in span:
                    _keys(span["status"], {"code", "message"}, "OTLP status")
    flattened = flatten_otlp_payload(payload)
    _validate_flat_spans(flattened)
    return flattened


def _parse_source_snapshot(raw, producer, live=False):
    """Parse one bounded source snapshot into canonical documents.

    Live mode normalizes raw Copilot exporter observations before
    validation: Copilot OTel span names are renamed to the canonical
    vocabulary and attribute entries outside the allowlist are dropped
    (counted, never silent). Live JSONL/list input is classified per
    record: metric records are counted and skipped, log records are
    counted and skipped, real-exporter span records are converted from
    the observed exporter shape ([seconds, nanos] times, model-suffixed
    chat and embeddings names, SDK fields, bounded error classes) with
    per-record rejection reasons in the normalization report, and
    ordinary flat spans keep the existing strict path.
    Canonical documents are never normalized; they must already be
    valid. Returns (kind, parsed, documents, normalization) where
    normalization carries the renamed/dropped counts plus the
    real-exporter accounting (records, metrics/logs skipped, spans
    accepted/rejected with reasons, error mappings, event sanitizing).
    For JSONL input, parsed is (kept_spans, unreadable_lines) with the
    kept spans already normalized.
    """
    normalization = {
        "renamed_spans": 0,
        "dropped_attributes": 0,
        # Real-exporter accounting (Gap C): populated only when live
        # JSONL/list input contains exporter-shaped records or metric
        # records. All counts are exact; "rejected" carries only short
        # machine reasons plus trace/span ids — never raw values or
        # error strings.
        "records_total": 0,
        "real_exporter_records": 0,
        "metric_records": 0,
        "metrics_skipped": 0,
        "metric_descriptors": 0,
        "log_records": 0,
        "logs_skipped": 0,
        "spans_accepted": 0,
        "spans_rejected": 0,
        "rejected": [],
        "unreadable_lines": 0,
        "model_suffixed_renames": 0,
        "embeddings_renames": 0,
        "error_types_mapped": 0,
        "error_messages_sanitized": 0,
        "exception_events_sanitized": 0,
        "events_dropped": 0,
        "dropped_counts_verified_zero": 0,
    }
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, dict) and "schema_version" in parsed and "run" in parsed:
        document_bytes = canonical_bytes(parsed)
        validate_artifact(parsed, document_bytes)
        return "canonical", parsed, [parsed], normalization
    if isinstance(parsed, dict) and "resourceSpans" in parsed:
        if live:
            renamed, dropped = _normalize_live_otlp(parsed)
            normalization["renamed_spans"] += renamed
            normalization["dropped_attributes"] += dropped
        spans = _validate_otlp_payload(parsed)
        mapped = map_spans(spans, producer)
        documents = [document for _, document in mapped["documents"]]
        for document in documents:
            validate_artifact(document, canonical_bytes(document))
        return "otlp", parsed, documents, normalization
    if isinstance(parsed, list):
        if live:
            parsed = _normalize_live_records(parsed, normalization)
        _validate_flat_spans(parsed)
        mapped = map_spans(parsed, producer)
        documents = [document for _, document in mapped["documents"]]
        for document in documents:
            validate_artifact(document, canonical_bytes(document))
        return "spans", parsed, documents, normalization
    if parsed is not None:
        return "unsupported", parsed, [], normalization

    text = raw.decode("utf-8", errors="replace")
    spans = []
    unreadable_lines = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            spans.append(json.loads(line))
        except ValueError:
            unreadable_lines += 1
    if live:
        spans = _normalize_live_records(spans, normalization)
    normalization["unreadable_lines"] = unreadable_lines
    _validate_flat_spans(spans)
    mapped = map_spans(spans, producer)
    documents = [document for _, document in mapped["documents"]]
    for document in documents:
        validate_artifact(document, canonical_bytes(document))
    return "jsonl", (spans, unreadable_lines), documents, normalization


def _validate_canonical_document(doc, raw, max_bytes):
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise BridgeError("artifact exceeds configured size limit")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BridgeError("artifact is not valid UTF-8 JSON") from None
    if not isinstance(doc, dict) or parsed != doc or canonical_bytes(parsed) != raw:
        raise BridgeError("artifact/document mismatch or non-canonical upload bytes")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise BridgeError("unsupported canonical schema version")
    _keys(doc, TOP_LEVEL_KEYS, "document")
    if doc.get("source") != SOURCE:
        raise BridgeError("unapproved source metadata")
    run = doc.get("run")
    spans = doc.get("spans")
    if not isinstance(run, dict):
        raise BridgeError("malformed run object")
    if not isinstance(spans, list):
        raise BridgeError("malformed span collection")
    _keys(run, RUN_KEYS, "run")
    if len(spans) > MAX_SPANS_PER_IMPORT:
        raise BridgeError("malformed or oversized span collection")
    events = doc.get("events", [])
    if events != []:
        raise BridgeError(
            "event-bearing artifacts are refused because AgentScope exposes no event readback"
        )

    trace = run.get("native_id")
    if not isinstance(trace, str) or not TRACE_ID_RE.fullmatch(trace):
        raise BridgeError("run identity is not an approved 32-hex identifier")
    if not isinstance(run.get("status"), str) or run.get("status") not in SPAN_STATUSES:
        raise BridgeError("unapproved run status")
    start = _timestamp(run.get("started_at"), "run")
    end = _timestamp(run.get("ended_at"), "run")
    metadata = run.get("metadata")
    if not isinstance(metadata, dict):
        raise BridgeError("unapproved run provenance metadata")
    _keys(metadata, {"fleet_schema_version", "producer", "trace_id", "graph"}, "run metadata")
    if (
        metadata.get("fleet_schema_version") != FLEET_SCHEMA_VERSION
        or metadata.get("trace_id") != trace
        or metadata.get("graph") != GRAPH_DERIVATION
        or not isinstance(metadata.get("producer"), str)
        or not PRODUCER_RE.fullmatch(metadata["producer"])
    ):
        raise BridgeError("unapproved run provenance metadata")

    seen = set()
    seqs = []
    parents = {}
    starts = []
    ends = []
    statuses = []
    for span in spans:
        _keys(span, SPAN_KEYS, "span")
        native = span.get("native_id")
        if not isinstance(native, str) or not SPAN_ID_RE.fullmatch(native) or native in seen:
            raise BridgeError("invalid or duplicate native span identity")
        seen.add(native)
        parent = span.get("parent_native_id")
        if parent is not None and (not isinstance(parent, str) or not SPAN_ID_RE.fullmatch(parent)):
            raise BridgeError("invalid native parent identity")
        parents[native] = parent
        seq = span.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise BridgeError("invalid span sequence")
        seqs.append(seq)
        if (
            not isinstance(span.get("kind"), str) or span.get("kind") not in SPAN_KINDS
            or not isinstance(span.get("status"), str) or span.get("status") not in SPAN_STATUSES
        ):
            raise BridgeError("unapproved span kind or status")
        name = span.get("name")
        if not isinstance(name, str) or name not in SPAN_NAMES:
            raise BridgeError("unapproved span name")
        for field in ("started_at", "ended_at"):
            timestamp = _timestamp(span.get(field), "span")
            if timestamp is not None:
                (starts if field == "started_at" else ends).append(timestamp)
        duration = span.get("duration_ms")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration < 0
        ):
            raise BridgeError("invalid span duration")
        attrs = span.get("attributes")
        _validate_attributes(attrs, "span")
        if attrs.get("fleet.span") != name or attrs.get("fleet.trace") != trace:
            raise BridgeError("span trace provenance mismatch")
        statuses.append(span["status"])

        if "node_name" in span:
            _safe_text(span["node_name"], "node name")
        llm = span.get("llm")
        if llm is not None:
            _keys(llm, {"model", "input_tokens", "output_tokens", "reasoning_tokens"}, "llm metadata")
            if span["kind"] != "llm":
                raise BridgeError("LLM metadata on non-LLM span")
            if "model" in llm:
                _safe_text(llm["model"], "LLM model")
            for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
                if key in llm and (isinstance(llm[key], bool) or not isinstance(llm[key], int) or llm[key] < 0):
                    raise BridgeError("invalid LLM token metadata")
        tool = span.get("tool")
        if tool is not None:
            _keys(tool, {"name", "call_id"}, "tool metadata")
            _safe_text(tool.get("name"), "tool name")
            if "call_id" in tool:
                _safe_text(tool["call_id"], "tool call identifier")
            if span["kind"] != "tool":
                raise BridgeError("tool metadata on non-tool span")
            if span.get("node_name") != tool["name"]:
                raise BridgeError("tool node name mismatch")
        error = span.get("error")
        if error is not None:
            _keys(error, {"message"}, "error metadata")
            if (
                span["status"] != "error"
                or not isinstance(error.get("message"), str)
                or error.get("message") not in SAFE_ERROR_MESSAGES
            ):
                raise BridgeError("unapproved error metadata")

    if sorted(seqs) != list(range(len(spans))):
        raise BridgeError("span sequences are not contiguous and unique")
    if any(
        parent is not None and (not isinstance(parent, str) or parent not in seen or parent == child)
        for child, parent in parents.items()
    ):
        raise BridgeError("span parent identity is dangling or self-referential")
    for child in parents:
        visited = set()
        node = child
        while node is not None:
            if node in visited:
                raise BridgeError("span parentage contains a cycle")
            visited.add(node)
            node = parents.get(node)

    expected_status = "error" if "error" in statuses else (
        "ok" if statuses and all(status == "ok" for status in statuses) else "unknown"
    )
    if run["status"] != expected_status:
        raise BridgeError("run status does not match span outcomes")
    if (min(starts) if starts else None) != start or (max(ends) if ends else None) != end:
        raise BridgeError("run timestamps do not match span timestamps")

    if not isinstance(events, list) or len(events) > MAX_SPANS_PER_IMPORT * 100:
        raise BridgeError("malformed or oversized event collection")
    event_ids = set()
    event_sequences = {}
    for event in events:
        _keys(event, {"native_id", "seq", "at", "type", "span_native_id", "data"}, "event")
        span_id = event.get("span_native_id")
        if not isinstance(span_id, str) or span_id not in seen:
            raise BridgeError("event refers to an unknown span")
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise BridgeError("invalid event sequence")
        event_sequences.setdefault(span_id, []).append(seq)
        expected_id = "%s:%s:ev/%d" % (trace, span_id, seq)
        if event.get("native_id") != expected_id or expected_id in event_ids:
            raise BridgeError("invalid or duplicate event identity")
        event_ids.add(expected_id)
        if not isinstance(event.get("type"), str) or not SAFE_EVENT_RE.fullmatch(event["type"]):
            raise BridgeError("unapproved event type")
        _timestamp(event.get("at"), "event")
        _validate_attributes(event.get("data"), "event data")
    if any(sorted(values) != list(range(len(values))) for values in event_sequences.values()):
        raise BridgeError("event sequences are not contiguous and unique")
    return True


def validate_artifact(doc, raw, max_bytes=MAX_BYTES_PER_IMPORT):
    """Validate exact canonical bytes, schema, identities, and privacy allowlists."""
    return _validate_canonical_document(doc, raw, max_bytes)


def predict_target_ids(raw):
    """Predict AgentScope ids from the exact frozen upload bytes."""
    doc = json.loads(raw.decode("utf-8"))
    validate_artifact(doc, raw)
    digest = hashlib.sha256(raw).hexdigest()
    trace = doc["run"]["native_id"]
    import_id = "imp_" + hashlib.sha256((digest + "|import|").encode()).hexdigest()[:16]
    run_id = "run_" + hashlib.sha256((digest + "|run|runs/0").encode()).hexdigest()[:16]
    span_ids = {
        span["native_id"]: "spn_" + hashlib.sha256(
            (digest + "|span|runs/0/" + span["native_id"]).encode()
        ).hexdigest()[:16]
        for span in doc["spans"]
    }
    return {"import_id": import_id, "runs": {trace: run_id}, "spans": {trace: span_ids}}


def _unwrap_run(value):
    if isinstance(value, dict) and isinstance(value.get("run"), dict):
        return value["run"]
    return value


def _span_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("spans"), list):
        return value["spans"]
    raise BridgeError("span readback is malformed", "readback_failed")


def _valid_report_source_ref(value, span_count):
    if not isinstance(value, dict) or set(value) != {"pointer"}:
        return False
    pointer = value.get("pointer")
    if pointer == "/graph":
        return True
    if not isinstance(pointer, str):
        return False
    match = re.fullmatch(r"/spans/(0|[1-9][0-9]*)(?:/duration_ms)?", pointer)
    if match is None:
        return False
    index_text = match.group(1)
    if len(index_text) > len(str(span_count)):
        return False
    return int(index_text) < span_count


def _timestamp_to_us(value, where):
    parsed = _timestamp(value, where)
    if parsed is None:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _validate_report(report, ids, doc, status=None):
    if not isinstance(report, dict):
        raise BridgeError("import report is malformed", "readback_failed")
    quality = report.get("quality")
    if quality in ("invalid", "unsupported"):
        raise BridgeError("AgentScope rejected import quality", "rejected")
    if quality not in ("complete", "warnings", "partial"):
        raise BridgeError("import report quality is missing or unknown", "readback_failed")
    if report.get("import_id") != ids["import_id"]:
        raise BridgeError("import report identity mismatch", "readback_failed")
    expected_runs = set(ids["runs"].values())
    run_ids = report.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(run_id, str) for run_id in run_ids)
        or len(run_ids) != len(set(run_ids))
        or set(run_ids) != expected_runs
    ):
        raise BridgeError("import report run identity mismatch", "readback_failed")
    stats = report.get("stats")
    if (
        not isinstance(stats, dict)
        or set(stats) != set(STATS_KEYS)
        or any(type(stats.get(key)) is not int or stats[key] < 0 for key in STATS_KEYS)
    ):
        raise BridgeError("import report span accounting mismatch", "readback_failed")
    expected_stats = {
        "runs": len(expected_runs), "spans": len(doc["spans"]),
        "node_executions": 0, "events": 0, "checkpoints": 0, "payloads": 0,
        "dropped": 0, "unmapped": 0, "detection_skipped_lines": 0,
    }
    if any(stats[key] != value for key, value in expected_stats.items()):
        raise BridgeError("import report content accounting mismatch", "readback_failed")
    warnings = report.get("warnings")
    if not isinstance(warnings, list):
        raise BridgeError("import report warnings are malformed", "readback_failed")
    for warning in warnings:
        _keys(warning, {"code", "message", "source_ref"}, "import warning")
        if not isinstance(warning.get("code"), str) or not isinstance(warning.get("message"), str):
            raise BridgeError("import report warnings are malformed", "readback_failed")
        if warning.get("source_ref") is not None and not _valid_report_source_ref(
            warning["source_ref"], len(doc["spans"])
        ):
            raise BridgeError("import report warning provenance is malformed", "readback_failed")
    if stats["warnings"] != len(warnings):
        raise BridgeError("import report warning accounting mismatch", "readback_failed")
    already = report.get("already_imported")
    if not isinstance(already, bool):
        raise BridgeError("import report idempotency state is malformed", "readback_failed")
    if status is not None and ((status == 200) != already or status not in (200, 201)):
        raise BridgeError("import status and idempotency report disagree", "readback_failed")
    return quality, already


def _report_signature(report):
    warnings = tuple(
        (warning.get("code"), warning.get("message"), warning.get("source_ref"))
        for warning in report["warnings"]
    )
    return (
        report["import_id"], report["quality"], tuple(report["run_ids"]),
        report["stats"], warnings,
    )


def _compare_llm(expected, got):
    if expected is None:
        if got.get("llm") not in (None, {}) or any(
            got.get(key) is not None
            for key in ("model", "tokens_in", "tokens_out", "tokens_reasoning", "finish_reason")
        ):
            raise BridgeError("unexpected LLM metadata in span readback", "readback_failed")
        return
    if got.get("llm") is not None:
        raise BridgeError("unexpected nested LLM metadata in span readback", "readback_failed")
    aliases = {
        "model": ("model",),
        "input_tokens": ("tokens_in", "input_tokens"),
        "output_tokens": ("tokens_out", "output_tokens"),
        "reasoning_tokens": ("tokens_reasoning", "reasoning_tokens"),
    }
    for key, names in aliases.items():
        present = [got[name] for name in names if got.get(name) is not None]
        if key in expected and (
            not present or any(value != expected[key] for value in present)
        ):
            raise BridgeError("LLM semantic readback mismatch", "readback_failed")
        if key not in expected and present:
            raise BridgeError("unexpected LLM metadata in span readback", "readback_failed")


def _compare_tool(expected, got):
    if expected is None:
        if got.get("tool") not in (None, {}) or any(
            got.get(key) is not None for key in ("tool_name", "tool_call_id")
        ):
            raise BridgeError("unexpected tool metadata in span readback", "readback_failed")
        return
    if got.get("tool") is not None:
        raise BridgeError("unexpected nested tool metadata in span readback", "readback_failed")
    aliases = {"name": ("tool_name", "node_name"), "call_id": ("tool_call_id", "call_id")}
    for key, names in aliases.items():
        present = [got[name] for name in names if got.get(name) is not None]
        if key in expected and (
            not present or any(value != expected[key] for value in present)
        ):
            raise BridgeError("tool semantic readback mismatch", "readback_failed")
        if key not in expected and present:
            raise BridgeError("unexpected tool metadata in span readback", "readback_failed")


def verify_readback(raw, report, run, spans=None, persisted_report=None, run_summary=None):
    """Independently compare persisted run, every span, and import receipt."""
    ids = predict_target_ids(raw)
    doc = json.loads(raw.decode("utf-8"))
    quality, _ = _validate_report(report, ids, doc)
    if persisted_report is not None:
        _validate_report(persisted_report, ids, doc)
        if _report_signature(persisted_report) != _report_signature(report):
            raise BridgeError("persisted import report differs from POST report", "readback_failed")
    trace = doc["run"]["native_id"]
    run_id = ids["runs"][trace]
    run = _unwrap_run(run)
    if not isinstance(run, dict):
        raise BridgeError("run readback is malformed", "readback_failed")
    if run.get("id") != run_id:
        raise BridgeError("run readback identity mismatch", "readback_failed")
    if run.get("status") != doc["run"]["status"]:
        raise BridgeError("run status readback mismatch", "readback_failed")
    if run.get("quality") != quality or run.get("graph_mode") != "tree":
        raise BridgeError("run quality or graph derivation readback mismatch", "readback_failed")
    if run.get("capabilities") != doc["source"].get("capabilities"):
        raise BridgeError("run capability provenance readback mismatch", "readback_failed")
    if "native_id" in run and run["native_id"] != trace:
        raise BridgeError("run native identity readback mismatch", "readback_failed")
    if "import_id" in run and run["import_id"] != ids["import_id"]:
        raise BridgeError("run import provenance mismatch", "readback_failed")
    if run.get("trace_id") is not None and run["trace_id"] != trace:
        raise BridgeError("run trace provenance mismatch", "readback_failed")

    if not isinstance(run_summary, dict) or run_summary.get("id") != run_id:
        raise BridgeError("run summary readback is missing or mismatched", "readback_failed")
    if (
        run_summary.get("status") != doc["run"]["status"]
        or run_summary.get("graph_mode") != "tree"
        or run_summary.get("started_us") != _timestamp_to_us(doc["run"].get("started_at"), "run")
        or run_summary.get("ended_us") != _timestamp_to_us(doc["run"].get("ended_at"), "run")
    ):
        raise BridgeError("run summary semantic readback mismatch", "readback_failed")

    records = _span_list(spans if spans is not None else run.get("spans"))
    if len(records) != len(doc["spans"]):
        raise BridgeError("span readback count mismatch", "readback_failed")
    by_native = {}
    for record in records:
        if not isinstance(record, dict):
            raise BridgeError("span readback contains malformed records", "readback_failed")
        native = record.get("native_id")
        if not isinstance(native, str) or native in by_native:
            raise BridgeError("span readback contains duplicate or missing native ids", "readback_failed")
        by_native[native] = record
    expected_ids = ids["spans"][trace]
    if set(by_native) != set(expected_ids):
        raise BridgeError("span readback native identity mismatch", "readback_failed")

    for expected_index, expected in enumerate(doc["spans"]):
        native = expected["native_id"]
        got = by_native[native]
        if got.get("id") != expected_ids[native]:
            raise BridgeError("span target identity or run provenance mismatch", "readback_failed")
        if "run_id" in got and got["run_id"] != run_id:
            raise BridgeError("span run provenance mismatch", "readback_failed")
        if "trace_id" in got and got["trace_id"] != trace:
            raise BridgeError("span trace provenance mismatch", "readback_failed")
        parent_native = expected.get("parent_native_id")
        expected_parent_id = expected_ids.get(parent_native) if parent_native is not None else None
        if got.get("parent_id") != expected_parent_id:
            raise BridgeError("span parent target link mismatch", "readback_failed")
        if "parent_native_id" in got and got.get("parent_native_id") != parent_native:
            raise BridgeError("span native parent link mismatch", "readback_failed")
        for key in ("seq", "kind", "name", "status"):
            if got.get(key) != expected.get(key):
                raise BridgeError("span semantic readback mismatch", "readback_failed")
        started_us = _timestamp_to_us(expected.get("started_at"), "expected span")
        ended_us = _timestamp_to_us(expected.get("ended_at"), "expected span")
        if got.get("started_us") != started_us or got.get("ended_us") != ended_us:
            raise BridgeError("span timestamp readback mismatch", "readback_failed")
        if started_us is not None and ended_us is not None:
            expected_duration_us = ended_us - started_us if ended_us >= started_us else None
        elif expected.get("duration_ms") is not None:
            expected_duration_us = round(expected["duration_ms"] * 1000)
        else:
            expected_duration_us = None
        if got.get("duration_us") != expected_duration_us:
            raise BridgeError("span duration readback mismatch", "readback_failed")
        if got.get("attributes") != expected.get("attributes"):
            raise BridgeError("span metadata readback mismatch", "readback_failed")
        if got.get("node_name") != expected.get("node_name"):
            raise BridgeError("span node name readback mismatch", "readback_failed")
        _compare_llm(expected.get("llm"), got)
        _compare_tool(expected.get("tool"), got)
        if got.get("finish_reason") is not None or got.get("cost_micro_usd") is not None or got.get("cost_basis") is not None:
            raise BridgeError("unexpected cost or finish metadata in span readback", "readback_failed")
        if any(got.get(key) is not None for key in ("input_payload", "output_payload")):
            raise BridgeError("unexpected content-bearing span field in readback", "readback_failed")
        if got.get("source_ref") != {"pointer": "/spans/%d" % expected_index}:
            raise BridgeError("span source reference readback mismatch", "readback_failed")
        if got.get("error") != expected.get("error"):
            raise BridgeError("span error readback mismatch", "readback_failed")
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener():
    # Do not route a local bearer token through environment-configured proxies.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _multipart(raw):
    boundary = "fleetbridge" + hashlib.sha256(raw).hexdigest()[:24]
    body = (
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"trace.json\"\r\n"
         "Content-Type: application/json\r\n\r\n" % boundary).encode("ascii")
        + raw + ("\r\n--%s--\r\n" % boundary).encode("ascii")
    )
    return boundary, body


def request_json(url, token=None, method="GET", body=None, content_type=None, attempts=3):
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
        raise BridgeError("request attempts must be between one and three")
    if token is not None and (
        not isinstance(token, str) or not token or "\r" in token or "\n" in token
    ):
        raise BridgeError("AgentScope token is missing or malformed", "authentication_failed")
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if content_type:
        headers["Content-Type"] = content_type
    opener = _opener()
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            with opener.open(request, timeout=5) as response:
                if response.status not in (200, 201):
                    raise BridgeError("AgentScope returned an unexpected HTTP status", "rejected")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise BridgeError("AgentScope response exceeds the configured limit", "readback_failed")
            try:
                return response.status, json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise BridgeError("AgentScope returned invalid JSON", "readback_failed") from None
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if 500 <= code <= 599 and attempt + 1 < attempts:
                continue
            if code == 401:
                raise BridgeError("AgentScope authentication failed", "authentication_failed") from None
            if code in (403, 413, 421, 422) or 400 <= code <= 499:
                raise BridgeError("AgentScope rejected the request (HTTP %d)" % code, "rejected") from None
            if 500 <= code <= 599:
                raise BridgeError("AgentScope server is unavailable", "server_unavailable") from None
            raise BridgeError("AgentScope rejected the request", "rejected") from None
        except BridgeError:
            raise
        except (
            urllib.error.URLError, http.client.HTTPException, socket.timeout,
            TimeoutError, ConnectionError, OSError,
        ):
            if attempt + 1 < attempts:
                continue
            raise BridgeError("AgentScope transport failed", "server_unavailable") from None
    raise BridgeError("AgentScope transport failed", "server_unavailable")


def _url(base, path, query=None):
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _read_span_pages(base, run_id, token):
    path = "/api/v1/runs/%s/spans" % urllib.parse.quote(run_id, safe="")
    offset = 0
    records = []
    seen_ids = set()
    seen_native_ids = set()
    seen_pages = set()
    for _ in range(MAX_READBACK_PAGES):
        query = {"limit": SPAN_PAGE_SIZE, "offset": offset}
        status, page = request_json(_url(base, path, query), token)
        if status != 200 or not isinstance(page, list) or len(page) > SPAN_PAGE_SIZE:
            raise BridgeError("span readback returned an unexpected status", "readback_failed")
        if len(records) + len(page) > MAX_SPANS_PER_IMPORT:
            raise BridgeError("span readback exceeds the configured limit", "readback_failed")
        if not page:
            return records
        signature = tuple(
            json.dumps(row, sort_keys=True, separators=(",", ":")) for row in page
        )
        if signature in seen_pages:
            raise BridgeError("span pagination made no progress", "readback_failed")
        seen_pages.add(signature)
        for row in page:
            if not isinstance(row, dict):
                raise BridgeError("span readback contains malformed records", "readback_failed")
            span_id = row.get("id")
            native_id = row.get("native_id")
            if (
                not isinstance(span_id, str) or not span_id
                or not isinstance(native_id, str) or not native_id
                or span_id in seen_ids or native_id in seen_native_ids
            ):
                raise BridgeError("span pagination contains duplicate or missing identities", "readback_failed")
            seen_ids.add(span_id)
            seen_native_ids.add(native_id)
        records.extend(page)
        if len(page) < SPAN_PAGE_SIZE:
            return records
        next_offset = offset + len(page)
        if next_offset <= offset:
            raise BridgeError("span pagination made no progress", "readback_failed")
        offset = next_offset
    raise BridgeError("span pagination exceeded the page limit", "readback_failed")


def _read_run_summary(base, run_id, token):
    """Find a run summary through the bounded public list endpoint."""
    offset = 0
    seen_ids = set()
    for _ in range(MAX_READBACK_PAGES):
        status, payload = request_json(
            _url(base, "/api/v1/runs", {"limit": RUN_PAGE_SIZE, "offset": offset}), token
        )
        if status != 200 or not isinstance(payload, list) or len(payload) > RUN_PAGE_SIZE:
            raise BridgeError("run summary readback is malformed", "readback_failed")
        for row in payload:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise BridgeError("run summary list contains malformed records", "readback_failed")
            if row["id"] in seen_ids:
                raise BridgeError("run summary pagination contains duplicate IDs", "readback_failed")
            seen_ids.add(row["id"])
            if row["id"] == run_id:
                return row
        if len(payload) < RUN_PAGE_SIZE:
            break
        offset += len(payload)
    raise BridgeError("run summary was not found within the readback limit", "readback_failed")


def _deliver_document(raw, server, token, *, synthetic_only=False, live=False):
    if live and synthetic_only:
        raise BridgeError("live and synthetic-only acknowledgments conflict; pass exactly one")
    if live:
        _require_live_acknowledgment(live)
    elif synthetic_only:
        _require_synthetic_only(synthetic_only)
    else:
        raise BridgeError("delivery requires an explicit acknowledgment: --synthetic-only for test data or --live for real observations")
    parsed_server = validate_server(server)
    base = "%s://%s" % (parsed_server.scheme, parsed_server.netloc)
    doc = json.loads(raw.decode("utf-8"))
    validate_artifact(doc, raw)
    ids = predict_target_ids(raw)
    boundary, body = _multipart(raw)
    status, report = request_json(
        base + "/api/v1/imports", token, method="POST", body=body,
        content_type="multipart/form-data; boundary=" + boundary,
    )
    quality, already_imported = _validate_report(report, ids, doc, status=status)
    import_status, persisted_report = request_json(
        base + "/api/v1/imports/" + urllib.parse.quote(ids["import_id"], safe=""), token
    )
    if import_status != 200:
        raise BridgeError("persisted import report returned an unexpected status", "readback_failed")
    run_id = ids["runs"][doc["run"]["native_id"]]
    run_status, run = request_json(
        base + "/api/v1/runs/" + urllib.parse.quote(run_id, safe=""), token
    )
    if run_status != 200:
        raise BridgeError("run readback returned an unexpected status", "readback_failed")
    run_summary = _read_run_summary(base, run_id, token)
    spans = _read_span_pages(base, run_id, token)
    verify_readback(
        raw, report, run, spans, persisted_report=persisted_report,
        run_summary=run_summary,
    )
    outcome = "already_imported" if already_imported else (
        "partial" if quality in ("partial", "warnings") else "delivered"
    )
    return {
        "outcome": outcome,
        "import_id": ids["import_id"],
        "run_id": run_id,
        "native_trace_id": doc["run"]["native_id"],
        "quality": quality,
        "already_imported": already_imported,
        "upload_sha256": hashlib.sha256(raw).hexdigest(),
        "upload_bytes": len(raw),
        "span_count": len(doc["spans"]),
        "verified": True,
        "verification_scope": [
            "POST and persisted ImportReport identities, quality, warnings, and all 11 counters",
            "RunDetail id/status/quality/tree mode/capabilities",
            "RunSummary id/status/tree mode and normalized run timestamps",
            "all SpanView identities, parentage, timestamps, duration, status, attributes, LLM/tool/error metadata",
            "absence of payload, finish-reason, and cost fields; exact safe source_ref pointers",
        ],
        "verification_limits": [
            "AgentScope API omits native trace/import ownership on RunDetail; linkage is checked through the persisted import run_ids and each span native_id/fleet.trace metadata.",
            "Verification is through the AgentScope API projection; no direct database inspection is performed.",
            "Event-bearing artifacts are refused because this AgentScope API exposes no event readback endpoint.",
        ],
    }


def deliver(raw, server, token, *, synthetic_only=False):
    """Deliver one already-canonical document (lower-level helper)."""
    _require_synthetic_only(synthetic_only)
    validate_server(server)
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise BridgeError("AgentScope token is missing or malformed", "authentication_failed")
    return _deliver_document(raw, server, token, synthetic_only=synthetic_only)


def _known_token_total(documents, key):
    llm_spans = [
        span
        for document in documents
        for span in document.get("spans", [])
        if span.get("kind") == "llm"
    ]
    if not llm_spans or any(key not in (span.get("llm") or {}) for span in llm_spans):
        return None
    return sum(span["llm"][key] for span in llm_spans)


def _receipt_summary(receipt, documents=None):
    provenance = receipt.get("provenance", {}) if isinstance(receipt, dict) else {}
    warnings = receipt.get("warnings", [])
    if not isinstance(warnings, list):
        raise BridgeError("local Fleet receipt warnings are malformed")
    stats = dict(receipt.get("stats") or {})
    warning_count = len(warnings)
    if stats.get("warnings") != warning_count:
        raise BridgeError("local Fleet receipt warning accounting is malformed")
    warning_codes = []
    for warning in warnings:
        if isinstance(warning, dict) and isinstance(warning.get("code"), str):
            warning_codes.append(warning["code"])
    tokens_in = receipt.get("tokens_in")
    tokens_out = receipt.get("tokens_out")
    if documents is not None:
        tokens_in = _known_token_total(documents, "input_tokens")
        tokens_out = _known_token_total(documents, "output_tokens")
    return {
        "import_id": receipt.get("import_id"),
        "quality": receipt.get("quality"),
        "run_ids": list(receipt.get("run_ids") or []),
        "already_imported": bool(receipt.get("already_imported", False)),
        "stats": stats,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "provenance": {
            "producer": provenance.get("producer"),
            "schema_version": provenance.get("schema_version"),
            "content_sha256": provenance.get("content_sha256"),
            "graph_derivation": provenance.get("graph_derivation"),
        },
        "warning_count": warning_count,
        "warning_codes": sorted(warning_codes),
    }


def _import_input(raw, parsed, source_kind, live, normalization):
    """Bytes the local Fleet import should consume for this snapshot.

    Normally the raw snapshot itself. But when live mode processed
    real-exporter records, the raw bytes contain metric records and
    un-normalized exporter fields (session ids, raw error strings,
    stack traces) that must never reach the ledger. The ledger must
    store exactly the normalized spans the bridge delivers, so the
    normalized spans are re-serialized as a JSON array and imported
    instead. The re-serialization is deterministic (sorted keys,
    compact separators), so re-import idempotency is preserved.
    """
    if not live or normalization.get("real_exporter_records", 0) <= 0:
        return raw
    if source_kind == "jsonl":
        normalized_spans = parsed[0]
    elif source_kind == "spans":
        normalized_spans = parsed
    else:
        return raw
    # JSON array, not JSONL: a one-span input re-serialized as JSONL
    # would parse back as a single JSON object, which the importer
    # reports "unsupported". The array form imports any span count.
    return json.dumps(normalized_spans, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deliver_snapshot(conn, raw, producer, server, token, settings=None, *, synthetic_only=False, live=False):
    """Validate, locally import, then deliver the same bounded source snapshot.

    Mapping and local import both consume the identical in-memory byte string;
    no later source-file read can change the artifact after validation.

    Exactly one acknowledgment is required: `synthetic_only=True` for
    synthetic test data, or `live=True` for real Copilot observations.
    Live delivery accepts only producer "copilot" and normalizes raw
    Copilot exporter spans (name mapping, attribute sanitizing) before
    validation; it never passes real observations with a synthetic-only
    acknowledgment.
    """
    if live and synthetic_only:
        raise BridgeError("live and synthetic-only acknowledgments conflict; pass exactly one")
    if live:
        if producer != "copilot":
            raise BridgeError("live delivery accepts Copilot observations only (producer must be copilot)")
        mode = "live"
    elif synthetic_only:
        mode = "synthetic"
    else:
        raise BridgeError("delivery requires an explicit acknowledgment: --synthetic-only for test data or --live for real observations")
    validate_server(server)
    if not isinstance(raw, bytes) or len(raw) > (settings or ImportSettings()).max_bytes_per_import:
        raise BridgeError("input snapshot exceeds the configured size limit")
    if not isinstance(producer, str) or not PRODUCER_RE.fullmatch(producer):
        raise BridgeError("producer must be a bounded safe identifier")
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise BridgeError("AgentScope token is missing or malformed", "authentication_failed")
    settings = settings or ImportSettings()
    try:
        source_kind, parsed, documents, normalization = _parse_source_snapshot(raw, producer, live=live)
        for document in documents:
            document_bytes = canonical_bytes(document)
            validate_artifact(document, document_bytes, settings.max_bytes_per_import)
        if live:
            for document in documents:
                if document["run"]["metadata"]["producer"] != "copilot":
                    raise BridgeError("live delivery accepts Copilot observations only (producer must be copilot)")
    except BridgeError:
        raise
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise BridgeError("source snapshot could not be safely validated") from None

    try:
        fleet_receipt = import_bytes(conn, _import_input(raw, parsed, source_kind, live, normalization), producer, settings=settings)
    except ImportRefused as exc:
        raise BridgeError("local Fleet import refused the snapshot") from None
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise BridgeError("local Fleet import could not process the snapshot") from None
    receipt_summary = _receipt_summary(fleet_receipt, documents)
    fleet_content_sha = receipt_summary["provenance"].get("content_sha256")
    if not isinstance(fleet_content_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", fleet_content_sha):
        raise BridgeError("local Fleet receipt provenance is malformed")
    fleet_run_ids = {
        document["run"]["native_id"]: run_id_for(fleet_content_sha, document["run"]["native_id"])
        for document in documents
    }
    base_result = {
        "source_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "source_snapshot_bytes": len(raw),
        "source_format": source_kind,
        "mode": mode,
        "live_normalization": normalization,
        "fleet_receipt": receipt_summary,
        "run_mappings": [],
        "deliveries": [],
        "failures": [],
        "unattempted": [],
        "verified": False,
    }
    if not documents or fleet_receipt.get("quality") in ("invalid", "unsupported"):
        base_result.update({
            "outcome": "rejected",
            "failures": [{"outcome": "rejected", "reason": "snapshot produced no deliverable canonical runs"}],
        })
        return base_result
    if (
        fleet_receipt.get("stats", {}).get("runs") != len(documents)
        or len(fleet_receipt.get("run_ids", [])) != len(documents)
        or set(fleet_run_ids.values()) != set(fleet_receipt.get("run_ids", []))
    ):
        raise BridgeError("local Fleet receipt does not match mapped run accounting")

    pending = []
    for document in documents:
        document_bytes = canonical_bytes(document)
        trace_id = document["run"]["native_id"]
        target_ids = predict_target_ids(document_bytes)
        mapping = {
            "native_trace_id": trace_id,
            "fleet_run_id": fleet_run_ids[trace_id],
            "agentscope_import_id": target_ids["import_id"],
            "agentscope_run_id": target_ids["runs"][trace_id],
            "upload_sha256": hashlib.sha256(document_bytes).hexdigest(),
            "upload_bytes": len(document_bytes),
        }
        base_result["run_mappings"].append(mapping)
        pending.append((document_bytes, mapping))

    for index, (document_bytes, mapping) in enumerate(pending):
        try:
            delivery = _deliver_document(document_bytes, server, token, synthetic_only=synthetic_only, live=live)
            base_result["deliveries"].append(delivery)
        except BridgeError as exc:
            base_result["failures"].append({
                "outcome": exc.outcome,
                **mapping,
                "reason": str(exc),
            })
            if exc.outcome == "authentication_failed":
                for _, unattempted in pending[index + 1:]:
                    base_result["unattempted"].append({
                        "outcome": "not_attempted",
                        **unattempted,
                        "reason": "delivery stopped after authentication failure",
                    })
                break
    if base_result["failures"]:
        success_count = len(base_result["deliveries"])
        outcomes = {failure["outcome"] for failure in base_result["failures"]}
        if success_count:
            overall = "partial"
        elif len(outcomes) == 1:
            overall = next(iter(outcomes))
        else:
            overall = "rejected"
    else:
        remote_partial = any(item["quality"] in ("partial", "warnings") for item in base_result["deliveries"])
        local_partial = fleet_receipt.get("quality") != "complete"
        if remote_partial or local_partial:
            overall = "partial"
        elif all(item["already_imported"] for item in base_result["deliveries"]):
            overall = "already_imported"
        else:
            overall = "delivered"
        base_result["verified"] = True
    base_result["outcome"] = overall
    return base_result
