"""OTLP/HTTP JSON encoding and export (stdlib transport).

The local receiver (receiver.py) speaks OTLP/HTTP JSON. The pinned
opentelemetry-exporter-otlp-proto-http package serializes with protobuf
(Content-Type: application/x-protobuf) — there is no official OTLP JSON
exporter package on PyPI — so the local loop uses this small exporter,
which emits spec-compliant OTLP JSON over HTTP with urllib.

The pinned proto-http (protobuf) exporter remains installed and can be
used against a real OTLP collector by pointing
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT at it; the stdlib-only local receiver
does not decode protobuf. That is a documented limitation, not a silent
substitution: every test in this milestone asserts against JSON payloads
received here.

No secrets are ever placed in a span, a header, or a URL by this module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, format_span_id, format_trace_id

# OTLP SpanKind values are the SDK SpanKind values + 1
# (OTLP: UNSPECIFIED=0, INTERNAL=1, SERVER=2, CLIENT=3, PRODUCER=4, CONSUMER=5).
_KIND_MAP = {
    SpanKind.INTERNAL: 1,
    SpanKind.SERVER: 2,
    SpanKind.CLIENT: 3,
    SpanKind.PRODUCER: 4,
    SpanKind.CONSUMER: 5,
}

_STATUS_MAP = {"UNSET": 0, "OK": 1, "ERROR": 2}


def _encode_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_encode_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _encode_attributes(attrs):
    return [
        {"key": key, "value": _encode_value(value)}
        for key, value in (attrs or {}).items()
    ]


def _encode_span(span: ReadableSpan):
    ctx = span.context
    data = {
        "traceId": format_trace_id(ctx.trace_id),
        "spanId": format_span_id(ctx.span_id),
        "name": span.name,
        "kind": _KIND_MAP.get(span.kind, 1),
        "startTimeUnixNano": str(span.start_time or 0),
        "endTimeUnixNano": str(span.end_time or 0),
        "attributes": _encode_attributes(span.attributes),
    }
    if span.parent is not None and span.parent.is_valid:
        data["parentSpanId"] = format_span_id(span.parent.span_id)
    status_code = span.status.status_code.name if span.status else "UNSET"
    status = {"code": _STATUS_MAP.get(status_code, 0)}
    if span.status and span.status.description:
        status["message"] = span.status.description
    data["status"] = status
    if span.events:
        data["events"] = [
            {
                "timeUnixNano": str(event.timestamp or 0),
                "name": event.name,
                "attributes": _encode_attributes(event.attributes),
            }
            for event in span.events
        ]
    if span.links:
        data["links"] = [
            {
                "traceId": format_trace_id(link.context.trace_id),
                "spanId": format_span_id(link.context.span_id),
                "attributes": _encode_attributes(link.attributes),
            }
            for link in span.links
        ]
    return data


def encode_spans(spans):
    """Encode SDK spans as an OTLP/HTTP JSON payload dict."""
    resource_spans = {}
    for span in spans:
        resource = span.resource
        resource_key = json.dumps(
            sorted((resource.attributes or {}).items()), sort_keys=True, default=str
        )
        scope = getattr(span, "instrumentation_scope", None)
        scope_name = getattr(scope, "name", "fleet-trace") or "fleet-trace"
        scope_version = getattr(scope, "version", "") or ""
        group_key = (resource_key, scope_name, scope_version)
        group = resource_spans.setdefault(
            group_key,
            {
                "resource": {"attributes": _encode_attributes(resource.attributes)},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name, "version": scope_version},
                        "spans": [],
                    }
                ],
            },
        )
        group["scopeSpans"][0]["spans"].append(_encode_span(span))
    return {"resourceSpans": list(resource_spans.values())}


class OTLPJSONExporter(SpanExporter):
    """SpanExporter that POSTs OTLP JSON to an HTTP endpoint (stdlib only).

    `endpoint` must be the full traces URL, e.g.
    http://127.0.0.1:4318/v1/traces. The endpoint must not embed credentials
    in the URL.
    """

    def __init__(self, endpoint, timeout=5):
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must be an http(s) URL")
        if "@" in endpoint.split("://", 1)[1].split("/", 1)[0]:
            raise ValueError("endpoint must not embed credentials in the URL")
        self._endpoint = endpoint
        self._timeout = timeout

    def export(self, spans):
        if not spans:
            return SpanExportResult.SUCCESS
        body = json.dumps(encode_spans(spans)).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                resp.read()
            return SpanExportResult.SUCCESS
        except (urllib.error.URLError, OSError, TimeoutError):
            return SpanExportResult.FAILURE

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class NullExporter(SpanExporter):
    """Discards spans. Used when no endpoint is configured so span IDs
    are still generated and reported (e.g. by child.py) without network."""

    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True
