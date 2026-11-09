"""The M5 import operation: payload -> canonical document -> ledger -> receipt.

Entry points (all return a receiver receipt; see CONTRACT.md §6):
  import_spans             flattened fleet spans (receiver shape)
  import_otlp_payload      OTLP/HTTP JSON payload (resourceSpans)
  import_canonical_document  an AgentScope canonical 0.1 document
  import_file              a file: canonical JSON, OTLP JSON, span JSON
                           array, or JSONL of flattened spans

Out-of-contract inputs raise ImportRefused (OTLP protobuf, oversize
uploads). Everything else gets a receipt — including unsupported and
invalid input, which the ledger records so the receipt resolves.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from . import ledger as ledger_mod
from .contract import (
    GRAPH_DERIVATION,
    JSON_CONTENT_TYPE,
    MAX_AGE_DAYS,
    MAX_BYTES_PER_IMPORT,
    MAX_IMPORTS,
    MAX_SPANS_PER_IMPORT,
    SCHEMA_VERSION,
    STATS_KEYS,
)
from .mapper import (
    canonical_bytes,
    import_id_for,
    map_spans,
    run_id_for,
)


class ImportRefused(Exception):
    """The import is out of contract (unsupported transport, oversize)."""


@dataclass
class ImportSettings:
    max_spans_per_import: int = MAX_SPANS_PER_IMPORT
    max_bytes_per_import: int = MAX_BYTES_PER_IMPORT
    max_imports: int = MAX_IMPORTS
    max_age_days: int = MAX_AGE_DAYS


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_to_ns(value):
    """ISO-8601 string -> Unix nanoseconds, with integer arithmetic.

    Floats must not be involved: a float round-trip loses nanosecond
    precision, which would make an identical re-delivery look like a
    conflict. ISO strings from the mapper carry microsecond precision,
    so the recovered value is exact to the microsecond.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


# ---------------------------------------------------------------------------
# OTLP payload flattening (same attribute decoding as receiver.flatten_payload,
# plus span events, which the receiver drops).
# ---------------------------------------------------------------------------

def _attrs_to_dict(attrs):
    out = {}
    for attr in attrs or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        value = attr.get("value") or {}
        if not isinstance(value, dict):
            out[key] = value
        elif "stringValue" in value:
            out[key] = value["stringValue"]
        elif "intValue" in value:
            try:
                out[key] = int(value["intValue"])
            except (TypeError, ValueError):
                out[key] = value["intValue"]
        elif "boolValue" in value:
            out[key] = bool(value["boolValue"])
        elif "doubleValue" in value:
            out[key] = float(value["doubleValue"])
        elif "arrayValue" in value:
            out[key] = str(value["arrayValue"])
        else:
            out[key] = None
    return out


def flatten_otlp_payload(payload):
    """OTLP/HTTP JSON resourceSpans -> flattened spans (with events)."""
    spans = []
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        resource_attrs = _attrs_to_dict((resource_span.get("resource") or {}).get("attributes"))
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            scope = scope_span.get("scope") or {}
            for span in scope_span.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                status = span.get("status") or {}
                spans.append(
                    {
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "parentSpanId": span.get("parentSpanId"),
                        "name": span.get("name"),
                        "kind": span.get("kind"),
                        "startTimeUnixNano": span.get("startTimeUnixNano"),
                        "endTimeUnixNano": span.get("endTimeUnixNano"),
                        "attributes": _attrs_to_dict(span.get("attributes")),
                        "events": span.get("events") or [],
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


# ---------------------------------------------------------------------------
# Receipt / quality
# ---------------------------------------------------------------------------

def _quality(runs, dropped, unreadable, any_partial, warnings):
    if runs == 0:
        return "invalid"
    if dropped or unreadable or any_partial:
        return "partial"
    if warnings:
        return "warnings"
    return "complete"


def _empty_stats():
    return {key: 0 for key in STATS_KEYS}


def _receipt(import_id, quality, warnings, stats, run_ids, tokens_in, tokens_out,
             producer, content_sha256, received_at, already_imported=False,
             graph_derivation=None):
    clean_warnings = []
    for warning in warnings:
        entry = {"code": warning["code"], "message": warning["message"]}
        if warning.get("trace_id"):
            entry["source_ref"] = "trace:%s" % warning["trace_id"]
        clean_warnings.append(entry)
    # D-F1 tree correction (ADR-005): the graph derivation is recorded in
    # provenance when a document was actually mapped (it is not a warning
    # and does not change receipt quality — see CONTRACT.md §5). Receipts
    # for unsupported/invalid outcomes carry no derivation claim.
    provenance = {
        "producer": producer,
        "feed": "fleet-tracing",
        "schema_version": SCHEMA_VERSION,
        "content_sha256": content_sha256,
        "received_at": received_at,
    }
    if graph_derivation is not None:
        provenance["graph_derivation"] = graph_derivation
    return {
        "import_id": import_id,
        "quality": quality,
        "warnings": clean_warnings,
        "stats": {key: stats.get(key, 0) for key in STATS_KEYS},
        "run_ids": list(run_ids.values()) if isinstance(run_ids, dict) else list(run_ids),
        "already_imported": already_imported,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "provenance": provenance,
    }


def _record_receipt(conn, import_row, span_rows, event_rows, run_ids, settings,
                    graph_derivation=None):
    import_row = dict(import_row)
    import_row["graph_derivation"] = graph_derivation
    ledger_mod.record_import(conn, import_row, span_rows, event_rows, run_ids)
    ledger_mod.purge(conn, settings.max_imports, settings.max_age_days, import_row["received_at"])
    return _receipt(
        import_row["import_id"],
        import_row["quality"],
        import_row["warnings"],
        import_row["stats"],
        run_ids,
        import_row["tokens_in"],
        import_row["tokens_out"],
        import_row["producer"],
        import_row["content_sha256"],
        import_row["received_at"],
        graph_derivation=graph_derivation,
    )


def _outcome_sha(documents, unreadable, dropped, notes):
    """Idempotency key for one upload.

    Covers the mapped documents AND the input accounting (unreadable
    items, dropped runs, mapping notes) — not just the documents.
    Two uploads that map to the same documents but differ in what was
    skipped or dropped are different uploads and get distinct
    receipts; the receipt must describe THIS upload, not an earlier
    one that happened to map the same way. (Found by the M5
    double-check: a duplicate-content JSONL upload with one bad line
    returned the original receipt, hiding the skipped line.)
    Write-time notes (duplicate_span / span_conflict) are per-upload
    consequences of ledger state, not input, so they are not part of
    the key — they are reported in the receipt of the upload that
    produced them.
    """
    digest = hashlib.sha256()
    for trace_id, document in sorted(documents, key=lambda item: item[0]):
        digest.update(canonical_bytes(document))
    accounting = {
        "unreadable": unreadable,
        "dropped": sorted(
            (
                {"trace_id": d.get("trace_id"), "code": d.get("code")}
                for d in dropped
            ),
            key=lambda d: (d["trace_id"] or "", d["code"] or ""),
        ),
        "notes": sorted(
            (
                {
                    "code": n.get("code"),
                    "message": n.get("message"),
                    "trace_id": n.get("trace_id"),
                }
                for n in notes
            ),
            key=lambda n: (n["code"] or "", n["message"] or "", n["trace_id"] or ""),
        ),
    }
    digest.update(json.dumps(accounting, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Shared write path for mapped documents
# ---------------------------------------------------------------------------

def _write_mapped(conn, mapped, producer, source, settings, received_at,
                  content_sha256, unreadable_extra=0, unmapped=0,
                  graph_derivation=GRAPH_DERIVATION):
    """Write mapped documents to the ledger and build the receipt.

    mapped is a map_spans() result. First write wins per (trace_id,
    span_id): identical re-delivery is noted as a duplicate, a changed
    re-delivery as a conflict, and neither overwrites the stored row.
    graph_derivation records the derivation claimed for the documents in
    this import (D-F1); import_canonical_document passes the incoming
    document's own run-metadata label instead of the mapper constant.
    """
    documents = mapped["documents"]
    notes = list(mapped["notes"])
    dropped = mapped["dropped"]
    unreadable = mapped["unreadable"] + unreadable_extra
    import_id = import_id_for(content_sha256)
    run_ids = {trace_id: run_id_for(content_sha256, trace_id) for trace_id, _ in documents}

    # Conflict / duplicate pre-check against stored spans and events.
    span_rows = []
    event_rows = []
    spans_in_import = 0
    events_in_import = 0
    for trace_id, document in documents:
        existing = ledger_mod.existing_spans(conn, trace_id)
        for span in document["spans"]:
            spans_in_import += 1
            span_id = span["native_id"]
            row = {
                "trace_id": trace_id,
                "span_id": span_id,
                "name": span["name"],
                "kind": span["kind"],
                "status": span["status"],
                "parent_span_id": span.get("parent_native_id"),
                "start_ns": _iso_to_ns(span.get("started_at")),
                "end_ns": _iso_to_ns(span.get("ended_at")),
                "attributes": span.get("attributes") or {},
            }
            prior = existing.get(span_id)
            if prior is not None:
                # Compare only the stored fields (the row carries
                # trace_id/span_id too, which the stored record lacks).
                same = all(
                    prior[key] == row[key]
                    for key in ("name", "kind", "status", "parent_span_id",
                                "start_ns", "end_ns", "attributes")
                )
                if same:
                    notes.append(
                        {
                            "code": "duplicate_span",
                            "message": "span %s already stored; first write wins" % span_id,
                            "trace_id": trace_id,
                        }
                    )
                else:
                    notes.append(
                        {
                            "code": "span_conflict",
                            "message": "span %s re-delivered with different fields; kept the first write" % span_id,
                            "trace_id": trace_id,
                        }
                    )
                continue
            span_rows.append(row)
        existing_events = ledger_mod.existing_event_ids(conn, source, producer)
        for event in document.get("events") or []:
            events_in_import += 1
            event_id = event["native_id"]
            if event_id in existing_events:
                notes.append(
                    {
                        "code": "duplicate_event",
                        "message": "event %s already stored; first write wins" % event_id,
                        "trace_id": trace_id,
                    }
                )
                continue
            event_rows.append(
                {
                    "source": source,
                    "producer": producer,
                    "event_id": event_id,
                    "trace_id": trace_id,
                    "span_id": event.get("span_native_id"),
                    "name": event.get("type"),
                    "at_ns": _iso_to_ns(event.get("at")),
                }
            )
    any_partial = False
    for trace_id, document in documents:
        if not document["spans"]:
            any_partial = True
    stats = _empty_stats()
    stats["runs"] = len(documents)
    stats["spans"] = spans_in_import
    stats["events"] = events_in_import
    stats["warnings"] = len(notes)
    stats["dropped"] = len(dropped)
    # Spans that were read but could not be imported because their run
    # was dropped (passed in by the caller, which knows the input).
    stats["unmapped"] = unmapped
    stats["detection_skipped_lines"] = unreadable
    quality = _quality(len(documents), len(dropped), unreadable, any_partial, notes)
    import_row = {
        "import_id": import_id,
        "content_sha256": content_sha256,
        "schema_version": SCHEMA_VERSION,
        "quality": quality,
        "stats": stats,
        "warnings": notes,
        "tokens_in": mapped["tokens_in"],
        "tokens_out": mapped["tokens_out"],
        "producer": producer,
        "received_at": received_at,
        "graph_derivation": graph_derivation,
    }
    receipt = _record_receipt(conn, import_row, span_rows, event_rows, run_ids, settings,
                              graph_derivation=graph_derivation)
    return receipt


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def import_spans(conn, spans, producer, settings=None, source="fleet-tracing",
                 received_at=None, unreadable_extra=0):
    """Import a batch of flattened fleet spans. Returns the receipt."""
    settings = settings or ImportSettings()
    received_at = received_at or now_iso()
    if not isinstance(spans, list):
        raise ImportRefused("spans must be a list")
    if len(spans) > settings.max_spans_per_import:
        raise ImportRefused(
            "batch of %d spans exceeds max_spans_per_import=%d"
            % (len(spans), settings.max_spans_per_import)
        )
    mapped = map_spans(spans, producer)
    unreadable = mapped["unreadable"] + unreadable_extra
    content_sha256 = _outcome_sha(
        mapped["documents"], unreadable, mapped["dropped"], mapped["notes"]
    )
    # Spans in dropped runs were read but could not be imported.
    by_trace = {}
    for span in spans:
        if isinstance(span, dict) and isinstance(span.get("traceId"), str):
            by_trace.setdefault(span["traceId"], []).append(span)
    unmapped = sum(len(by_trace.get(drop["trace_id"], [])) for drop in mapped["dropped"])
    existing = ledger_mod.get_import_by_sha(conn, content_sha256)
    if existing is not None:
        existing["already_imported"] = True
        return existing
    return _write_mapped(
        conn, mapped, producer, source, settings, received_at, content_sha256,
        unreadable_extra=unreadable_extra, unmapped=unmapped,
    )


def import_otlp_payload(conn, payload, producer, content_type=JSON_CONTENT_TYPE,
                        settings=None, source="fleet-tracing", received_at=None):
    """Import an OTLP/HTTP JSON payload (application/json only)."""
    settings = settings or ImportSettings()
    received_at = received_at or now_iso()
    if content_type is not None and content_type != JSON_CONTENT_TYPE:
        raise ImportRefused(
            "OTLP content type %s is not supported; send OTLP/HTTP JSON"
            " (%s)" % (content_type, JSON_CONTENT_TYPE)
        )
    if not isinstance(payload, dict) or "resourceSpans" not in payload:
        return _unsupported_receipt(conn, producer, settings, received_at,
                                    "payload has no resourceSpans; no OTLP adapter matches")
    spans = flatten_otlp_payload(payload)
    if not spans:
        return _invalid_receipt(conn, producer, settings, received_at,
                                "payload has no spans; no run could be formed")
    return import_spans(conn, spans, producer, settings=settings, source=source,
                        received_at=received_at)


def import_canonical_document(conn, document, producer, settings=None,
                              source="fleet-tracing", received_at=None):
    """Import an AgentScope canonical 0.1 document directly."""
    settings = settings or ImportSettings()
    received_at = received_at or now_iso()
    if not isinstance(document, dict):
        return _unsupported_receipt(conn, producer, settings, received_at,
                                    "document is not a JSON object; no adapter matches")
    # Deep-copy: the normalizer moves unknown fields into attributes /
    # metadata.extra and must never mutate the caller's document.
    document = json.loads(json.dumps(document))
    if document.get("schema_version") != SCHEMA_VERSION:
        return _unsupported_receipt(
            conn, producer, settings, received_at,
            "schema_version %r is not supported; expected %r"
            % (document.get("schema_version"), SCHEMA_VERSION),
        )
    notes = []
    for key in document:
        if key not in ("schema_version", "source", "run", "graph", "spans",
                       "events", "node_executions", "checkpoints", "payloads",
                       "findings"):
            notes.append({"code": "unknown_key", "message": "unknown top-level key %r ignored" % key})
    run = document.get("run")
    spans = document.get("spans")
    if not isinstance(run, dict) or not isinstance(spans, list):
        return _invalid_receipt(conn, producer, settings, received_at,
                                "document has no run or no spans list; no run could be formed")
    trace_id = run.get("native_id")
    if not isinstance(trace_id, str) or not trace_id:
        return _invalid_receipt(conn, producer, settings, received_at,
                                "run has no native_id; no run could be formed")
    known_run_keys = {"native_id", "status", "started_at", "ended_at", "metadata"}
    unknown_run = {key: run[key] for key in run if key not in known_run_keys}
    if unknown_run:
        metadata = run.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            run["metadata"] = metadata
        extra = metadata.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            metadata["extra"] = extra
        extra.update(unknown_run)
        for key in unknown_run:
            notes.append(
                {"code": "unknown_run_field",
                 "message": "unknown run field %r kept in metadata.extra" % key}
            )
    # Normalize the document's spans into the mapped-document shape.
    known_span_keys = {
        "native_id", "parent_native_id", "seq", "kind", "name", "node_name",
        "status", "started_at", "ended_at", "duration_ms", "error", "llm",
        "tool", "attributes",
    }
    seen = set()
    canonical_spans = []
    tokens_in = 0
    tokens_out = 0
    for index, span in enumerate(spans):
        if not isinstance(span, dict) or not isinstance(span.get("native_id"), str):
            notes.append({"code": "unreadable_span", "message": "span at index %d has no native_id; skipped" % index})
            continue
        if span["native_id"] in seen:
            return _invalid_receipt(
                conn, producer, settings, received_at,
                "duplicate native_id %s in one trace; run dropped" % span["native_id"],
            )
        seen.add(span["native_id"])
        span = dict(span)
        attributes = dict(span.get("attributes") or {})
        for key in list(span):
            if key not in known_span_keys:
                attributes[key] = span.pop(key)
                notes.append({"code": "unknown_span_field", "message": "unknown span field %r moved under attributes" % key})
        span["attributes"] = attributes
        span.setdefault("seq", index)
        llm = span.get("llm") or {}
        if isinstance(llm.get("input_tokens"), int):
            tokens_in += llm["input_tokens"]
        if isinstance(llm.get("output_tokens"), int):
            tokens_out += llm["output_tokens"]
        canonical_spans.append(span)
    document = dict(document)
    document["spans"] = canonical_spans
    content_sha256 = _outcome_sha([(trace_id, document)], 0, [], notes)
    existing = ledger_mod.get_import_by_sha(conn, content_sha256)
    if existing is not None:
        existing["already_imported"] = True
        return existing
    mapped = {
        "documents": [(trace_id, document)],
        "notes": notes,
        "dropped": [],
        "unreadable": 0,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }
    # The receipt records the derivation the incoming document claims
    # for itself (D-F1): a fleet-mapped document carries the tree label
    # in run metadata; a foreign document carries whatever it claims,
    # or nothing at all — the receipt never invents a derivation.
    metadata = run.get("metadata") if isinstance(run, dict) else None
    claimed = metadata.get("graph") if isinstance(metadata, dict) else None
    graph_derivation = claimed if isinstance(claimed, str) else None
    receipt = _write_mapped(conn, mapped, producer, source, settings, received_at,
                            content_sha256, graph_derivation=graph_derivation)
    if not canonical_spans:
        ledger_mod.set_run_partial(conn, trace_id, receipt["import_id"])
    return receipt


def import_bytes(conn, raw, producer, settings=None, source="fleet-tracing",
                received_at=None):
    """Import one immutable byte snapshot using the normal M5 adapters."""
    settings = settings or ImportSettings()
    received_at = received_at or now_iso()
    if not isinstance(raw, bytes):
        raise ImportRefused("input snapshot must be bytes")
    if len(raw) > settings.max_bytes_per_import:
        raise ImportRefused(
            "input of %d bytes exceeds max_bytes_per_import=%d"
            % (len(raw), settings.max_bytes_per_import)
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if parsed is not None:
        if isinstance(parsed, dict) and "resourceSpans" in parsed:
            return import_otlp_payload(conn, parsed, producer, settings=settings,
                                       source=source, received_at=received_at)
        if isinstance(parsed, dict) and "schema_version" in parsed and "run" in parsed:
            return import_canonical_document(conn, parsed, producer, settings=settings,
                                             source=source, received_at=received_at)
        if isinstance(parsed, list):
            return import_spans(conn, parsed, producer, settings=settings, source=source,
                                received_at=received_at)
        return _unsupported_receipt(conn, producer, settings, received_at,
                                    "JSON document matches no adapter (no resourceSpans, no canonical run)")
    # JSONL: one flattened span per line.
    spans = []
    unreadable_lines = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            unreadable_lines += 1
            continue
        spans.append(item)
    if not spans and unreadable_lines:
        return _invalid_receipt(conn, producer, settings, received_at,
                                "no readable spans in file; %d line(s) skipped" % unreadable_lines)
    return import_spans(conn, spans, producer, settings=settings, source=source,
                        received_at=received_at, unreadable_extra=unreadable_lines)


def import_file(conn, path, producer, settings=None, source="fleet-tracing",
                received_at=None):
    """Import a file: canonical JSON, OTLP JSON, span JSON array, or JSONL.

    Read at most one byte past the configured limit. Callers that need to
    bind validation and import to the same snapshot should use import_bytes.
    """
    settings = settings or ImportSettings()
    with open(path, "rb") as handle:
        raw = handle.read(settings.max_bytes_per_import + 1)
    return import_bytes(conn, raw, producer, settings=settings, source=source,
                        received_at=received_at)


# ---------------------------------------------------------------------------
# Recorded unsupported / invalid receipts (no run imported, but the
# receipt resolves — mirroring the real importer's import_id rule).
# ---------------------------------------------------------------------------

def _recorded_outcome(conn, producer, settings, received_at, quality, warnings,
                      content_sha256):
    existing = ledger_mod.get_import_by_sha(conn, content_sha256)
    if existing is not None:
        existing["already_imported"] = True
        return existing
    import_id = import_id_for(content_sha256)
    stats = _empty_stats()
    stats["warnings"] = len(warnings)
    import_row = {
        "import_id": import_id,
        "content_sha256": content_sha256,
        "schema_version": SCHEMA_VERSION,
        "quality": quality,
        "stats": stats,
        "warnings": warnings,
        "tokens_in": 0,
        "tokens_out": 0,
        "producer": producer,
        "received_at": received_at,
    }
    return _record_receipt(conn, import_row, [], [], {}, settings)


def _unsupported_receipt(conn, producer, settings, received_at, message):
    content_sha256 = hashlib.sha256(
        ("unsupported|%s" % message).encode("utf-8")
    ).hexdigest()
    return _recorded_outcome(
        conn, producer, settings, received_at, "unsupported",
        [{"code": "unsupported", "message": message}], content_sha256,
    )


def _invalid_receipt(conn, producer, settings, received_at, message):
    content_sha256 = hashlib.sha256(
        ("invalid|%s" % message).encode("utf-8")
    ).hexdigest()
    return _recorded_outcome(
        conn, producer, settings, received_at, "invalid",
        [{"code": "invalid", "message": message}], content_sha256,
    )
