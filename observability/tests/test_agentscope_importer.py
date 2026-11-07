"""Milestone 5 tests (unittest) — agentscope_importer contract.

Covers the M5 contract (CONTRACT.md): mapping (kinds, tokens, graph,
seq order, native-ID preservation, dangling parents, duplicate native
ids, parent cycles, unknown values, stripped content), the import
operation (receipt shape, duplicate delivery, late spans, out-of-order
arrival, partial coverage, OTLP content types, restart recovery,
retention purge, no raw-payload storage, span conflicts, event dedup),
the file entry points (JSON / JSONL), the CLI assertion
(verify.run_verify), and the real-import check (skips cleanly without
AGENTSCOPE_SRC + pydantic; see examples/verify_agentscope_import.py).

Synthetic spans only — no AgentScope server, no live vendor runs.
The live contract (real import on the laptop) is the reviewer's job
(see reviews/milestone-05/builder-evidence.md).

Run from observability/:
    ../.venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, sys_path)

from agentscope_importer import ledger as ledger_mod  # noqa: E402
from agentscope_importer.contract import STATS_KEYS  # noqa: E402
from agentscope_importer.importer import (  # noqa: E402
    ImportRefused,
    ImportSettings,
    import_canonical_document,
    import_file,
    import_otlp_payload,
    import_spans,
)
from agentscope_importer.mapper import (  # noqa: E402
    canonical_bytes,
    map_spans,
)
from agentscope_importer.verify import (  # noqa: E402
    real_import_check,
    run_verify,
    synthetic_turn,
)


BASE = 1_700_000_000_000_000_000


def span(trace_id, span_id, parent=None, name="agent.run", start=BASE,
         end=BASE + 1_000_000_000, attributes=None, code=1):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": attributes or {},
        "status": {"code": code},
    }


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp.name, "ledger.sqlite3")
        self.conn = ledger_mod.open_ledger(self.ledger_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------

class TestMapper(unittest.TestCase):
    def test_happy_path_kinds_tokens_graph(self):
        spans = synthetic_turn("t1")
        mapped = map_spans(spans, "copilot")
        self.assertEqual(len(mapped["documents"]), 1)
        doc = mapped["documents"][0][1]
        self.assertEqual(doc["schema_version"], "0.1")
        self.assertEqual(doc["run"]["native_id"], "t1")
        self.assertEqual(doc["run"]["status"], "ok")
        kinds = {s["native_id"]: s["kind"] for s in doc["spans"]}
        self.assertEqual(kinds, {
            "t1-root": "run", "t1-llm": "llm", "t1-tool": "tool"})
        seqs = {s["native_id"]: s["seq"] for s in doc["spans"]}
        self.assertEqual(seqs, {"t1-root": 0, "t1-llm": 1, "t1-tool": 2})
        llm = [s for s in doc["spans"] if s["kind"] == "llm"][0]
        self.assertEqual(llm["llm"]["model"], "gpt-5")
        self.assertEqual(llm["llm"]["input_tokens"], 120)
        self.assertEqual(llm["llm"]["output_tokens"], 340)
        tool = [s for s in doc["spans"] if s["kind"] == "tool"][0]
        self.assertEqual(tool["tool"]["name"], "read_file")
        self.assertEqual(tool["tool"]["call_id"], "call-1")
        self.assertEqual(doc["graph"]["entry"], "t1-root")
        self.assertEqual(len(doc["graph"]["nodes"]), 3)
        self.assertEqual(len(doc["graph"]["edges"]), 2)
        self.assertEqual(doc["source"]["adapter"], "canonical")
        self.assertTrue(doc["source"]["capabilities"]["supports_token_usage"])
        self.assertEqual(mapped["tokens_in"], 120)
        self.assertEqual(mapped["tokens_out"], 340)

    def test_native_ids_verbatim_and_run_id(self):
        mapped = map_spans(synthetic_turn("t-native"), "copilot")
        doc = mapped["documents"][0][1]
        self.assertEqual(doc["run"]["native_id"], "t-native")
        self.assertEqual(
            {s["native_id"] for s in doc["spans"]},
            {"t-native-root", "t-native-llm", "t-native-tool"})

    def test_seq_follows_start_time_not_arrival_order(self):
        spans = list(reversed(synthetic_turn("t-order")))
        doc = map_spans(spans, "copilot")["documents"][0][1]
        seqs = {s["native_id"]: s["seq"] for s in doc["spans"]}
        self.assertEqual(seqs["t-order-root"], 0)
        self.assertEqual(seqs["t-order-llm"], 1)
        self.assertEqual(seqs["t-order-tool"], 2)

    def test_dangling_parent_nulled_with_note(self):
        orphan = span("t-dangle", "t-dangle-1", parent="t-dangle-ghost", name="tool.exec",
                      attributes={"gen_ai.tool.name": "shell"})
        mapped = map_spans([orphan], "copilot")
        doc = mapped["documents"][0][1]
        self.assertIsNone(doc["spans"][0]["parent_native_id"])
        self.assertTrue(any(n["code"] == "dangling_parent" for n in mapped["notes"]))

    def test_duplicate_native_id_drops_run(self):
        spans = synthetic_turn("t-dup")
        spans.append(dict(spans[1]))
        mapped = map_spans(spans, "copilot")
        self.assertEqual(mapped["documents"], [])
        self.assertEqual(len(mapped["dropped"]), 1)
        self.assertEqual(mapped["dropped"][0]["code"], "duplicate_native_id")

    def test_parent_cycle_drops_run(self):
        a = span("t-cycle", "t-cycle-a", parent="t-cycle-b")
        b = span("t-cycle", "t-cycle-b", parent="t-cycle-a", name="llm.call",
                 start=BASE + 500_000_000, end=BASE + 1_500_000_000)
        mapped = map_spans([a, b], "copilot")
        self.assertEqual(mapped["documents"], [])
        self.assertEqual(mapped["dropped"][0]["code"], "parent_cycle")

    def test_self_parent_is_a_cycle(self):
        a = span("t-self", "t-self-a", parent="t-self-a")
        mapped = map_spans([a], "copilot")
        self.assertEqual(mapped["documents"], [])
        self.assertEqual(mapped["dropped"][0]["code"], "parent_cycle")

    def test_unknown_span_name_and_status_carried(self):
        unknown = span("t-unk", "t-unk-1", name="state.write", code=7,
                       attributes={"some.future.field": "v", "state.ref": "ref:1"})
        doc = map_spans([unknown], "copilot")["documents"][0][1]
        kept = doc["spans"][0]
        self.assertEqual(kept["kind"], "other")
        self.assertEqual(kept["attributes"]["fleet.span"], "state.write")
        self.assertEqual(kept["attributes"]["some.future.field"], "v")
        self.assertEqual(kept["status"], "unknown")

    def test_stripped_content_dropped_and_counted(self):
        spans = synthetic_turn("t-strip", with_canaries=True)
        mapped = map_spans(spans, "copilot")
        doc = mapped["documents"][0][1]
        llm = [s for s in doc["spans"] if s["kind"] == "llm"][0]
        self.assertNotIn("gen_ai.input.messages", llm["attributes"])
        tool = [s for s in doc["spans"] if s["kind"] == "tool"][0]
        self.assertNotIn("gen_ai.tool.call.arguments", tool["attributes"])
        self.assertTrue(any(n["code"] == "sanitized_dropped" for n in mapped["notes"]))

    def test_string_times_from_otlp_json(self):
        s = span("t-str", "t-str-1", name="agent.run")
        s["startTimeUnixNano"] = str(BASE)
        s["endTimeUnixNano"] = str(BASE + 1_000_000_000)
        doc = map_spans([s], "copilot")["documents"][0][1]
        self.assertTrue(doc["spans"][0]["started_at"].startswith("2023-11-14"))
        self.assertIsNotNone(doc["spans"][0]["duration_ms"])

    def test_end_before_start_noted_duration_unknown(self):
        s = span("t-ebs", "t-ebs-1", start=BASE + 2_000_000_000, end=BASE)
        mapped = map_spans([s], "copilot")
        doc = mapped["documents"][0][1]
        self.assertIsNone(doc["spans"][0]["duration_ms"])
        self.assertTrue(any(n["code"] == "end_before_start" for n in mapped["notes"]))

    def test_multiple_traces_become_multiple_documents(self):
        spans = synthetic_turn("t-a") + synthetic_turn("t-b")
        mapped = map_spans(spans, "copilot")
        self.assertEqual(len(mapped["documents"]), 2)
        self.assertEqual({t for t, _ in mapped["documents"]}, {"t-a", "t-b"})

    def test_unreadable_spans_counted_not_crash(self):
        mapped = map_spans([{"no": "ids"}, "not-a-dict", None], "copilot")
        self.assertEqual(mapped["documents"], [])
        self.assertEqual(mapped["unreadable"], 3)

    def test_canonical_bytes_deterministic(self):
        doc = map_spans(synthetic_turn("t-det"), "copilot")["documents"][0][1]
        self.assertEqual(canonical_bytes(doc), canonical_bytes(doc))


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------

class TestImporter(LedgerCase):
    def test_receipt_shape(self):
        receipt = import_spans(self.conn, synthetic_turn("r1"), "copilot")
        self.assertTrue(receipt["import_id"].startswith("imp_"))
        self.assertEqual(receipt["quality"], "complete")
        self.assertEqual(set(receipt["stats"]), set(STATS_KEYS))
        self.assertEqual(receipt["stats"]["runs"], 1)
        self.assertEqual(receipt["stats"]["spans"], 3)
        self.assertEqual(receipt["stats"]["node_executions"], 0)
        self.assertEqual(receipt["stats"]["payloads"], 0)
        self.assertEqual(len(receipt["run_ids"]), 1)
        self.assertFalse(receipt["already_imported"])
        self.assertEqual(receipt["tokens_in"], 120)
        self.assertEqual(receipt["tokens_out"], 340)
        self.assertEqual(receipt["provenance"]["schema_version"], "0.1")

    def test_duplicate_delivery_idempotent(self):
        spans = synthetic_turn("r2")
        first = import_spans(self.conn, spans, "copilot")
        before = ledger_mod.counts(self.conn)
        second = import_spans(self.conn, spans, "copilot")
        self.assertTrue(second["already_imported"])
        self.assertEqual(second["import_id"], first["import_id"])
        self.assertEqual(ledger_mod.counts(self.conn), before)

    def test_duplicate_content_with_bad_line_gets_distinct_receipt(self):
        # Double-check finding: a duplicate-content upload that also
        # carries an unreadable line must NOT return the original
        # receipt — the skipped line is part of THIS upload and must
        # be visible. The spans are still not stored twice.
        spans = synthetic_turn("r2b")
        first = import_spans(self.conn, spans, "copilot")
        lines = [json.dumps(s) for s in spans] + ["{broken"]
        path = os.path.join(self.tmp.name, "dup.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        before = ledger_mod.counts(self.conn)
        second = import_file(self.conn, path, "copilot")
        self.assertFalse(second["already_imported"])
        self.assertNotEqual(second["import_id"], first["import_id"])
        self.assertEqual(second["quality"], "partial")
        self.assertEqual(second["stats"]["detection_skipped_lines"], 1)
        self.assertEqual(second["stats"]["spans"], 3)
        self.assertEqual(ledger_mod.counts(self.conn)["spans"], before["spans"])

    def test_same_documents_different_dropped_runs_get_distinct_receipts(self):
        # Same mapped documents, different dropped runs: upload A drops
        # a run, upload B drops nothing. B must not inherit A's receipt.
        good = synthetic_turn("r2c-good")
        bad = synthetic_turn("r2c-bad")
        bad.append(dict(bad[1]))
        first = import_spans(self.conn, good + bad, "copilot")
        self.assertEqual(first["quality"], "partial")
        self.assertEqual(first["stats"]["dropped"], 1)
        second = import_spans(self.conn, good, "copilot")
        self.assertFalse(second["already_imported"])
        # Quality is warnings, not complete: this upload's spans were
        # all duplicates of the first upload's stored rows — honestly
        # reported, and no rows were stored twice. What matters is
        # that it did NOT inherit the first receipt (dropped=0 here).
        self.assertEqual(second["quality"], "warnings")
        self.assertEqual(second["stats"]["dropped"], 0)
        self.assertTrue(any(w["code"] == "duplicate_span" for w in second["warnings"]))

    def test_late_span_joins_run(self):
        spans = synthetic_turn("r3")
        import_spans(self.conn, spans, "copilot")
        late = span("r3", "r3-late", parent="r3-root", name="tool.exec",
                    start=BASE + 5_000_000_000, end=BASE + 6_000_000_000,
                    attributes={"gen_ai.tool.name": "write_file"})
        receipt = import_spans(self.conn, spans + [late], "copilot")
        self.assertEqual(receipt["quality"], "warnings")
        self.assertTrue(any(w["code"] == "duplicate_span" for w in receipt["warnings"]))
        stored = ledger_mod.existing_spans(self.conn, "r3")
        self.assertEqual(len(stored), 4)
        self.assertIn("r3-late", stored)

    def test_span_conflict_first_write_wins(self):
        spans = synthetic_turn("r4")
        import_spans(self.conn, spans, "copilot")
        changed = [dict(s) for s in spans]
        changed[1]["attributes"] = dict(changed[1]["attributes"])
        changed[1]["attributes"]["gen_ai.request.model"] = "gpt-6"
        receipt = import_spans(self.conn, changed, "copilot")
        self.assertTrue(any(w["code"] == "span_conflict" for w in receipt["warnings"]))
        stored = ledger_mod.existing_spans(self.conn, "r4")
        self.assertEqual(len(stored), 3)
        # The stored attributes still carry the FIRST write's model.
        self.assertEqual(stored["r4-llm"]["attributes"]["gen_ai.request.model"], "gpt-5")

    def test_out_of_order_new_trace(self):
        spans = list(reversed(synthetic_turn("r5")))
        receipt = import_spans(self.conn, spans, "copilot")
        self.assertEqual(receipt["quality"], "complete")
        self.assertEqual(len(ledger_mod.existing_spans(self.conn, "r5")), 3)

    def test_missing_parent_imports_with_warning(self):
        orphan = span("r6", "r6-1", parent="r6-ghost", name="tool.exec",
                      attributes={"gen_ai.tool.name": "shell"})
        receipt = import_spans(self.conn, [orphan], "copilot")
        self.assertEqual(receipt["quality"], "warnings")
        self.assertTrue(any(w["code"] == "dangling_parent" for w in receipt["warnings"]))
        self.assertEqual(len(ledger_mod.existing_spans(self.conn, "r6")), 1)

    def test_partial_coverage_direct_document(self):
        doc = {
            "schema_version": "0.1",
            "source": {"adapter": "canonical"},
            "run": {"native_id": "r7", "status": "unknown"},
            "spans": [],
        }
        receipt = import_canonical_document(self.conn, doc, "copilot")
        self.assertEqual(receipt["quality"], "partial")
        row = self.conn.execute(
            "SELECT span_count, partial FROM runs WHERE trace_id = ?", ("r7",)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["span_count"], 0)
        self.assertEqual(row["partial"], 1)

    def test_duplicate_native_id_invalid(self):
        spans = synthetic_turn("r8")
        spans.append(dict(spans[1]))
        receipt = import_spans(self.conn, spans, "copilot")
        self.assertEqual(receipt["quality"], "invalid")
        self.assertEqual(receipt["stats"]["runs"], 0)
        self.assertEqual(receipt["stats"]["dropped"], 1)
        self.assertEqual(receipt["stats"]["unmapped"], 4)
        self.assertEqual(ledger_mod.counts(self.conn)["runs"], 0)

    def test_parent_cycle_invalid(self):
        a = span("r9", "r9-a", parent="r9-b")
        b = span("r9", "r9-b", parent="r9-a", name="llm.call",
                 start=BASE + 500_000_000, end=BASE + 1_500_000_000)
        receipt = import_spans(self.conn, [a, b], "copilot")
        self.assertEqual(receipt["quality"], "invalid")
        self.assertEqual(receipt["stats"]["dropped"], 1)

    def test_restart_recovery(self):
        spans = synthetic_turn("r10")
        first = import_spans(self.conn, spans, "copilot")
        self.conn.close()
        self.conn = ledger_mod.open_ledger(self.ledger_path)
        resolved = ledger_mod.get_receipt(self.conn, first["import_id"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["import_id"], first["import_id"])
        second = import_spans(self.conn, spans, "copilot")
        self.assertTrue(second["already_imported"])

    def test_otlp_json_accepted_protobuf_refused(self):
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "copilot"}}]},
                "scopeSpans": [{
                    "scope": {"name": "copilot"},
                    "spans": [{
                        "traceId": "r11", "spanId": "r11-root", "name": "agent.run",
                        "startTimeUnixNano": str(BASE), "endTimeUnixNano": str(BASE + 1_000_000_000),
                        "attributes": [], "status": {"code": 1},
                    }],
                }],
            }]
        }
        receipt = import_otlp_payload(self.conn, payload, "copilot")
        self.assertIn(receipt["quality"], ("complete", "warnings"))
        self.assertEqual(receipt["stats"]["runs"], 1)
        with self.assertRaises(ImportRefused):
            import_otlp_payload(self.conn, payload, "copilot",
                                content_type="application/x-protobuf")

    def test_unsupported_payload_receipt(self):
        receipt = import_otlp_payload(self.conn, {"nope": True}, "copilot")
        self.assertEqual(receipt["quality"], "unsupported")
        self.assertTrue(receipt["import_id"])
        self.assertEqual(receipt["stats"]["runs"], 0)
        resolved = ledger_mod.get_receipt(self.conn, receipt["import_id"])
        self.assertIsNotNone(resolved)

    def test_empty_span_list_invalid(self):
        receipt = import_spans(self.conn, [], "copilot")
        self.assertEqual(receipt["quality"], "invalid")
        self.assertEqual(receipt["stats"]["runs"], 0)

    def test_unsupported_schema_version(self):
        doc = {"schema_version": "9.9", "run": {"native_id": "r12"}, "spans": []}
        receipt = import_canonical_document(self.conn, doc, "copilot")
        self.assertEqual(receipt["quality"], "unsupported")

    def test_direct_document_unknown_fields_visible(self):
        doc = map_spans(synthetic_turn("r13"), "copilot")["documents"][0][1]
        doc["mystery_top"] = "x"
        doc["spans"][0]["mystery_span"] = "y"
        receipt = import_canonical_document(self.conn, doc, "copilot")
        codes = {w["code"] for w in receipt["warnings"]}
        self.assertIn("unknown_key", codes)
        self.assertIn("unknown_span_field", codes)
        self.assertEqual(receipt["quality"], "warnings")

    def test_event_dedup_by_explicit_identity(self):
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": []},
                "scopeSpans": [{
                    "scope": {"name": "copilot"},
                    "spans": [{
                        "traceId": "r14", "spanId": "r14-root", "name": "agent.run",
                        "startTimeUnixNano": str(BASE), "endTimeUnixNano": str(BASE + 1_000_000_000),
                        "attributes": [],
                        "events": [{
                            "name": "tool.called",
                            "timeUnixNano": str(BASE + 100_000_000),
                            "attributes": [{"key": "tool", "value": {"stringValue": "read"}}],
                        }],
                        "status": {"code": 1},
                    }],
                }],
            }]
        }
        first = import_otlp_payload(self.conn, payload, "copilot")
        self.assertEqual(first["stats"]["events"], 1)
        # Same event id re-delivered with a second span in the same trace.
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"].append({
            "traceId": "r14", "spanId": "r14-late", "parentSpanId": "r14-root",
            "name": "tool.exec", "startTimeUnixNano": str(BASE + 2_000_000_000),
            "endTimeUnixNano": str(BASE + 3_000_000_000), "attributes": [],
            "events": [{
                "name": "tool.called",
                "timeUnixNano": str(BASE + 100_000_000),
                "attributes": [],
            }],
            "status": {"code": 1},
        })
        # The late span's event has a different native id (different span),
        # so it is a new event; the root's event is a duplicate.
        second = import_otlp_payload(self.conn, payload, "copilot")
        self.assertTrue(any(w["code"] == "duplicate_event" for w in second["warnings"]))
        self.assertEqual(ledger_mod.counts(self.conn)["events"], 2)

    def test_no_raw_payload_storage(self):
        import_spans(self.conn, synthetic_turn("r15", with_canaries=True), "copilot")
        self.conn.commit()
        with open(self.ledger_path, "rb") as handle:
            blob = handle.read()
        self.assertNotIn(b"CANARY-INPUT-MESSAGES-DO-NOT-STORE", blob)
        self.assertNotIn(b"CANARY-TOOL-ARGUMENTS-DO-NOT-STORE", blob)

    def test_retention_purge_max_imports(self):
        settings = ImportSettings(max_imports=3, max_age_days=365)
        for i in range(5):
            import_spans(self.conn, synthetic_turn("purge-%d" % i), "copilot",
                         settings=settings, received_at="2026-11-07T%02d:00:00Z" % i)
        self.assertEqual(ledger_mod.counts(self.conn)["imports"], 3)

    def test_retention_purge_max_age(self):
        settings = ImportSettings(max_imports=100, max_age_days=30)
        import_spans(self.conn, synthetic_turn("old-1"), "copilot", settings=settings,
                     received_at="2026-09-01T00:00:00Z")
        import_spans(self.conn, synthetic_turn("new-1"), "copilot", settings=settings,
                     received_at="2026-11-07T00:00:00Z")
        self.assertEqual(ledger_mod.counts(self.conn)["imports"], 1)
        self.assertEqual(len(ledger_mod.existing_spans(self.conn, "new-1")), 3)
        self.assertEqual(len(ledger_mod.existing_spans(self.conn, "old-1")), 0)

    def test_bounds_refused(self):
        with self.assertRaises(ImportRefused):
            import_spans(self.conn, [span("b", "b-%d" % i) for i in range(3)],
                         "copilot", settings=ImportSettings(max_spans_per_import=2))


class TestImportFile(LedgerCase):
    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_json_array(self):
        path = self._write("spans.json", json.dumps(synthetic_turn("f1")))
        receipt = import_file(self.conn, path, "copilot")
        self.assertEqual(receipt["quality"], "complete")
        self.assertEqual(receipt["stats"]["spans"], 3)

    def test_jsonl_with_bad_line(self):
        lines = [json.dumps(s) for s in synthetic_turn("f2")]
        lines.append("{not json")
        path = self._write("spans.jsonl", "\n".join(lines) + "\n")
        receipt = import_file(self.conn, path, "copilot")
        self.assertEqual(receipt["quality"], "partial")
        self.assertEqual(receipt["stats"]["detection_skipped_lines"], 1)
        self.assertEqual(receipt["stats"]["spans"], 3)

    def test_jsonl_all_bad_invalid(self):
        path = self._write("bad.jsonl", "{nope\n{also nope\n")
        receipt = import_file(self.conn, path, "copilot")
        self.assertEqual(receipt["quality"], "invalid")

    def test_canonical_json_file(self):
        doc = map_spans(synthetic_turn("f3"), "copilot")["documents"][0][1]
        path = self._write("doc.json", json.dumps(doc))
        receipt = import_file(self.conn, path, "copilot")
        self.assertEqual(receipt["quality"], "complete")
        self.assertEqual(receipt["stats"]["runs"], 1)

    def test_otlp_json_file(self):
        payload = {"resourceSpans": [{
            "resource": {"attributes": []},
            "scopeSpans": [{"scope": {"name": "copilot"}, "spans": [{
                "traceId": "f4", "spanId": "f4-root", "name": "agent.run",
                "startTimeUnixNano": str(BASE), "endTimeUnixNano": str(BASE + 1_000_000_000),
                "attributes": [], "status": {"code": 1},
            }]}],
        }]}
        path = self._write("otlp.json", json.dumps(payload))
        receipt = import_file(self.conn, path, "copilot")
        self.assertEqual(receipt["stats"]["runs"], 1)


class TestVerify(unittest.TestCase):
    def test_run_verify_all_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "v.sqlite3")
            conn = ledger_mod.open_ledger(ledger_path)
            results, conn = run_verify(conn, ledger_path)
            conn.close()
        failed = [r for r in results if not r["passed"]]
        self.assertEqual(len(results), 15)
        self.assertEqual(failed, [])

    def test_real_import_check_skips_without_checkout(self):
        status, _ = real_import_check(
            map_spans(synthetic_turn("skip-1"), "copilot")["documents"][0][1], "")
        self.assertEqual(status, "skip")

    def test_real_import_check_with_checkout(self):
        src = os.environ.get("AGENTSCOPE_SRC", "")
        if not src:
            self.skipTest("AGENTSCOPE_SRC is not set")
        document = map_spans(synthetic_turn("real-1"), "copilot")["documents"][0][1]
        status, detail = real_import_check(document, src)
        self.assertEqual(status, "pass", detail)


if __name__ == "__main__":
    unittest.main()
