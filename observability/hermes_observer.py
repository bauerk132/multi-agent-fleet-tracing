"""Hermes observer — Milestone 3 (isolated Hermes observer).

Read-only observer for Hermes hook events. It turns hook deliveries into
OTel spans and exports them to the local OTLP/HTTP JSON receiver. It never
modifies model/tool behavior, never touches conversation text, and never
performs network I/O on a hook callback: a callback only validates and
enqueues small allowlisted metadata and returns None. A single worker
thread owns all span lifecycle and export (off-path).

Design (plan §5, §12, §13):
- Schema v1: a small stable event schema (see EVENT_TYPES, ALLOWED_FIELDS).
  Prompt/command/answer/content fields are denied and stripped; only
  allowlisted metadata is ever emitted.
- Explicit parenting, never ambient: spans are created with
  tracer.start_span() + an explicit parent Context and end(). Independent
  callbacks never share a current-span context. A turn root span
  ("agent.run") is opened lazily on the first event of a turn; operation
  spans parent to it, or to an explicitly named enclosing request
  (parent_request_id), never by arrival order.
- Lifecycle: on_session_end is run/turn-scoped — it reconciles the turn
  (closes orphaned spans as incomplete) and flushes, but never shuts the
  provider down. on_session_finalize / on_session_reset clean up a
  session. The provider is process-scoped; shutdown() is called only by
  the owner at process teardown, never by a hook callback.
- Bounded everything: bounded queue (overflow drops + counts), bounded
  dedup set, stale-event TTL, dropped/partial counters. Loss is visible;
  the agent is never blocked.
- Privacy: all span attributes go through sanitize_attributes (allowlist
  + 256-char guard); errors are classified with a bounded enum, never
  raw text.

The Hermes plugin surface itself (ctx.register_hook signatures, payload
shapes) is validated against the installed build in the M3 isolated
contract test on the dev machine; this module is Hermes-independent and
is fully testable here with synthetic hook events.
"""

from __future__ import annotations

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

from fleet_trace import MAX_ATTRIBUTE_CHARS, sanitize_attributes
from otlp_json import OTLPJSONExporter

# ---------------------------------------------------------------------------
# Schema v1
# ---------------------------------------------------------------------------

EVENT_TYPES = frozenset(
    {
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_tool_call",
        "post_tool_call",
        "subagent_start",
        "subagent_stop",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "on_human_input_request",
        "on_human_input_resolved",
    }
)

# Fields a hook event may carry. Anything else is ignored.
ALLOWED_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "session_id",
        "turn_id",
        "request_id",
        "parent_request_id",
        "attempt",
        "outcome",
        "error_type",
        "model",
        "tool_name",
        "timestamp_ns",
    }
)

# Content fields: never collected, never emitted. Present in an event they
# are stripped and counted.
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

# Bounded outcome / error vocabularies. Unknown values map to "unknown".
OUTCOMES = frozenset(
    {"observed_success", "observed_failure", "cancelled", "timed_out", "incomplete", "unknown"}
)
ERROR_TYPES = frozenset(
    {"LLMError", "ToolError", "Timeout", "Cancelled", "Unknown"}
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Span kinds (operation -> span name, open/close event types).
_OPEN_EVENTS = {
    "pre_api_request": ("llm.call", "llm"),
    "pre_tool_call": ("tool.exec", "tool"),
    "subagent_start": ("subagent.spawn", "subagent"),
    "on_human_input_request": ("approval.wait", "approval"),
}
_CLOSE_EVENTS = {
    "post_api_request": "llm",
    "api_request_error": "llm",
    "post_tool_call": "tool",
    "subagent_stop": "subagent",
    "on_human_input_resolved": "approval",
}

DEFAULT_QUEUE_MAXSIZE = 256
DEFAULT_STALE_TTL_SECONDS = 300.0
DEFAULT_DRAIN_TIMEOUT_SECONDS = 10.0


def _valid_id(value):
    return isinstance(value, str) and bool(_ID_RE.match(value))


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


class HermesObserver:
    """Read-only Hermes hook observer. See the module docstring."""

    def __init__(
        self,
        endpoint,
        service_name="hermes",
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
            "invalid_events": 0,
            "stripped_fields": 0,
            "handle_errors": 0,
            "export_failures": 0,
            "stale_closed": 0,
            "orphans": 0,
            "late_dropped": 0,
            "incomplete_closed": 0,
        }
        self._seen = {}  # (event_type, session, turn, request, attempt) -> time
        self._turn_roots = {}  # (session_id, turn_id) -> (span, context)
        self._open = {}  # (session_id, turn_id, request_id, kind) -> (span, context, started_ns)

        self._provider = TracerProvider(
            resource=Resource(
                {
                    "service.name": service_name,
                    "agent.name": "hermes",
                    "observation.source": "hook",
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
        self._tracer = self._provider.get_tracer("fleet.hermes-observer", "1.0.0")
        self._worker = threading.Thread(
            target=self._run, name="hermes-observer-worker", daemon=True
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

    # -- callback entry point (never raises, never blocks on I/O) --------
    def handle_event(self, event):
        """Validate + enqueue one hook event. Always returns None.

        A callback must never raise into the host agent and never wait on
        network export; all failures are counted, never raised.
        """
        try:
            clean = self._validate(event)
            if clean is None:
                return None
            key = (
                clean["event_type"],
                clean["session_id"],
                clean["turn_id"],
                clean.get("request_id", ""),
                clean.get("attempt", 1),
            )
            with self._lock:
                if key in self._seen:
                    self._counters["duplicates"] += 1
                    return None
                if len(self._seen) > 4096:
                    self._seen.pop(next(iter(self._seen)))
                self._seen[key] = time.time()
            try:
                self._queue.put_nowait(clean)
            except queue.Full:
                self._count("dropped_queue_full")
                return None
            self._count("accepted")
            return None
        except Exception:
            self._count("handle_errors")
            return None

    def _validate(self, event):
        """Return the allowlisted event dict, or None if invalid."""
        if not isinstance(event, dict):
            self._count("invalid_events")
            return None
        event_type = event.get("event_type")
        if event_type not in EVENT_TYPES:
            self._count("invalid_events")
            return None
        session_id = event.get("session_id", "")
        turn_id = event.get("turn_id", "")
        if not _valid_id(session_id) or not _valid_id(turn_id):
            self._count("invalid_events")
            return None
        stripped = 0
        clean = {}
        for key, value in event.items():
            if key in DENIED_FIELDS:
                stripped += 1
                continue
            if key in ALLOWED_FIELDS:
                clean[key] = value
        if stripped:
            self._count("stripped_fields", stripped)
        # request_id is required for operation events, optional for
        # session lifecycle events.
        if event_type not in ("on_session_end", "on_session_finalize", "on_session_reset"):
            request_id = clean.get("request_id", "")
            if not _valid_id(request_id):
                self._count("invalid_events")
                return None
        attempt = clean.get("attempt", 1)
        if not isinstance(attempt, int) or attempt < 1:
            clean["attempt"] = 1
        outcome = clean.get("outcome")
        if outcome is not None and outcome not in OUTCOMES:
            clean["outcome"] = "unknown"
        error_type = clean.get("error_type")
        if error_type is not None and error_type not in ERROR_TYPES:
            clean["error_type"] = "Unknown"
        return clean

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
        # Drain remaining items on stop (bounded: whatever is queued).
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
        """Block until the queue is empty (all enqueued events processed)."""
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
        """Bounded teardown. Called only by the owner at process teardown.

        Never called by a hook callback (on_session_end reconciles and
        flushes but does not shut down — the provider is process-scoped).
        """
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

    # -- event processing (worker thread only) ------------------------------
    def _process(self, event):
        event_type = event["event_type"]
        if event_type in _OPEN_EVENTS:
            self._open_span(event)
        elif event_type in _CLOSE_EVENTS:
            self._close_span(event)
        elif event_type == "on_session_end":
            self._end_turn(event["session_id"], event["turn_id"])
        elif event_type in ("on_session_finalize", "on_session_reset"):
            self._end_session(event["session_id"])
        self._prune_stale()

    def _turn_root(self, session_id, turn_id):
        key = (session_id, turn_id)
        root = self._turn_roots.get(key)
        if root is not None:
            return root
        span = self._tracer.start_span(
            "agent.run",
            context=Context(),
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(
                {
                    "agent.name": "hermes",
                    "agent.role": "observed-agent",
                    "session.id": session_id,
                    "turn.id": turn_id,
                    "observation.source": "hook",
                    "observation.completeness": "complete",
                }
            ),
        )
        ctx = _span_context(span)
        self._turn_roots[key] = (span, ctx)
        return (span, ctx)

    def _open_span(self, event):
        session_id = event["session_id"]
        turn_id = event["turn_id"]
        request_id = event["request_id"]
        span_name, kind = _OPEN_EVENTS[event["event_type"]]
        key = (session_id, turn_id, request_id, kind)
        if key in self._open:
            self._count("duplicates")
            return
        _, root_ctx = self._turn_root(session_id, turn_id)
        parent_ctx = root_ctx
        parent_request_id = event.get("parent_request_id")
        if parent_request_id:
            for (s, t, r, k), (_, ctx, _) in self._open.items():
                if s == session_id and t == turn_id and r == parent_request_id:
                    parent_ctx = ctx
                    break
        attrs = {
            "agent.name": "hermes",
            "session.id": session_id,
            "turn.id": turn_id,
            "request.id": request_id,
            "observation.source": "hook",
            "observation.completeness": "complete",
        }
        if event.get("model"):
            attrs["gen_ai.request.model"] = str(event["model"])[:MAX_ATTRIBUTE_CHARS]
        if event.get("tool_name"):
            attrs["gen_ai.tool.name"] = str(event["tool_name"])[:MAX_ATTRIBUTE_CHARS]
        if kind == "approval":
            attrs["approval.kind"] = "approval"
        if event.get("attempt", 1) > 1:
            attrs["hermes.attempt"] = int(event["attempt"])
        span = self._tracer.start_span(
            span_name,
            context=parent_ctx,
            kind=SpanKind.INTERNAL,
            attributes=sanitize_attributes(attrs),
        )
        self._open[key] = (span, _span_context(span), time.time())

    def _close_span(self, event):
        session_id = event["session_id"]
        turn_id = event["turn_id"]
        request_id = event["request_id"]
        kind = _CLOSE_EVENTS[event["event_type"]]
        key = (session_id, turn_id, request_id, kind)
        entry = self._open.pop(key, None)
        if entry is None:
            # Late completion after the turn already ended, or a close
            # with no matching open: dropped, counted, never invented.
            self._count("late_dropped")
            return
        span, _, _ = entry
        if event["event_type"] == "api_request_error":
            span.set_attribute(
                "error.type", event.get("error_type", "Unknown")
            )
            span.set_status(Status(StatusCode.ERROR, "Agent call failed; details withheld"))
        elif event.get("outcome") in ("observed_failure", "timed_out", "cancelled"):
            span.set_status(Status(StatusCode.ERROR, "Agent call failed; details withheld"))
        span.end()

    def _end_turn(self, session_id, turn_id):
        """Reconcile one turn: close orphaned spans as incomplete, close
        the turn root, flush (bounded). Never shuts the provider down —
        the next turn still exports."""
        for key in [k for k in self._open if k[0] == session_id and k[1] == turn_id]:
            span, _, _ = self._open.pop(key)
            span.set_attribute("observation.completeness", "incomplete")
            self._count("incomplete_closed")
            span.end()
        root = self._turn_roots.pop((session_id, turn_id), None)
        if root is not None:
            root[0].end()
        try:
            self._provider.force_flush(timeout_millis=5000)
        except Exception:
            self._count("export_failures")

    def _end_session(self, session_id):
        """Session cleanup (finalize/reset): close everything belonging
        to the session as incomplete, clear its dedup entries, flush."""
        for key in [k for k in self._open if k[0] == session_id]:
            span, _, _ = self._open.pop(key)
            span.set_attribute("observation.completeness", "incomplete")
            self._count("incomplete_closed")
            span.end()
        for key in [k for k in self._turn_roots if k[0] == session_id]:
            self._turn_roots.pop(key)[0].end()
        with self._lock:
            for key in [k for k in self._seen if k[1] == session_id]:
                del self._seen[key]
        try:
            self._provider.force_flush(timeout_millis=5000)
        except Exception:
            self._count("export_failures")

    def _prune_stale(self):
        now = time.time()
        for key in list(self._open):
            _, _, started = self._open[key]
            if now - started > self._stale_ttl:
                span, _, _ = self._open.pop(key)
                span.set_attribute("observation.completeness", "incomplete")
                self._count("stale_closed")
                self._count("incomplete_closed")
                span.end()


def _span_context(span):
    """The Context carrying a just-started span (explicit parenting)."""
    from opentelemetry.trace import set_span_in_context

    return set_span_in_context(span)
