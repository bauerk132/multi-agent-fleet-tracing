"""Copilot observer — Milestone 4 (isolated Copilot observer).

Receiver-side adapter for GitHub Copilot in VS Code. Unlike Hermes
(M3), Copilot exposes NO host hook/callback plugin API — there is
nothing to register into. What Copilot does expose, verified against
the official VS Code docs
(https://code.visualstudio.com/docs/agents/guides/monitoring-agents,
crawled 2026-11-07), is native OpenTelemetry export: Copilot Chat
emits traces, metrics, and events via OTel when the owner opts in
(``github.copilot.chat.otel.enabled`` / ``COPILOT_OTEL_ENABLED`` /
``OTEL_EXPORTER_OTLP_ENDPOINT``). This adapter consumes those native
spans and re-emits them as fleet schema-v1 spans. It never enables
Copilot telemetry itself, never enables content capture, never enables
identity capture.

Verified native surface (official docs):
- Span kinds (``gen_ai.operation.name``): ``invoke_agent`` (root per
  agent interaction), ``chat`` (one per LLM API call), ``execute_tool``
  (one per tool invocation), ``execute_hook`` (one per hook execution,
  e.g. PreToolUse / Stop). Each carries GenAI-semantic-convention
  attributes plus ``github.copilot.*`` / legacy ``copilot_chat.*`` keys.
- Subagent propagation is native: a subagent's ``invoke_agent`` span
  appears as a child of the parent's ``execute_tool`` span (runSubagent).
- Metrics (token/duration counters, edit acceptance, user feedback)
  and span events (session.start, agent.turn, tool.call, edit
  feedback, survival) exist but are NOT translated into fleet spans
  in this round (see "Not translated" below).
- Services: ``copilot-chat`` (extension spans) and ``github-copilot``
  (shared runtime spans + CLI terminal sessions). CLI terminal sessions
  are independent root traces, NOT linked to extension traces.
- Content capture is OFF by default; identity capture is opt-in only
  (Local harness sessions). The adapter strips both kinds of
  attributes even if they arrive (misconfigured upstream).

What is NOT observable / NOT claimed:
- No inbound TRACEPARENT injection into Copilot Chat is documented
  anywhere on the page (no "traceparent" mention): Copilot emits its
  own trace IDs and there is no supported way to parent a Copilot
  trace under a fleet trace. Per plan §1 ("unsupported parenting
  stays external"), Copilot spans are correlated, never re-parented:
  fleet trace IDs are SDK-generated; the native traceId is preserved
  as the turn correlation key (``turn.id`` = ``<traceId>:<rootSpanId>``)
  and the native parentSpanId as ``parent_request_id``. No remote
  span ID is ever fabricated.
- No vendor internals: only the documented span kinds and attributes
  above. The adapter never claims to trace hidden Copilot internals.
- Claude agent spans (``gen_ai.agent.name`` == "claude", service
  ``claude-code``) are out of scope — Claude Code has its own M2 path.
  They are dropped and counted, never emitted as Copilot spans.
- OTLP/HTTP JSON: copilot-chat v0.73.0 defaults the otlp-http
  exporter protocol to http/json, so the local receiver accepts it;
  earlier versions defaulted to protobuf. The verified local loop
  is the FILE exporter (``github.copilot.chat.otel.outfile`` /
  ``COPILOT_OTEL_FILE_EXPORTER_PATH``, JSON lines). Each line is one
  ReadableSpan JSON object (``spanContext`` / ``parentSpanContext``,
  HrTime ``startTime``/``endTime`` arrays, nested
  ``resource.attributes``) — see config.md; ``ingest_jsonl`` also
  accepts a full OTLP payload per line and a single flattened span
  per line, and skips metric/log lines (never errors). The SQLite
  dbSpanExporter path is not implemented (schema undocumented).

Ingestion (both entry points are off-path, never raise, never block):
- ``handle_payload(payload)`` — an OTLP/HTTP JSON payload
  (``resourceSpans``), the same shape ``receiver.flatten_payload``
  produces. Used by tests and by any JSON-speaking exporter/collector
  wired in front of the adapter.
- ``ingest_jsonl(path)`` — the documented file exporter: one
  ReadableSpan JSON value per line (see config.md). Metric/log lines
  are skipped and counted, never treated as errors. Span lines are
  collected across the file and handed to ``handle_spans`` in one
  batch so parents order before children even when the file lists
  children first.
- The adapter performs NO network I/O on the ingest path: ingest only
  validates and enqueues. A single worker thread owns span lifecycle
  and export (off-path), same as M3.

Design (plan §5, §12, §13; mirrors the M3 engine shape):
- Schema v1: the fleet span names ``agent.run`` / ``llm.call`` /
  ``tool.exec`` are reused; ``execute_hook`` adds span ``hook.exec`` —
  a documented v1 vocabulary extension for Copilot (the M3 12-type
  event set is unchanged for Hermes).
- Explicit parenting, never ambient: fleet spans are created with
  tracer.start_span() + an explicit parent Context and end(). Native
  parentSpanId links are resolved through a bounded native-context
  map; unresolved parents fall back to the turn root and are counted,
  never invented.
- Turn roots: one ``agent.run`` per ``invoke_agent`` span, keyed by
  (traceId, rootSpanId). Children arriving before their root create a
  lazy root (start = first child's start); the root folds the
  invoke_agent attributes when it arrives and ends at its end time.
  If the invoke_agent span never arrives, stale pruning closes the
  root incomplete. A late invoke_agent after its root already closed
  is dropped and counted (no second root for one turn).
- Bounded everything: bounded queue (overflow drops + counts),
  bounded dedup set, bounded native index/context maps, stale-event
  TTL, dropped/partial counters. Loss is visible; Copilot is never
  blocked.
- Privacy: all span attributes go through sanitize_attributes
  (allowlist + 256-char guard); errors are classified with a bounded
  enum, never raw text. Content-capture, identity, and identifying
  repo attributes are stripped and counted even when present.
- Native timestamps are preserved (fleet span start/end = native
  start/end) so durations stay faithful.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.trace import SpanKind, Status, StatusCode, format_span_id, format_trace_id

from ._sanitize import MAX_ATTRIBUTE_CHARS, sanitize_attributes
from ._otlp_json import OTLPJSONExporter

# ---------------------------------------------------------------------------
# Verified Copilot surface (official VS Code docs, 2026-11-07)
# ---------------------------------------------------------------------------

# Native operation names (gen_ai.operation.name, falling back to span name).
OPERATIONS = frozenset({"invoke_agent", "chat", "execute_tool", "execute_hook"})

# Fleet span names per operation. hook.exec is a v1 vocabulary extension
# for Copilot (see module docstring); the other three reuse the M3 names.
_SPAN_NAMES = {
    "invoke_agent": "agent.run",
    "chat": "llm.call",
    "execute_tool": "tool.exec",
    "execute_hook": "hook.exec",
}

# Resource service.name values that belong to Copilot. Anything else
# present (e.g. "claude-code") is out of scope. A span with NO service
# name is accepted (provenance unknown but operation-gated); a span
# with a service name outside this set is dropped.
COPILOT_SERVICES = frozenset({"copilot-chat", "github-copilot"})

# Content-capture attributes: never collected, never emitted. Present in
# a native span they are stripped and counted. (Also covers the M3
# generic content fields as a safety net — see DENIED_FIELDS.)
# FR7-05: real copilot-chat v0.73.0 / SDK content keys, so
# stripped_fields counts them when they arrive.
CONTENT_ATTRIBUTES = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.definitions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "github.copilot.tool.parameters.command",
        "github.copilot.tool.parameters.file_path",
        "github.copilot.tool.parameters.mcp_server_name",
        "copilot_chat.hook_input",
        "copilot_chat.hook_output",
        "copilot_chat.user_request",
        "copilot_chat.hook_command",
        "copilot_chat.file.relative_path",
        "gen_ai.system_instructions",
        "gen_ai.tool.description",
        "gen_ai.agent.description",
        "copilot_chat.reasoning_content",
        "copilot_chat.prompt_context",
        "copilot_chat.prompt_instructions",
        "copilot_chat.markdown_content",
        "copilot_chat.event_details",
        "github.copilot.skill.content",
        "github.copilot.elicitation.message",
        "github.copilot.hook.error_message",
    }
)

# Identity attributes: only emitted when identity capture is opted in
# (Local harness sessions). The adapter never enables it and strips
# these keys if they arrive.
IDENTITY_ATTRIBUTES = frozenset(
    {
        "user.name",
        "process.user.name",
        "host.name",
        "enduser.pseudo.id",
    }
)

# Identifying repo attributes: the repository remote URL and GitHub org
# identify the user's work and are not needed for tracing. Branch and
# commit SHA are kept (opaque, short).
REPO_ATTRIBUTES = frozenset(
    {
        "github.copilot.git.repository",
        "github.copilot.github.org",
        "copilot_chat.repo.remote_url",
        "github.copilot.cwd",
        "github.copilot.git_root",
        "github.copilot.repository",
        "github.copilot.skill.path",
        "github.copilot.mcp.server.name",
    }
)

# Other native attributes that are never emitted: hostnames, finish
# reasons (array, not in the allowlist), and the legacy turn count
# (superseded by copilot.turn_count on the root — kept only there).
DROPPED_ATTRIBUTES = frozenset(
    {
        "server.address",
        "gen_ai.response.finish_reasons",
        "copilot_chat.repo.head_branch_name",
        "copilot_chat.repo.head_commit_hash",
    }
)

# Generic content fields (M3 safety net): never collected, never emitted.
DENIED_FIELDS = frozenset(
    {
        "prompt",
        "command",
        "answer",
        "content",
        "arguments",
        "result",
        "text",
        "input",
        "output",
    }
)

# Everything stripped (content + identity + repo + dropped + generic).
_STRIPPED = CONTENT_ATTRIBUTES | IDENTITY_ATTRIBUTES | REPO_ATTRIBUTES | DROPPED_ATTRIBUTES | DENIED_FIELDS

# Bounded outcome / error vocabularies (same as M3 schema v1).
OUTCOMES = frozenset(
    {"observed_success", "observed_failure", "cancelled", "timed_out", "incomplete", "unknown"}
)
ERROR_TYPES = frozenset(
    {"LLMError", "ToolError", "Timeout", "Cancelled", "Unknown"}
)

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX16_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

DEFAULT_QUEUE_MAXSIZE = 256
DEFAULT_STALE_TTL_SECONDS = 300.0
DEFAULT_DRAIN_TIMEOUT_SECONDS = 10.0
MAX_SEEN = 4096
MAX_NATIVE_INDEX = 4096


def _valid_trace_id(value):
    return isinstance(value, str) and bool(_HEX32_RE.match(value))


def _valid_span_id(value):
    return isinstance(value, str) and bool(_HEX16_RE.match(value))


def _valid_id(value):
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _parse_ns(value):
    """OTLP nanosecond timestamp (int or digit string) -> int, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
    return None


def _parse_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _parse_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _attrs_to_dict(attrs):
    """Convert an OTLP attribute list to a plain {key: value} dict.

    A flat dict passes through unchanged (ReadableSpan shape).
    """
    if isinstance(attrs, dict):
        return dict(attrs)
    out = {}
    for attr in attrs or []:
        key = attr.get("key")
        value = attr.get("value") or {}
        if "stringValue" in value:
            out[key] = value["stringValue"]
        elif "intValue" in value:
            try:
                out[key] = int(value["intValue"])
            except (TypeError, ValueError):
                out[key] = value["intValue"]
        elif "boolValue" in value:
            out[key] = bool(value["boolValue"])
        elif "doubleValue" in value:
            try:
                out[key] = float(value["doubleValue"])
            except (TypeError, ValueError):
                out[key] = value["doubleValue"]
        elif "arrayValue" in value:
            out[key] = str(value["arrayValue"])
        else:
            out[key] = None
    return out


def _attr_first(attrs, *keys):
    """First present (non-None) value across attribute aliases."""
    for key in keys:
        value = (attrs or {}).get(key)
        if value is not None:
            return value
    return None


def _hrtime_to_ns(value):
    """OTel JS HrTime [seconds, nanoseconds] -> Unix nanoseconds.

    The FileSpanExporter writes startTime/endTime as HrTime arrays;
    returns None for anything else. (FR7-01)
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        sec = _parse_int(value[0])
        nsec = _parse_int(value[1])
        if sec is not None and nsec is not None and sec >= 0 and 0 <= nsec < 1_000_000_000:
            return sec * 1_000_000_000 + nsec
    return None


def _span_time_ns(span, unix_key, hrtime_key):
    """Native span timestamp: UnixNano first, HrTime array fallback."""
    ns = _parse_ns(span.get(unix_key))
    if ns is not None:
        return ns
    return _hrtime_to_ns(span.get(hrtime_key))


def _resource_attrs(resource):
    """Flatten a resource block to a plain {key: value} dict.

    Accepts the OTLP/HTTP JSON list form (resource.attributes as a
    key/value list), the ReadableSpan flat-dict form
    (resource.attributes as a dict), and the top-level flat form
    ({"service.name": ...} directly on resource). (FR7-01)
    """
    if not isinstance(resource, dict):
        return {}
    attrs = resource.get("attributes")
    if isinstance(attrs, dict):
        return dict(attrs)
    if isinstance(attrs, list):
        return _attrs_to_dict(attrs)
    return {k: v for k, v in resource.items() if k != "attributes"}


def _normalize_event(ev):
    if not isinstance(ev, dict):
        return {"name": None, "timeUnixNano": None, "attributes": {}}
    attrs = ev.get("attributes")
    if isinstance(attrs, list):
        attrs = _attrs_to_dict(attrs)
    elif not isinstance(attrs, dict):
        attrs = {}
    t = _span_time_ns(ev, "timeUnixNano", "time")
    return {
        "name": ev.get("name"),
        "timeUnixNano": str(t) if t is not None else None,
        "attributes": dict(attrs),
    }


def _normalize_link(link):
    if not isinstance(link, dict):
        return {"traceId": None, "spanId": None, "attributes": {}}
    ctx = link.get("context") or {}
    attrs = link.get("attributes")
    if isinstance(attrs, list):
        attrs = _attrs_to_dict(attrs)
    elif not isinstance(attrs, dict):
        attrs = {}
    return {
        "traceId": link.get("traceId") or ctx.get("traceId"),
        "spanId": link.get("spanId") or ctx.get("spanId"),
        "attributes": dict(attrs),
    }


def _normalize_span(span):
    """Normalize one raw span dict into the internal flattened shape.

    Accepts both the OTLP/HTTP JSON span (startTimeUnixNano,
    parentSpanId, attributes as a key/value list) and the
    FileSpanExporter ReadableSpan shape (startTime/endTime as HrTime
    [seconds, nanoseconds] arrays, spanContext/parentSpanContext,
    attributes as a flat dict, nested resource.attributes).
    Already-flat spans pass through unchanged. (FR7-01)
    """
    if not isinstance(span, dict):
        return span
    span_ctx = span.get("spanContext") or {}
    parent_ctx = span.get("parentSpanContext") or {}
    attrs = span.get("attributes")
    if isinstance(attrs, list):
        attrs = _attrs_to_dict(attrs)
    elif not isinstance(attrs, dict):
        attrs = {}
    start_ns = _span_time_ns(span, "startTimeUnixNano", "startTime")
    end_ns = _span_time_ns(span, "endTimeUnixNano", "endTime")
    scope = span.get("scope") or span.get("instrumentationScope") or span.get("instrumentationLibrary") or {}
    if not isinstance(scope, dict):
        scope = {}
    return {
        "traceId": span.get("traceId") or span_ctx.get("traceId"),
        "spanId": span.get("spanId") or span_ctx.get("spanId"),
        "parentSpanId": span.get("parentSpanId") or parent_ctx.get("spanId") or "",
        "name": span.get("name"),
        "kind": span.get("kind"),
        "startTimeUnixNano": str(start_ns) if start_ns is not None else None,
        "endTimeUnixNano": str(end_ns) if end_ns is not None else None,
        "attributes": dict(attrs),
        "events": [_normalize_event(ev) for ev in (span.get("events") or [])],
        "links": [_normalize_link(link) for link in (span.get("links") or [])],
        "status": span.get("status") or {},
        "resource": _resource_attrs(span.get("resource")),
        "scope": {
            "name": scope.get("name"),
            "version": scope.get("version"),
        },
    }


# Session-id keys, in precedence order: the GenAI conversation id,
# then the Copilot chat-session aliases. (FR7-03)
_SESSION_ID_KEYS = (
    "gen_ai.conversation.id",
    "copilot_chat.chat_session_id",
    "copilot_chat.session_id",
)


def _resolve_session_id(attrs):
    """First non-empty session id across the conversation-id keys."""
    for key in _SESSION_ID_KEYS:
        value = (attrs or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def flatten_payload(payload):
    """Flatten an OTLP/HTTP JSON payload into a list of span dicts.

    Same span shape as receiver.flatten_payload (M1): traceId, spanId,
    parentSpanId, name, kind, start/endTimeUnixNano, attributes, links,
    status, events, resource, scope.
    """
    spans = []
    for resource_span in payload.get("resourceSpans") or []:
        resource_attrs = _resource_attrs(resource_span.get("resource"))
        for scope_span in resource_span.get("scopeSpans") or []:
            scope = scope_span.get("scope") or {}
            for span in scope_span.get("spans") or []:
                status = span.get("status") or {}
                start_ns = _span_time_ns(span, "startTimeUnixNano", "startTime")
                end_ns = _span_time_ns(span, "endTimeUnixNano", "endTime")
                parent_ctx = span.get("parentSpanContext") or {}
                spans.append(
                    {
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "parentSpanId": span.get("parentSpanId")
                        or parent_ctx.get("spanId")
                        or "",
                        "name": span.get("name"),
                        "kind": span.get("kind"),
                        "startTimeUnixNano": str(start_ns) if start_ns is not None else None,
                        "endTimeUnixNano": str(end_ns) if end_ns is not None else None,
                        "attributes": _attrs_to_dict(span.get("attributes")),
                        "events": [
                            {
                                "name": ev.get("name"),
                                "timeUnixNano": ev.get("timeUnixNano"),
                                "attributes": _attrs_to_dict(ev.get("attributes")),
                            }
                            for ev in (span.get("events") or [])
                        ],
                        "links": [
                            {
                                "traceId": link.get("traceId"),
                                "spanId": link.get("spanId"),
                                "attributes": _attrs_to_dict(link.get("attributes")),
                            }
                            for link in (span.get("links") or [])
                        ],
                        "status": {
                            "code": status.get("code", 0),
                            "message": status.get("message"),
                        },
                        "resource": resource_attrs,
                        "scope": {
                            "name": scope.get("name"),
                            "version": scope.get("version"),
                        },
                    }
                )
    return spans


def _map_error_type(error):
    """Map a native error.type value onto the bounded ERROR_TYPES enum.

    Accepts a plain string (the documented shape: an error class) or
    an error dict / exception (defensive). Unknown shapes map to
    "Unknown" — raw error text is never emitted.
    """
    if error is None:
        return None
    if isinstance(error, dict):
        raw = (
            error.get("type")
            or error.get("kind")
            or error.get("name")
            or error.get("code")
            or ""
        )
    elif isinstance(error, BaseException):
        raw = type(error).__name__
    else:
        raw = str(error)
    raw = str(raw).strip()
    if raw in ERROR_TYPES:
        return raw
    low = raw.lower().replace("_", "").replace(" ", "")
    if "timeout" in low or "deadlineexceeded" in low:
        return "Timeout"
    if "cancel" in low:
        return "Cancelled"
    if "tool" in low:
        return "ToolError"
    if "llm" in low or "model" in low or "api" in low or "provider" in low:
        return "LLMError"
    return "Unknown"


def _status_outcome(code):
    """Native OTLP status code -> (is_error, schema-v1 outcome)."""
    if code in (2, "2", "ERROR", "STATUS_CODE_ERROR"):
        return True, "observed_failure"
    if code in (1, "1", "OK", "STATUS_CODE_OK"):
        return False, "observed_success"
    return False, "unknown"


class _CountingExporter:
    """Wraps an exporter and counts failed exports (loss visibility)."""

    def __init__(self, inner, on_failure):
        self._inner = inner
        self._on_failure = on_failure

    def export(self, spans):
        result = self._inner.export(spans)
        if result is not SpanExportResult.SUCCESS:
            self._on_failure()
        return result

    def shutdown(self):
        self._inner.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self._inner.force_flush(timeout_millis)


class CopilotObserver:
    """Read-only Copilot OTel adapter. See the module docstring."""

    def __init__(
        self,
        endpoint,
        service_name="copilot",
        queue_maxsize=DEFAULT_QUEUE_MAXSIZE,
        stale_ttl_seconds=DEFAULT_STALE_TTL_SECONDS,
    ):
        self._endpoint = endpoint
        self._stale_ttl = float(stale_ttl_seconds)
        self._queue = queue.Queue(maxsize=int(queue_maxsize))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._counters = {
            "accepted": 0,
            "dropped_queue_full": 0,
            "duplicates": 0,
            "invalid_spans": 0,
            "stripped_fields": 0,
            "handle_errors": 0,
            "export_failures": 0,
            "stale_closed": 0,
            "incomplete_closed": 0,
            "out_of_scope": 0,
            "unknown_operation": 0,
            "parent_unresolved": 0,
            "session_fallback": 0,
            "late_root_dropped": 0,
            "late_child": 0,
            "lazy_roots_folded": 0,
            "session_start_seen": 0,
            "agent_turn_seen": 0,
            "tool_call_event_seen": 0,
            "user_event_seen": 0,
            "inference_event_seen": 0,
            "unknown_event_seen": 0,
            "ingest_errors": 0,
            "ingest_lines_skipped": 0,
            "ingest_lines_invalid": 0,
        }
        self._seen = {}  # (traceId, spanId) -> time (bounded dedup)
        # (traceId, spanId) -> (operation, parentSpanId, session_id)
        # (session_id resolved via _resolve_session_id, FR7-03)
        self._native_index = {}
        # (traceId, spanId) -> Context carrying the fleet span (for
        # native parent resolution; spans may be ended already).
        self._native_contexts = {}
        # (traceId, rootSpanId|"") -> {"span", "ctx", "session_id",
        # "turn_id", "ended", "lazy", "started_ns", "created_wall"}
        # Only OPEN roots live here; ended roots move to _closed_roots.
        self._turn_roots = {}
        # (traceId, rootSpanId) -> Context of the ended root, kept so a
        # late child can still parent to its turn (bounded, FIFO).
        self._closed_roots = {}

        self._provider = TracerProvider(
            resource=Resource(
                {
                    "service.name": service_name,
                    "agent.name": "copilot",
                    "observation.source": "otel-export",
                }
            )
        )
        self._provider.add_span_processor(
            SimpleSpanProcessor(
                _CountingExporter(
                    OTLPJSONExporter(endpoint), self._count_export_failure
                )
            )
        )
        self._tracer = self._provider.get_tracer("fleet.copilot-observer", "1.0.0")
        self._worker = threading.Thread(
            target=self._run, name="copilot-observer-worker", daemon=True
        )
        self._worker.start()

    # -- counters ----------------------------------------------------------
    def _count(self, key, n=1):
        with self._lock:
            self._counters[key] += n

    def _count_export_failure(self):
        self._count("export_failures")

    def diagnostics(self):
        with self._lock:
            return dict(self._counters)

    # -- ingest entry points (never raise, never block on I/O) ------------
    def handle_payload(self, payload):
        """Validate + enqueue one OTLP/HTTP JSON payload. Returns count
        enqueued. Never raises; failures are counted."""
        try:
            if not isinstance(payload, dict):
                self._count("invalid_spans")
                return 0
            spans = flatten_payload(payload)
            return self._index_and_enqueue(spans)
        except Exception:
            self._count("handle_errors")
            return 0

    def handle_spans(self, spans):
        """Validate + enqueue already-flattened span dicts. Returns
        count enqueued. Never raises."""
        try:
            if not isinstance(spans, list):
                self._count("invalid_spans")
                return 0
            return self._index_and_enqueue(spans)
        except Exception:
            self._count("handle_errors")
            return 0

    def ingest_jsonl(self, path):
        """Read the Copilot file exporter's JSONL output.

        One JSON value per line. Each line is accepted as either a full
        OTLP payload (``resourceSpans``) or a single span — the
        FileSpanExporter ReadableSpan shape (``spanContext`` /
        ``parentSpanContext``, HrTime ``startTime``/``endTime``,
        nested ``resource.attributes``) or a flattened span
        (``traceId`` + ``spanId``). Metric/log lines
        (``resourceMetrics`` / ``resourceLogs``, ``scopeMetrics`` /
        ``scopeLogs``, ``type`` == "metric", ``dataPoints``, OTel JS
        log records with ``hrTime`` + ``body``) are skipped and
        counted, never treated as errors. Span lines are collected
        across the file and handed to ``handle_spans`` in one batch so
        parents order before children even when the file lists
        children first. Returns a summary dict. Never raises.
        """
        summary = {
            "lines": 0,
            "spans_enqueued": 0,
            "skipped_non_trace": 0,
            "invalid_lines": 0,
            "ingest_errors": 0,
        }
        collected = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    summary["lines"] += 1
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        summary["invalid_lines"] += 1
                        self._count("ingest_lines_invalid")
                        continue
                    if not isinstance(obj, dict):
                        summary["invalid_lines"] += 1
                        self._count("ingest_lines_invalid")
                        continue
                    if "resourceSpans" in obj:
                        collected.extend(flatten_payload(obj))
                    elif (
                        "resourceMetrics" in obj
                        or "resourceLogs" in obj
                        or "scopeMetrics" in obj
                        or "scopeLogs" in obj
                        or obj.get("type") in ("metric", "log")
                        or "dataPoints" in obj
                        or "hrTime" in obj
                        or "hrTimeObserved" in obj
                        or "severityNumber" in obj
                        or "severityText" in obj
                    ):
                        # FileLogExporter / FileMetricExporter lines.
                        # A log record carries a spanContext while its
                        # span is still active, so this skip MUST run
                        # before the span branches below: otherwise the
                        # log is collected as a span, fails
                        # _operation_of, and poisons _seen for the real
                        # span that shares its ids (FR8-01).
                        summary["skipped_non_trace"] += 1
                        self._count("ingest_lines_skipped")
                    elif "traceId" in obj and "spanId" in obj:
                        collected.append(obj)
                    elif (
                        isinstance(obj.get("spanContext"), dict)
                        and "traceId" in obj["spanContext"]
                        and "spanId" in obj["spanContext"]
                    ):
                        # ReadableSpan line: spanContext carries the ids.
                        collected.append(obj)
                    else:
                        summary["invalid_lines"] += 1
                        self._count("ingest_lines_invalid")
            if collected:
                summary["spans_enqueued"] = self.handle_spans(collected)
        except Exception:
            summary["ingest_errors"] += 1
            self._count("ingest_errors")
        return summary

    # -- batch indexing + ordering -----------------------------------------
    def _operation_of(self, span):
        attrs = span.get("attributes") or {}
        op = attrs.get("gen_ai.operation.name")
        if not op:
            # FileSpanExporter span names carry a target suffix
            # ("chat github.copilot", "hook PreToolUse"); the operation
            # is the first token. (FR7-04)
            parts = (span.get("name") or "").split()
            op = parts[0] if parts else ""
        if op == "hook":
            op = "execute_hook"
        return op if op in OPERATIONS else None

    def _index_and_enqueue(self, spans):
        """Filter, dedup, index, topologically order, enqueue."""
        # Pass 1: validate IDs, filter scope/operation, dedup, index.
        kept = []
        for raw_span in spans:
            # Normalize the FileSpanExporter ReadableSpan shape into
            # the internal flattened shape (HrTime timestamps,
            # parentSpanContext, nested resource attributes). Already
            # flat spans pass through unchanged. (FR7-01)
            span = _normalize_span(raw_span)
            if not isinstance(span, dict):
                self._count("invalid_spans")
                continue
            trace_id = span.get("traceId")
            span_id = span.get("spanId")
            if not _valid_trace_id(trace_id) or not _valid_span_id(span_id):
                self._count("invalid_spans")
                continue
            trace_id = trace_id.lower()
            span_id = span_id.lower()
            key = (trace_id, span_id)
            attrs = span.get("attributes") or {}
            resource = span.get("resource") or {}
            service = resource.get("service.name")
            # Out-of-scope services are matched case-insensitively
            # ("Claude-Code", "COPILOT-CHAT"). (FR7-05)
            if service is not None and (
                not isinstance(service, str) or service.lower() not in COPILOT_SERVICES
            ):
                self._count("out_of_scope")
                continue
            agent_name = attrs.get("gen_ai.agent.name")
            if (
                isinstance(agent_name, str)
                and agent_name.strip().lower() in ("claude", "claude-code")
            ):
                # Claude agent spans (invoke_agent claude) are not
                # Copilot spans; Claude Code has its own M2 path.
                self._count("out_of_scope")
                continue
            op = self._operation_of(span)
            if op is None:
                self._count("unknown_operation")
                continue
            # Dedup AFTER the scope/operation filters: a rejected or
            # unknown-operation object must never poison _seen for the
            # real span that shares its ids (FR8-01).
            with self._lock:
                if key in self._seen:
                    self._counters["duplicates"] += 1
                    continue
                if len(self._seen) > MAX_SEEN:
                    self._seen.pop(next(iter(self._seen)))
                self._seen[key] = time.time()
            parent_id = span.get("parentSpanId") or ""
            if parent_id and not _valid_span_id(parent_id):
                parent_id = ""
            conv = _resolve_session_id(attrs)
            with self._lock:
                if len(self._native_index) > MAX_NATIVE_INDEX:
                    self._native_index.pop(next(iter(self._native_index)))
                self._native_index[key] = (op, parent_id.lower() if parent_id else "", conv)
            kept.append((span, trace_id, span_id, op))
        # Pass 2: parents before children within the batch (Kahn's
        # algorithm on parentSpanId edges inside the batch; ties and
        # cycles fall back to start-time order).
        ordered = self._topological_order(kept)
        enqueued = 0
        for span, trace_id, span_id, op in ordered:
            try:
                self._queue.put_nowait((span, trace_id, span_id, op))
            except queue.Full:
                self._count("dropped_queue_full")
                continue
            self._count("accepted")
            enqueued += 1
        return enqueued

    def _topological_order(self, kept):
        if len(kept) <= 1:
            return kept
        by_key = {(t, s): i for i, (_, t, s, _) in enumerate(kept)}
        indegree = {i: 0 for i in range(len(kept))}
        children = {i: [] for i in range(len(kept))}
        for i, (span, trace_id, span_id, _) in enumerate(kept):
            parent = (span.get("parentSpanId") or "").lower()
            if parent and (trace_id, parent) in by_key:
                j = by_key[(trace_id, parent)]
                children[j].append(i)
                indegree[i] += 1

        def _start_ns(item):
            return _parse_ns(item[0].get("startTimeUnixNano")) or 0

        ready = sorted(
            [i for i in range(len(kept)) if indegree[i] == 0], key=lambda i: _start_ns(kept[i])
        )
        ordered = []
        while ready:
            i = ready.pop(0)
            ordered.append(kept[i])
            nxt = sorted(children[i], key=lambda j: _start_ns(kept[j]))
            for j in nxt:
                indegree[j] -= 1
                if indegree[j] == 0:
                    ready.append(j)
            ready.sort(key=lambda k: _start_ns(kept[k]))
        if len(ordered) < len(kept):
            # Cycle (or a missed edge): append the rest in start order.
            rest = [kept[i] for i in range(len(kept)) if kept[i] not in ordered]
            rest.sort(key=_start_ns)
            ordered.extend(rest)
        return ordered

    # -- worker ------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._prune_stale()
                continue
            try:
                self._process(item)
            except Exception:
                self._count("handle_errors")
            finally:
                self._queue.task_done()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._process(item)
            except Exception:
                self._count("handle_errors")
            finally:
                self._queue.task_done()

    def drain(self, timeout=DEFAULT_DRAIN_TIMEOUT_SECONDS):
        """Block until the queue is empty (all enqueued spans processed)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.empty():
                return True
            try:
                self._queue.join()
                return True
            except Exception:
                return False
        return self._queue.empty()

    def shutdown(self, timeout=DEFAULT_DRAIN_TIMEOUT_SECONDS):
        """Bounded teardown. Called only by the owner at process teardown."""
        self._stop.set()
        self._worker.join(timeout=timeout)
        try:
            self._provider.force_flush(timeout_millis=int(timeout * 1000))
        except Exception:
            pass
        try:
            self._provider.shutdown()
        except Exception:
            pass

    # -- span processing (worker thread only) ------------------------------
    def _process(self, item):
        span, trace_id, span_id, op = item
        self._count_span_events(span)
        if op == "invoke_agent":
            self._process_invoke_agent(span, trace_id, span_id)
        else:
            self._process_operation(span, trace_id, span_id, op)
        self._prune_stale()

    def _count_span_events(self, span):
        """Span events are counted, never emitted as fleet spans."""
        for ev in span.get("events") or []:
            name = ev.get("name") or ""
            if name == "copilot_chat.session.start":
                self._count("session_start_seen")
            elif name == "copilot_chat.agent.turn":
                self._count("agent_turn_seen")
            elif name == "copilot_chat.tool.call":
                self._count("tool_call_event_seen")
            elif name in (
                "copilot_chat.edit.feedback",
                "copilot_chat.edit.hunk.action",
                "copilot_chat.inline.done",
                "copilot_chat.edit.survival",
                "copilot_chat.user.feedback",
                "copilot_chat.cloud.session.invoke",
            ):
                # User-behavior telemetry is out of the fleet's purpose.
                self._count("user_event_seen")
            elif name == "gen_ai.client.inference.operation.details":
                self._count("inference_event_seen")
            else:
                self._count("unknown_event_seen")

    def _session_id_for(self, trace_id, span_id, attrs):
        """Session id for a span: its own, else the enclosing
        invoke_agent's (via the native index), else the trace fallback."""
        own = _resolve_session_id(attrs)
        if own:
            return own
        # Walk up the native parent chain to the enclosing invoke_agent.
        seen = set()
        cur = span_id
        while cur and cur not in seen:
            seen.add(cur)
            entry = self._native_index.get((trace_id, cur))
            if entry is None:
                break
            op, parent, conv_up = entry
            if conv_up:
                return conv_up
            if op == "invoke_agent":
                break
            cur = parent
        self._count("session_fallback")
        return "trace:%s" % trace_id

    def _root_span_id_for(self, trace_id, span_id):
        """Enclosing invoke_agent spanId for a span, or '' if unknown.

        In streaming export the parent invoke_agent may not have
        finished yet, so it is not in the index; the highest known
        ancestor parentSpanId is then returned (FR7-02) so the child
        keys its lazy root under (trace_id, root_span_id) and the
        root folds into it when it arrives.
        """
        seen = set()
        cur = span_id
        while cur and cur not in seen:
            seen.add(cur)
            entry = self._native_index.get((trace_id, cur))
            if entry is None:
                return cur if cur != span_id else ""
            op, parent, _ = entry
            if op == "invoke_agent":
                return cur
            cur = parent
        return ""

    def _strip_counts(self, attrs):
        stripped = 0
        for key in list(attrs):
            if key in _STRIPPED:
                stripped += 1
        if stripped:
            self._count("stripped_fields", stripped)
        return stripped

    def _root_attrs(self, span, session_id, turn_id):
        attrs = span.get("attributes") or {}
        self._strip_counts(attrs)
        out = {
            "agent.name": "copilot",
            "agent.role": "observed-agent",
            "session.id": session_id,
            "turn.id": turn_id,
            "observation.source": "otel-export",
            "observation.completeness": "complete",
        }
        self._add_common_attrs(out, attrs)
        turn_count = _parse_int(
            _attr_first(
                attrs, "copilot_chat.turn_count", "github.copilot.turn_count"
            )
        )
        if turn_count is not None:
            out["copilot.turn_count"] = turn_count
        return out

    def _add_common_attrs(self, out, attrs):
        """Copy the allowlisted native metadata keys into fleet attrs."""
        for key, fleet_key in (
            ("gen_ai.provider.name", "gen_ai.provider.name"),
            ("gen_ai.request.model", "gen_ai.request.model"),
            ("gen_ai.response.model", "gen_ai.response.model"),
            ("gen_ai.usage.input_tokens", "gen_ai.usage.input_tokens"),
            ("gen_ai.usage.output_tokens", "gen_ai.usage.output_tokens"),
            ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read.input_tokens"),
            ("gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_creation.input_tokens"),
            ("gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning.output_tokens"),
            ("github.copilot.agent.type", "copilot.agent.type"),
            ("github.copilot.git.branch", "copilot.git.branch"),
            ("github.copilot.git.commit_sha", "copilot.git.commit_sha"),
        ):
            value = attrs.get(key)
            if value is None:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                out[fleet_key] = value
            elif isinstance(value, str) and value != "":
                out[fleet_key] = value[:MAX_ATTRIBUTE_CHARS]

    def _turn_root(self, trace_id, root_key, session_id, turn_id, start_ns):
        """Get or create the (possibly lazy) turn root for a turn."""
        key = (trace_id, root_key)
        root = self._turn_roots.get(key)
        if root is not None:
            return root
        span = self._tracer.start_span(
            "agent.run",
            context=Context(),
            kind=SpanKind.INTERNAL,
            start_time=start_ns,
            attributes=sanitize_attributes(
                {
                    "agent.name": "copilot",
                    "agent.role": "observed-agent",
                    "session.id": session_id,
                    "turn.id": turn_id,
                    "observation.source": "otel-export",
                    "observation.completeness": "complete",
                }
            ),
        )
        ctx = _span_context(span)
        root = {
            "span": span,
            "ctx": ctx,
            "session_id": session_id,
            "turn_id": turn_id,
            "ended": False,
            "lazy": True,
            "started_ns": start_ns,
            "created_wall": time.time(),
        }
        self._turn_roots[key] = root
        if root_key:
            # Keyed lazy root: register the native context so direct
            # child spans arriving in later streaming batches resolve
            # their parent instead of counting parent_unresolved
            # (FR8-02). The real invoke_agent overwrites this entry
            # with its own context when it folds the lazy root.
            if len(self._native_contexts) > MAX_NATIVE_INDEX:
                self._native_contexts.pop(next(iter(self._native_contexts)))
            self._native_contexts[(trace_id, root_key)] = ctx
        return root

    def _process_invoke_agent(self, span, trace_id, span_id):
        attrs = span.get("attributes") or {}
        session_id = self._session_id_for(trace_id, span_id, attrs)
        turn_id = "%s:%s" % (trace_id, span_id)
        start_ns = _parse_ns(span.get("startTimeUnixNano"))
        end_ns = _parse_ns(span.get("endTimeUnixNano"))
        if end_ns is not None and start_ns is not None and end_ns < start_ns:
            end_ns = start_ns
        # Subagent nesting: a child invoke_agent parents to the
        # execute_tool span that invoked it (native parentSpanId).
        parent_ctx = None
        parent_id = (span.get("parentSpanId") or "").lower()
        if parent_id and _valid_span_id(parent_id):
            parent_ctx = self._native_contexts.get((trace_id, parent_id))
            if parent_ctx is None:
                self._count("parent_unresolved")
        lazy_key = (trace_id, "")
        lazy = self._turn_roots.get(lazy_key)
        if lazy is not None and not parent_id:
            # Children arrived before their root: fold the root's
            # attributes into the lazy root and close it at the
            # invoke_agent end time. One agent.run per turn, never two.
            # (If the lazy root was created before the subagent's
            # parent tool span was known, it keeps its own fresh trace
            # instead of nesting under that tool span; the turn
            # correlation via turn.id still holds.)
            # FR7-02: only a top-level invoke_agent (no parent span)
            # may fold the unkeyed lazy root — a subagent invoke_agent
            # with a parent_span_id must never hijack the parent
            # turn's lazy root.
            root = lazy
            del self._turn_roots[lazy_key]
            root["session_id"] = session_id
            root["turn_id"] = turn_id
            root["lazy"] = False
            for k, v in sanitize_attributes(self._root_attrs(span, session_id, turn_id)).items():
                try:
                    root["span"].set_attribute(k, v)
                except Exception:
                    pass
            self._count("lazy_roots_folded")
            self._close_root(root, span, end_ns, (trace_id, span_id))
            return
        if (trace_id, span_id) in self._closed_roots:
            # The root already closed (stale prune): the turn already
            # exported incomplete. Do not emit a second agent.run.
            self._count("late_root_dropped")
            return
        existing = self._turn_roots.get((trace_id, span_id))
        if existing is not None:
            # Keyed lazy root: children arrived before their
            # invoke_agent (streaming export — the parent had not
            # finished when the children were exported, so they keyed
            # the lazy root under (trace_id, root_span_id) per
            # _root_span_id_for, FR7-02). Fold the root attributes
            # onto it and record the native context before ending it,
            # so agent.run retains all root attributes
            # (gen_ai.request.model, gen_ai.usage.*, copilot.agent.type,
            # copilot.git.*, copilot.turn_count).
            existing["session_id"] = session_id
            existing["turn_id"] = turn_id
            existing["lazy"] = False
            for k, v in sanitize_attributes(self._root_attrs(span, session_id, turn_id)).items():
                try:
                    existing["span"].set_attribute(k, v)
                except Exception:
                    pass
            self._native_contexts[(trace_id, span_id)] = existing["ctx"]
            self._count("lazy_roots_folded")
            self._close_root(existing, span, end_ns, (trace_id, span_id))
            return
        # Normal case: the root arrives first (or with no children).
        root_span = self._tracer.start_span(
            "agent.run",
            context=parent_ctx if parent_ctx is not None else Context(),
            kind=SpanKind.INTERNAL,
            start_time=start_ns,
            attributes=sanitize_attributes(self._root_attrs(span, session_id, turn_id)),
        )
        ctx = _span_context(root_span)
        self._native_contexts[(trace_id, span_id)] = ctx
        root = {
            "span": root_span,
            "ctx": ctx,
            "session_id": session_id,
            "turn_id": turn_id,
            "ended": True,
            "lazy": False,
            "started_ns": start_ns,
            "created_wall": time.time(),
        }
        self._close_root(root, span, end_ns, (trace_id, span_id))

    def _close_root(self, root, span, end_ns, key):
        """Set the root's outcome/status from the invoke_agent span, end
        it, and move it to _closed_roots so late children can still
        parent to its turn."""
        is_error, _ = _status_outcome((span.get("status") or {}).get("code", 0))
        attrs = span.get("attributes") or {}
        error_type = _map_error_type(attrs.get("error.type"))
        if is_error:
            if error_type:
                try:
                    root["span"].set_attribute("error.type", error_type)
                except Exception:
                    pass
            root["span"].set_status(Status(StatusCode.ERROR, "Agent call failed; details withheld"))
        elif (span.get("status") or {}).get("code") in (1, "1", "OK", "STATUS_CODE_OK"):
            root["span"].set_status(Status(StatusCode.OK))
        try:
            root["span"].end(end_time=end_ns)
        except Exception:
            pass
        root["ended"] = True
        self._turn_roots.pop(key, None)
        if len(self._closed_roots) > MAX_NATIVE_INDEX:
            self._closed_roots.pop(next(iter(self._closed_roots)))
        self._closed_roots[key] = root["ctx"]

    def _process_operation(self, span, trace_id, span_id, op):
        attrs = span.get("attributes") or {}
        self._strip_counts(attrs)
        root_key = self._root_span_id_for(trace_id, span_id)
        session_id = self._session_id_for(trace_id, span_id, attrs)
        turn_id = "%s:%s" % (trace_id, root_key) if root_key else trace_id
        start_ns = _parse_ns(span.get("startTimeUnixNano"))
        end_ns = _parse_ns(span.get("endTimeUnixNano"))
        if end_ns is not None and start_ns is not None and end_ns < start_ns:
            end_ns = start_ns
        root = self._turn_roots.get((trace_id, root_key))
        if root is None:
            closed_ctx = self._closed_roots.get((trace_id, root_key))
            if closed_ctx is not None:
                # Late child after its turn root closed: parent to the
                # ended root's context (still valid for parenting).
                self._count("late_child")
                root = {"ctx": closed_ctx, "ended": True}
            else:
                root = self._turn_root(trace_id, root_key, session_id, turn_id, start_ns)
        parent_ctx = root["ctx"]
        parent_id = (span.get("parentSpanId") or "").lower()
        if parent_id and _valid_span_id(parent_id):
            resolved = self._native_contexts.get((trace_id, parent_id))
            if resolved is not None:
                parent_ctx = resolved
            else:
                self._count("parent_unresolved")
        fleet_attrs = {
            "agent.name": "copilot",
            "session.id": session_id,
            "turn.id": turn_id,
            "request.id": self._request_id_for(span, span_id, op),
            "observation.source": "otel-export",
            "observation.completeness": "complete",
        }
        self._add_common_attrs(fleet_attrs, attrs)
        self._add_operation_attrs(fleet_attrs, attrs, op)
        fleet_span = self._tracer.start_span(
            _SPAN_NAMES[op],
            context=parent_ctx,
            kind=SpanKind.INTERNAL,
            start_time=start_ns,
            attributes=sanitize_attributes(fleet_attrs),
        )
        self._native_contexts[(trace_id, span_id)] = _span_context(fleet_span)
        is_error, _ = _status_outcome((span.get("status") or {}).get("code", 0))
        error_type = _map_error_type(attrs.get("error.type"))
        if is_error:
            if error_type:
                try:
                    fleet_span.set_attribute("error.type", error_type)
                except Exception:
                    pass
            fleet_span.set_status(Status(StatusCode.ERROR, "Agent call failed; details withheld"))
        elif (span.get("status") or {}).get("code") in (1, "1", "OK", "STATUS_CODE_OK"):
            fleet_span.set_status(Status(StatusCode.OK))
        try:
            fleet_span.end(end_time=end_ns)
        except Exception:
            pass

    def _request_id_for(self, span, span_id, op):
        attrs = span.get("attributes") or {}
        if op == "execute_tool":
            call_id = attrs.get("gen_ai.tool.call.id")
            if isinstance(call_id, str) and _valid_id(call_id):
                return call_id
        return span_id

    def _add_operation_attrs(self, out, attrs, op):
        if op == "chat":
            ttft = _parse_int(attrs.get("copilot_chat.time_to_first_token"))
            if ttft is not None:
                out["copilot.time_to_first_token_ms"] = ttft
        elif op == "execute_tool":
            tool = attrs.get("gen_ai.tool.name")
            if isinstance(tool, str) and tool != "":
                out["gen_ai.tool.name"] = tool[:MAX_ATTRIBUTE_CHARS]
            tool_type = attrs.get("gen_ai.tool.type")
            if isinstance(tool_type, str) and tool_type != "":
                out["copilot.tool.type"] = tool_type[:MAX_ATTRIBUTE_CHARS]
            for key, fleet_key in (
                ("github.copilot.tool.parameters.mcp_tool_name", "copilot.mcp_tool_name"),
                ("github.copilot.tool.parameters.skill_name", "copilot.skill_name"),
                ("github.copilot.tool.parameters.edit_type", "copilot.edit_type"),
            ):
                value = attrs.get(key)
                if isinstance(value, str) and value != "":
                    out[fleet_key] = value[:MAX_ATTRIBUTE_CHARS]
            # SDK alias for the MCP server name hash (FR7-04).
            mcp_hash = _attr_first(
                attrs,
                "github.copilot.tool.parameters.mcp_server_name_hash",
                "github.copilot.mcp.server.name_hash",
            )
            if isinstance(mcp_hash, str) and mcp_hash != "":
                out["copilot.mcp_server_name_hash"] = mcp_hash[:MAX_ATTRIBUTE_CHARS]
        elif op == "execute_hook":
            decision = attrs.get("github.copilot.hook.decision")
            if isinstance(decision, str) and decision != "":
                out["copilot.hook.decision"] = decision[:MAX_ATTRIBUTE_CHARS]
            duration = _parse_float(attrs.get("github.copilot.hook.duration"))
            if duration is not None:
                out["copilot.hook.duration_s"] = duration
            hook_type = _attr_first(
                attrs, "copilot_chat.hook_type", "github.copilot.hook.type"
            )
            if isinstance(hook_type, str) and hook_type != "":
                out["copilot.hook.type"] = hook_type[:MAX_ATTRIBUTE_CHARS]
            result = attrs.get("copilot_chat.hook_result_kind")
            if isinstance(result, str) and result != "":
                out["copilot.hook.result"] = result[:MAX_ATTRIBUTE_CHARS]

    def _prune_stale(self):
        now = time.time()
        for key in list(self._turn_roots):
            root = self._turn_roots[key]
            # Staleness is wall-clock age since the root was created by
            # the worker (native timestamps may be absent or skewed).
            if now - root["created_wall"] > self._stale_ttl:
                try:
                    root["span"].set_attribute("observation.completeness", "incomplete")
                except Exception:
                    pass
                try:
                    root["span"].end()
                except Exception:
                    pass
                root["ended"] = True
                self._turn_roots.pop(key, None)
                if len(self._closed_roots) > MAX_NATIVE_INDEX:
                    self._closed_roots.pop(next(iter(self._closed_roots)))
                self._closed_roots[key] = root["ctx"]
                self._count("stale_closed")
                self._count("incomplete_closed")
        # Bound the native-context map (FIFO cap; entries carry no
        # timestamp of their own).
        with self._lock:
            while len(self._native_contexts) > MAX_NATIVE_INDEX:
                self._native_contexts.pop(next(iter(self._native_contexts)))


def _span_context(span):
    """The Context carrying a just-started span (explicit parenting)."""
    from opentelemetry.trace import set_span_in_context

    return set_span_in_context(span)
