"""Bounded local intake ledger (SQLite, stdlib only).

The ledger is the receiver side of the M5 contract: it records
imports, finalized spans, normalized events, and runs, enforces
first-write-wins deduplication, and purges per the retention policy.

No raw payloads are stored here — only the mapped metadata, the
content hash of the canonical document, and the receipt.
"""

from __future__ import annotations

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    import_id TEXT PRIMARY KEY,
    content_sha256 TEXT UNIQUE NOT NULL,
    schema_version TEXT NOT NULL,
    quality TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    run_ids_json TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    producer TEXT,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    import_id TEXT NOT NULL,
    name TEXT,
    kind TEXT,
    status TEXT,
    parent_span_id TEXT,
    start_ns INTEGER,
    end_ns INTEGER,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (trace_id, span_id)
);
CREATE TABLE IF NOT EXISTS span_imports (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    import_id TEXT NOT NULL,
    PRIMARY KEY (trace_id, span_id, import_id)
);
CREATE TABLE IF NOT EXISTS events (
    source TEXT NOT NULL,
    producer TEXT NOT NULL,
    event_id TEXT NOT NULL,
    trace_id TEXT,
    span_id TEXT,
    name TEXT,
    at_ns INTEGER,
    import_id TEXT NOT NULL,
    PRIMARY KEY (source, producer, event_id)
);
CREATE TABLE IF NOT EXISTS event_imports (
    source TEXT NOT NULL,
    producer TEXT NOT NULL,
    event_id TEXT NOT NULL,
    import_id TEXT NOT NULL,
    PRIMARY KEY (source, producer, event_id, import_id)
);
CREATE TABLE IF NOT EXISTS runs (
    trace_id TEXT PRIMARY KEY,
    status TEXT,
    started_at INTEGER,
    ended_at INTEGER,
    span_count INTEGER NOT NULL,
    partial INTEGER NOT NULL,
    import_id TEXT NOT NULL
);
"""


def default_ledger_path():
    override = os.environ.get("FLEET_LEDGER_DIR")
    base = override or os.path.expanduser("~/.local/share/fleet-tracing")
    return os.path.join(base, "agentscope-ledger.sqlite3")


def open_ledger(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Lightweight migration for ledgers created before attributes_json.
    columns = {row["name"] for row in conn.execute("SELECT name FROM pragma_table_info('spans')")}
    if "attributes_json" not in columns:
        conn.execute("ALTER TABLE spans ADD COLUMN attributes_json TEXT NOT NULL DEFAULT '{}'")
    conn.commit()
    return conn


def get_import_by_sha(conn, content_sha256):
    row = conn.execute(
        "SELECT * FROM imports WHERE content_sha256 = ?", (content_sha256,)
    ).fetchone()
    return _receipt_from_row(row) if row is not None else None


def get_receipt(conn, import_id):
    row = conn.execute(
        "SELECT * FROM imports WHERE import_id = ?", (import_id,)
    ).fetchone()
    return _receipt_from_row(row) if row is not None else None


def _receipt_from_row(row):
    return {
        "import_id": row["import_id"],
        "quality": row["quality"],
        "warnings": json.loads(row["warnings_json"]),
        "stats": json.loads(row["stats_json"]),
        "run_ids": json.loads(row["run_ids_json"]),
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "provenance": {
            "producer": row["producer"],
            "feed": "fleet-tracing",
            "schema_version": row["schema_version"],
            "content_sha256": row["content_sha256"],
            "received_at": row["received_at"],
        },
    }


def existing_spans(conn, trace_id):
    """{span_id: {name, kind, status, parent_span_id, start_ns, end_ns, attributes}}."""
    rows = conn.execute(
        "SELECT span_id, name, kind, status, parent_span_id, start_ns, end_ns,"
        " attributes_json FROM spans WHERE trace_id = ?",
        (trace_id,),
    ).fetchall()
    out = {}
    for row in rows:
        try:
            attributes = json.loads(row["attributes_json"] or "{}")
        except ValueError:
            attributes = {}
        out[row["span_id"]] = {
            "name": row["name"],
            "kind": row["kind"],
            "status": row["status"],
            "parent_span_id": row["parent_span_id"],
            "start_ns": row["start_ns"],
            "end_ns": row["end_ns"],
            "attributes": attributes if isinstance(attributes, dict) else {},
        }
    return out


def existing_event_ids(conn, source, producer):
    rows = conn.execute(
        "SELECT event_id FROM events WHERE source = ? AND producer = ?",
        (source, producer),
    ).fetchall()
    return {row["event_id"] for row in rows}


def record_import(conn, import_row, span_rows, event_rows, run_ids):
    """Write one import and its rows in a single transaction.

    span_rows: list of dicts with trace_id, span_id, name, kind,
    status, parent_span_id, start_ns, end_ns. First write wins for a
    (trace_id, span_id) pair — callers pre-check conflicts and pass
    only genuinely new spans. event_rows likewise.
    """
    with conn:
        conn.execute(
            "INSERT INTO imports"
            " (import_id, content_sha256, schema_version, quality, stats_json,"
            "  warnings_json, run_ids_json, tokens_in, tokens_out, producer, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                import_row["import_id"],
                import_row["content_sha256"],
                import_row["schema_version"],
                import_row["quality"],
                json.dumps(import_row["stats"]),
                json.dumps(import_row["warnings"]),
                json.dumps(run_ids),
                import_row["tokens_in"],
                import_row["tokens_out"],
                import_row["producer"],
                import_row["received_at"],
            ),
        )
        for span in span_rows:
            conn.execute(
                "INSERT OR IGNORE INTO spans"
                " (trace_id, span_id, import_id, name, kind, status,"
                "  parent_span_id, start_ns, end_ns, attributes_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    span["trace_id"],
                    span["span_id"],
                    import_row["import_id"],
                    span.get("name"),
                    span.get("kind"),
                    span.get("status"),
                    span.get("parent_span_id"),
                    span.get("start_ns"),
                    span.get("end_ns"),
                    json.dumps(span.get("attributes") or {}, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO span_imports (trace_id, span_id, import_id)"
                " VALUES (?, ?, ?)",
                (span["trace_id"], span["span_id"], import_row["import_id"]),
            )
        for event in event_rows:
            conn.execute(
                "INSERT OR IGNORE INTO events"
                " (source, producer, event_id, trace_id, span_id, name, at_ns, import_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["source"],
                    event["producer"],
                    event["event_id"],
                    event.get("trace_id"),
                    event.get("span_id"),
                    event.get("name"),
                    event.get("at_ns"),
                    import_row["import_id"],
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO event_imports"
                " (source, producer, event_id, import_id) VALUES (?, ?, ?, ?)",
                (event["source"], event["producer"], event["event_id"], import_row["import_id"]),
            )
        traces = {span["trace_id"] for span in span_rows}
        traces.update(run_ids.keys())
        for trace_id in traces:
            _refresh_run(conn, trace_id, import_row["import_id"])


def _refresh_run(conn, trace_id, import_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(start_ns) AS started, MAX(end_ns) AS ended,"
        " MAX(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS has_error,"
        " MAX(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS has_ok"
        " FROM spans WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()
    if row["n"] == 0:
        conn.execute("DELETE FROM runs WHERE trace_id = ?", (trace_id,))
        return
    if row["has_error"]:
        status = "error"
    elif row["has_ok"]:
        status = "ok"
    else:
        status = "unknown"
    conn.execute(
        "INSERT INTO runs"
        " (trace_id, status, started_at, ended_at, span_count, partial, import_id)"
        " VALUES (?, ?, ?, ?, ?, 0, ?)"
        " ON CONFLICT(trace_id) DO UPDATE SET"
        " status = excluded.status, started_at = excluded.started_at,"
        " ended_at = excluded.ended_at, span_count = excluded.span_count,"
        " partial = 0",
        (trace_id, status, row["started"], row["ended"], row["n"], import_id),
    )


def set_run_partial(conn, trace_id, import_id):
    """Record a run with no spans (partial coverage, direct-doc path)."""
    with conn:
        conn.execute(
            "INSERT INTO runs"
            " (trace_id, status, started_at, ended_at, span_count, partial, import_id)"
            " VALUES (?, 'unknown', NULL, NULL, 0, 1, ?)"
            " ON CONFLICT(trace_id) DO NOTHING",
            (trace_id, import_id),
        )


def purge(conn, max_imports, max_age_days, now_iso):
    """Enforce retention: drop imports older than max_age_days and
    anything beyond the newest max_imports. Returns a dict of counts."""
    rows = conn.execute(
        "SELECT import_id, received_at FROM imports ORDER BY received_at ASC"
    ).fetchall()
    cutoff = _age_cutoff(now_iso, max_age_days)
    to_purge = []
    kept = 0
    for row in reversed(rows):
        if kept < max_imports and row["received_at"] >= cutoff:
            kept += 1
        else:
            to_purge.append(row["import_id"])
    if not to_purge:
        return {"purged_imports": 0, "purged_spans": 0, "purged_events": 0}
    placeholders = ",".join("?" for _ in to_purge)
    with conn:
        conn.execute(
            "DELETE FROM span_imports WHERE import_id IN (%s)" % placeholders, to_purge
        )
        conn.execute(
            "DELETE FROM event_imports WHERE import_id IN (%s)" % placeholders, to_purge
        )
        conn.execute(
            "DELETE FROM imports WHERE import_id IN (%s)" % placeholders, to_purge
        )
        spans_before = conn.execute("SELECT COUNT(*) AS n FROM spans").fetchone()["n"]
        events_before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        conn.execute(
            "DELETE FROM spans WHERE NOT EXISTS ("
            " SELECT 1 FROM span_imports"
            " WHERE span_imports.trace_id = spans.trace_id"
            " AND span_imports.span_id = spans.span_id)"
        )
        conn.execute(
            "DELETE FROM events WHERE NOT EXISTS ("
            " SELECT 1 FROM event_imports"
            " WHERE event_imports.source = events.source"
            " AND event_imports.producer = events.producer"
            " AND event_imports.event_id = events.event_id)"
        )
        conn.execute(
            "DELETE FROM runs WHERE trace_id NOT IN (SELECT trace_id FROM spans)"
            " AND partial = 0"
        )
        # Partial runs have no spans; they go when their import goes.
        conn.execute(
            "DELETE FROM runs WHERE partial = 1 AND import_id IN (%s)" % placeholders,
            to_purge,
        )
        spans_after = conn.execute("SELECT COUNT(*) AS n FROM spans").fetchone()["n"]
        events_after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    return {
        "purged_imports": len(to_purge),
        "purged_spans": spans_before - spans_after,
        "purged_events": events_before - events_after,
    }


def _age_cutoff(now_iso, max_age_days):
    from datetime import datetime, timedelta

    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    cutoff = now - timedelta(days=max_age_days)
    return cutoff.isoformat().replace("+00:00", "Z")


def counts(conn):
    out = {}
    for table in ("imports", "spans", "events", "runs"):
        out[table] = conn.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
    return out
