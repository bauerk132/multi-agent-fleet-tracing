"""The CLI assertion (CONTRACT.md §7): one synthetic fleet turn run
through every contract edge case, with a receipt at every step.

`run_verify` executes the checks against a ledger connection and
returns a list of {"name", "passed", "detail"}. The same checks are
mirrored as unit tests in observability/tests/test_agentscope_importer.py;
this module is the runnable proof, not the only proof.
"""

from __future__ import annotations

import os

from . import ledger as ledger_mod
from .contract import STATS_KEYS
from .importer import (
    ImportRefused,
    import_canonical_document,
    import_otlp_payload,
    import_spans,
    now_iso,
)
from .mapper import map_spans


def synthetic_turn(trace_id, include_tool=True, tokens=(120, 340), with_canaries=False):
    """One synthetic Copilot turn: agent.run -> llm.call -> tool.exec.

    By default the turn is pre-sanitized, as a real observer emits it
    (no content/identity attributes). With with_canaries=True the turn
    carries stripped content attributes, to prove the mapper drops
    them again (defense in depth) and never stores them.
    """
    base = 1_700_000_000_000_000_000
    llm_attrs = {
        "gen_ai.request.model": "gpt-5",
        "gen_ai.usage.input_tokens": tokens[0],
        "gen_ai.usage.output_tokens": tokens[1],
    }
    if with_canaries:
        # Content that must NEVER reach the ledger (stripped).
        llm_attrs["gen_ai.input.messages"] = "CANARY-INPUT-MESSAGES-DO-NOT-STORE"
    spans = [
        {
            "traceId": trace_id,
            "spanId": trace_id + "-root",
            "parentSpanId": None,
            "name": "agent.run",
            "startTimeUnixNano": base,
            "endTimeUnixNano": base + 9_000_000_000,
            "attributes": {"agent.name": "copilot"},
            "status": {"code": 1},
        },
        {
            "traceId": trace_id,
            "spanId": trace_id + "-llm",
            "parentSpanId": trace_id + "-root",
            "name": "llm.call",
            "startTimeUnixNano": base + 1_000_000_000,
            "endTimeUnixNano": base + 4_000_000_000,
            "attributes": llm_attrs,
            "status": {"code": 1},
        },
    ]
    if include_tool:
        tool_attrs = {
            "gen_ai.tool.name": "read_file",
            "gen_ai.tool.call.id": "call-1",
        }
        if with_canaries:
            tool_attrs["gen_ai.tool.call.arguments"] = "CANARY-TOOL-ARGUMENTS-DO-NOT-STORE"
        spans.append(
            {
                "traceId": trace_id,
                "spanId": trace_id + "-tool",
                "parentSpanId": trace_id + "-llm",
                "name": "tool.exec",
                "startTimeUnixNano": base + 2_000_000_000,
                "endTimeUnixNano": base + 3_000_000_000,
                "attributes": tool_attrs,
                "status": {"code": 1},
            }
        )
    return spans


def _check(name, passed, detail):
    return {"name": name, "passed": bool(passed), "detail": detail}


def run_verify(conn, ledger_path, producer="fleet-tracing-verify"):
    """Run every contract check. Returns (results, conn).

    The restart-recovery check closes and reopens the ledger; the
    returned connection is the live one.
    """
    results = []
    trace = "verify-trace-0001"

    # 1. Happy path: complete, real token totals, receipt.
    spans = synthetic_turn(trace)
    receipt = import_spans(conn, spans, producer)
    results.append(
        _check(
            "happy_path_complete",
            receipt["quality"] == "complete"
            and receipt["stats"]["runs"] == 1
            and receipt["stats"]["spans"] == 3
            and receipt["tokens_in"] == 120
            and receipt["tokens_out"] == 340
            and len(receipt["run_ids"]) == 1
            and receipt["import_id"].startswith("imp_"),
            "quality=%s stats=%s tokens=%s/%s import_id=%s"
            % (
                receipt["quality"],
                {k: receipt["stats"][k] for k in ("runs", "spans", "events")},
                receipt["tokens_in"],
                receipt["tokens_out"],
                receipt["import_id"],
            ),
        )
    )
    first_import_id = receipt["import_id"]

    # 2. Duplicate delivery: same receipt, already_imported, no new rows.
    before = ledger_mod.counts(conn)
    receipt2 = import_spans(conn, spans, producer)
    after = ledger_mod.counts(conn)
    results.append(
        _check(
            "duplicate_delivery",
            receipt2["already_imported"] is True
            and receipt2["import_id"] == first_import_id
            and before == after,
            "already_imported=%s same_id=%s counts_unchanged=%s"
            % (receipt2["already_imported"], receipt2["import_id"] == first_import_id, before == after),
        )
    )

    # 2b. Content stripping: a turn carrying stripped content
    #     attributes imports as warnings (a correction, per the real
    #     importer's ladder), the sanitized_dropped note is visible,
    #     and the canary values never reach the ledger.
    canary_trace = "verify-trace-canary"
    canary_spans = synthetic_turn(canary_trace, with_canaries=True)
    receipt_canary = import_spans(conn, canary_spans, producer)
    with open(ledger_path, "rb") as handle:
        blob_canary = handle.read()
    results.append(
        _check(
            "content_stripped",
            receipt_canary["quality"] == "warnings"
            and any(w["code"] == "sanitized_dropped" for w in receipt_canary["warnings"])
            and b"CANARY-INPUT-MESSAGES-DO-NOT-STORE" not in blob_canary
            and b"CANARY-TOOL-ARGUMENTS-DO-NOT-STORE" not in blob_canary,
            "quality=%s sanitized_note=%s canary_absent=%s"
            % (
                receipt_canary["quality"],
                any(w["code"] == "sanitized_dropped" for w in receipt_canary["warnings"]),
                b"CANARY-INPUT-MESSAGES-DO-NOT-STORE" not in blob_canary
                and b"CANARY-TOOL-ARGUMENTS-DO-NOT-STORE" not in blob_canary,
            ),
        )
    )

    # 3. Out-of-order arrival: a new trace, spans reversed, still maps
    #    by start time (seq follows time, not arrival order).
    trace2 = "verify-trace-0002"
    spans2 = synthetic_turn(trace2)
    receipt3 = import_spans(conn, list(reversed(spans2)), producer)
    stored = ledger_mod.existing_spans(conn, trace2)
    seq_ok = True
    mapped = map_spans(spans2, producer)
    doc = dict(mapped["documents"][0][1])
    seqs = {s["native_id"]: s["seq"] for s in doc["spans"]}
    seq_ok = (
        seqs[trace2 + "-root"] == 0
        and seqs[trace2 + "-llm"] == 1
        and seqs[trace2 + "-tool"] == 2
    )
    results.append(
        _check(
            "out_of_order_arrival",
            receipt3["quality"] == "complete" and len(stored) == 3 and seq_ok,
            "quality=%s stored_spans=%d seq_by_start_time=%s" % (receipt3["quality"], len(stored), seq_ok),
        )
    )

    # 4. Late span: re-deliver the trace with one extra span; the new
    #    span is stored, the old ones are noted as duplicates, and the
    #    run grows. Quality is warnings (duplicates are visible).
    late = {
        "traceId": trace,
        "spanId": trace + "-late-tool",
        "parentSpanId": trace + "-root",
        "name": "tool.exec",
        "startTimeUnixNano": 1_700_000_005_000_000_000,
        "endTimeUnixNano": 1_700_000_006_000_000_000,
        "attributes": {"gen_ai.tool.name": "write_file", "gen_ai.tool.call.id": "call-2"},
        "status": {"code": 1},
    }
    receipt4 = import_spans(conn, spans + [late], producer)
    stored_after = ledger_mod.existing_spans(conn, trace)
    codes = {w["code"] for w in receipt4["warnings"]}
    results.append(
        _check(
            "late_span",
            receipt4["quality"] == "warnings"
            and len(stored_after) == 4
            and "duplicate_span" in codes,
            "quality=%s stored_spans=%d duplicate_notes=%s"
            % (receipt4["quality"], len(stored_after), "duplicate_span" in codes),
        )
    )

    # 5. Missing parent: dangling parent nulled, span kept, visible note.
    trace3 = "verify-trace-0003"
    orphan = {
        "traceId": trace3,
        "spanId": trace3 + "-orphan",
        "parentSpanId": trace3 + "-does-not-exist",
        "name": "tool.exec",
        "startTimeUnixNano": 1_700_000_000_000_000_000,
        "endTimeUnixNano": 1_700_000_001_000_000_000,
        "attributes": {"gen_ai.tool.name": "shell"},
        "status": {"code": 1},
    }
    receipt5 = import_spans(conn, [orphan], producer)
    doc5 = map_spans([orphan], producer)["documents"][0][1]
    results.append(
        _check(
            "missing_parent",
            receipt5["quality"] == "warnings"
            and doc5["spans"][0]["parent_native_id"] is None
            and any(w["code"] == "dangling_parent" for w in receipt5["warnings"]),
            "quality=%s parent_nulled=%s dangling_note=%s"
            % (
                receipt5["quality"],
                doc5["spans"][0]["parent_native_id"] is None,
                any(w["code"] == "dangling_parent" for w in receipt5["warnings"]),
            ),
        )
    )

    # 6. Unknown values: unknown span name -> kind other, name kept in
    #    attributes; unknown status code -> status unknown; unknown
    #    attribute value carried verbatim, never invented.
    trace4 = "verify-trace-0004"
    unknown_span = {
        "traceId": trace4,
        "spanId": trace4 + "-state",
        "parentSpanId": None,
        "name": "state.write",
        "startTimeUnixNano": 1_700_000_000_000_000_000,
        "endTimeUnixNano": 1_700_000_001_000_000_000,
        "attributes": {"some.future.field": "some-future-value", "state.ref": "ref:abc123"},
        "status": {"code": 7},
    }
    receipt6 = import_spans(conn, [unknown_span], producer)
    doc6 = map_spans([unknown_span], producer)["documents"][0][1]
    kept = doc6["spans"][0]
    results.append(
        _check(
            "unknown_values",
            kept["kind"] == "other"
            and kept["attributes"]["fleet.span"] == "state.write"
            and kept["attributes"]["some.future.field"] == "some-future-value"
            and kept["status"] == "unknown"
            and "state.ref" in kept["attributes"],
            "kind=%s status=%s unknown_attr_carried=%s"
            % (kept["kind"], kept["status"], kept["attributes"].get("some.future.field")),
        )
    )

    # 7. Partial coverage: a canonical document with no spans imports
    #    as partial, with the run recorded.
    trace5 = "verify-trace-0005"
    doc7 = {
        "schema_version": "0.1",
        "source": {
            "adapter": "canonical",
            "framework": "fleet-tracing",
            "framework_version": "1",
            "capabilities": {
                "supports_spans": True,
                "supports_graph_topology": True,
                "supports_node_execution_mapping": False,
                "supports_state_snapshots": False,
                "supports_checkpoints": False,
                "supports_interrupts": False,
                "supports_token_usage": True,
                "supports_cost": False,
            },
        },
        "run": {"native_id": trace5, "status": "unknown"},
        "spans": [],
    }
    receipt7 = import_canonical_document(conn, doc7, producer)
    run_row = conn.execute(
        "SELECT span_count, partial FROM runs WHERE trace_id = ?", (trace5,)
    ).fetchone()
    results.append(
        _check(
            "partial_coverage",
            receipt7["quality"] == "partial"
            and run_row is not None
            and run_row["span_count"] == 0
            and run_row["partial"] == 1,
            "quality=%s run_recorded=%s span_count=%s"
            % (receipt7["quality"], run_row is not None, run_row["span_count"] if run_row else None),
        )
    )

    # 8. Duplicate native id in one trace: run dropped, quality invalid.
    trace6 = "verify-trace-0006"
    dup = synthetic_turn(trace6)
    dup.append(dict(dup[1]))
    receipt8 = import_spans(conn, dup, producer)
    results.append(
        _check(
            "duplicate_native_id",
            receipt8["quality"] == "invalid"
            and receipt8["stats"]["runs"] == 0
            and receipt8["stats"]["dropped"] == 1,
            "quality=%s runs=%s dropped=%s"
            % (receipt8["quality"], receipt8["stats"]["runs"], receipt8["stats"]["dropped"]),
        )
    )

    # 9. Parent cycle: run dropped, quality invalid.
    trace7 = "verify-trace-0007"
    cycle_a = {
        "traceId": trace7, "spanId": trace7 + "-a", "parentSpanId": trace7 + "-b",
        "name": "agent.run", "startTimeUnixNano": 1_700_000_000_000_000_000,
        "endTimeUnixNano": 1_700_000_001_000_000_000, "attributes": {}, "status": {"code": 1},
    }
    cycle_b = {
        "traceId": trace7, "spanId": trace7 + "-b", "parentSpanId": trace7 + "-a",
        "name": "llm.call", "startTimeUnixNano": 1_700_000_000_500_000_000,
        "endTimeUnixNano": 1_700_000_001_500_000_000, "attributes": {}, "status": {"code": 1},
    }
    receipt9 = import_spans(conn, [cycle_a, cycle_b], producer)
    results.append(
        _check(
            "parent_cycle",
            receipt9["quality"] == "invalid"
            and receipt9["stats"]["runs"] == 0
            and receipt9["stats"]["dropped"] == 1,
            "quality=%s runs=%s dropped=%s"
            % (receipt9["quality"], receipt9["stats"]["runs"], receipt9["stats"]["dropped"]),
        )
    )

    # 10. Restart recovery: close and reopen the ledger; the first
    #     receipt resolves and re-delivery is still idempotent.
    conn.close()
    conn = ledger_mod.open_ledger(ledger_path)
    resolved = ledger_mod.get_receipt(conn, first_import_id)
    receipt10 = import_spans(conn, spans, producer)
    results.append(
        _check(
            "restart_recovery",
            resolved is not None
            and resolved["import_id"] == first_import_id
            and receipt10["already_imported"] is True,
            "receipt_resolves=%s reimport_idempotent=%s"
            % (resolved is not None, receipt10["already_imported"]),
        )
    )

    # 11. Native-ID preservation: stored rows carry the exact native
    #     ids; the mapped document carries them verbatim too.
    stored_ids = set(ledger_mod.existing_spans(conn, trace))
    expected = {trace + "-root", trace + "-llm", trace + "-tool", trace + "-late-tool"}
    doc11 = map_spans(spans, producer)["documents"][0][1]
    doc_ids = {s["native_id"] for s in doc11["spans"]}
    results.append(
        _check(
            "native_id_preservation",
            stored_ids == expected
            and doc_ids == {trace + "-root", trace + "-llm", trace + "-tool"}
            and doc11["run"]["native_id"] == trace,
            "stored=%s mapped=%s run=%s" % (sorted(stored_ids), sorted(doc_ids), doc11["run"]["native_id"]),
        )
    )

    # 12. No raw-payload storage: the stripped canary values never
    #     appear in the ledger file, and no raw-payload column exists.
    with open(ledger_path, "rb") as handle:
        blob = handle.read()
    canary_leaked = (
        b"CANARY-INPUT-MESSAGES-DO-NOT-STORE" in blob
        or b"CANARY-TOOL-ARGUMENTS-DO-NOT-STORE" in blob
    )
    columns = {
        row["name"]
        for row in conn.execute("SELECT name FROM pragma_table_info('spans')")
    }
    columns |= {
        row["name"]
        for row in conn.execute("SELECT name FROM pragma_table_info('imports')")
    }
    results.append(
        _check(
            "no_raw_payload_storage",
            not canary_leaked and "raw_payload" not in columns and "payload" not in columns,
            "canary_leaked=%s raw_payload_column=%s" % (canary_leaked, "raw_payload" in columns),
        )
    )

    # 13. OTLP content types: JSON accepted, protobuf refused.
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "copilot"}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "copilot"},
                        "spans": [
                            {
                                "traceId": "verify-trace-0008",
                                "spanId": "verify-trace-0008-root",
                                "name": "agent.run",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001000000000",
                                "attributes": [],
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    receipt13 = import_otlp_payload(conn, payload, producer)
    try:
        import_otlp_payload(conn, payload, producer, content_type="application/x-protobuf")
        protobuf_refused = False
    except ImportRefused:
        protobuf_refused = True
    results.append(
        _check(
            "otlp_content_types",
            receipt13["quality"] in ("complete", "warnings")
            and receipt13["stats"]["runs"] == 1
            and protobuf_refused,
            "json_quality=%s protobuf_refused=%s" % (receipt13["quality"], protobuf_refused),
        )
    )

    # 14. Receipt on every step: every receipt above has an import id,
    #     a quality, and the 11 stats keys.
    receipts = [receipt, receipt2, receipt_canary, receipt3, receipt4, receipt5,
                receipt6, receipt7, receipt8, receipt9, receipt10, receipt13]
    all_have = all(
        r.get("import_id") and r.get("quality") and set(r["stats"]) == set(STATS_KEYS)
        for r in receipts
    )
    results.append(
        _check(
            "receipt_on_every_step",
            all_have,
            "%d receipts checked, all carry import_id + quality + 11 stats keys" % len(receipts),
        )
    )
    return results, conn


def real_import_check(document, agentscope_src):
    """Run a mapped document through the REAL AgentScope importer.

    Returns ("pass"|"skip"|"fail", detail). Skips cleanly when the
    AgentScope checkout or pydantic is unavailable. Uses the real
    entry point, make_importer(data_dir), from pipeline.py — the same
    function the API calls. The real importer writes into a temporary
    data dir that is deleted afterwards.
    """
    import sys
    import tempfile
    from pathlib import Path

    if not agentscope_src:
        return "skip", "AGENTSCOPE_SRC is not set"
    backend_src = os.path.join(agentscope_src, "backend", "src")
    if not os.path.isdir(backend_src):
        return "skip", "no backend/src under AGENTSCOPE_SRC=%s" % agentscope_src
    try:
        import pydantic  # noqa: F401
    except ImportError:
        return "skip", "pydantic is not installed in this interpreter"
    if backend_src not in sys.path:
        sys.path.insert(0, backend_src)
    try:
        from agentscope.ingest.pipeline import make_importer
        from agentscope.store.db import DB_FILENAME, connect
        from agentscope.store.migrate import apply_migrations
    except ImportError as exc:
        return "skip", "could not import the real importer entry point: %s" % exc
    from .mapper import canonical_bytes

    doc_bytes = canonical_bytes(document)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            # The API applies migrations at startup (api/app.py lifespan);
            # make_importer alone does not create the schema.
            conn = connect(tmp_path / DB_FILENAME)
            try:
                apply_migrations(conn)
            finally:
                conn.close()
            importer = make_importer(tmp_path)
            report = importer(doc_bytes, "fleet-trace.json")
        except Exception as exc:  # noqa: BLE001 - surface as fail detail
            return "fail", "real importer raised %s: %s" % (type(exc).__name__, exc)
        quality = getattr(report.quality, "value", str(report.quality))
        if quality not in ("complete", "warnings"):
            return "fail", "real importer quality=%r (expected complete or warnings)" % quality
        if not report.run_ids:
            return "fail", "real importer returned no run_ids"
        try:
            report2 = importer(doc_bytes, "fleet-trace.json")
        except Exception as exc:  # noqa: BLE001 - surface as fail detail
            return "fail", "real importer raised on re-import: %s: %s" % (type(exc).__name__, exc)
        if report2.already_imported is not True:
            return "fail", "real importer did not flag the second identical import as already_imported"
    return "pass", "real importer accepted the document (quality=%s, run_ids=%s, idempotent)" % (
        quality,
        report.run_ids,
    )
