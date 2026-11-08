"""Synthetic-only security, snapshot, transport, and readback tests."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope_importer import bridge, ledger  # noqa: E402
from agentscope_importer.bridge import (  # noqa: E402
    BridgeError, deliver_snapshot, predict_target_ids, validate_artifact,
    validate_server, verify_readback,
)
from agentscope_importer.importer import import_spans  # noqa: E402
from agentscope_importer.mapper import canonical_bytes, map_spans  # noqa: E402


TRACE_A = "a" * 32
ROOT = "1" * 16
CHILD = "2" * 16
BASE = 1_700_000_000_000_000_000


def source_spans(trace=TRACE_A, with_llm=True):
    spans = [{
        "traceId": trace, "spanId": ROOT, "name": "agent.run",
        "startTimeUnixNano": BASE, "endTimeUnixNano": BASE + 2_000_000_000,
        "attributes": {"agent.name": "copilot"}, "status": {"code": 1},
    }]
    if with_llm:
        spans.append({
            "traceId": trace, "spanId": CHILD, "parentSpanId": ROOT,
            "name": "llm.call", "startTimeUnixNano": BASE + 500_000_000,
            "endTimeUnixNano": BASE + 1_500_000_000,
            "attributes": {"gen_ai.request.model": "gpt-5", "gen_ai.usage.input_tokens": 17,
                           "gen_ai.usage.output_tokens": 9}, "status": {"code": 1},
        })
    return spans


def canonical_doc(trace=TRACE_A, with_llm=True):
    return map_spans(source_spans(trace, with_llm), "copilot")["documents"][0][1]


def report_for(raw, quality="partial", already=False):
    ids = predict_target_ids(raw)
    doc = json.loads(raw.decode("utf-8"))
    return {
        "import_id": ids["import_id"], "quality": quality,
        "run_ids": list(ids["runs"].values()), "already_imported": already,
        "stats": {
            "runs": 1, "spans": len(doc["spans"]), "node_executions": 0,
            "events": 0, "checkpoints": 0, "payloads": 0, "findings": 0,
            "warnings": 0, "dropped": 0, "unmapped": 0,
            "detection_skipped_lines": 0,
        },
        "warnings": [],
    }


def readback_for(raw):
    doc = json.loads(raw.decode("utf-8"))
    ids = predict_target_ids(raw)
    trace = doc["run"]["native_id"]
    rid = ids["runs"][trace]
    report = report_for(raw)
    run = {
        "id": rid, "status": doc["run"]["status"],
        "quality": report["quality"], "graph_mode": "tree",
        "capabilities": doc["source"]["capabilities"],
    }
    run_summary = {
        "id": rid, "status": doc["run"]["status"], "graph_mode": "tree",
        "started_us": bridge._timestamp_to_us(doc["run"].get("started_at"), "test"),
        "ended_us": bridge._timestamp_to_us(doc["run"].get("ended_at"), "test"),
    }
    spans = []
    for index, expected in enumerate(doc["spans"]):
        native = expected["native_id"]
        started_us = bridge._timestamp_to_us(expected.get("started_at"), "test")
        ended_us = bridge._timestamp_to_us(expected.get("ended_at"), "test")
        row = {
            "id": ids["spans"][trace][native], "native_id": native,
            "parent_id": ids["spans"][trace].get(expected["parent_native_id"]),
            "seq": expected["seq"], "kind": expected["kind"], "name": expected["name"],
            "status": expected["status"], "started_us": started_us,
            "ended_us": ended_us,
            "duration_us": ended_us - started_us if started_us is not None and ended_us is not None else None,
            "node_name": expected.get("node_name"), "attributes": expected["attributes"],
            "model": (expected.get("llm") or {}).get("model"),
            "tokens_in": (expected.get("llm") or {}).get("input_tokens"),
            "tokens_out": (expected.get("llm") or {}).get("output_tokens"),
            "tokens_reasoning": (expected.get("llm") or {}).get("reasoning_tokens"),
            "finish_reason": None,
            "tool_name": (expected.get("tool") or {}).get("name"),
            "tool_call_id": (expected.get("tool") or {}).get("call_id"),
            "error": expected.get("error"),
            "input_payload": None, "output_payload": None,
            "source_ref": {"pointer": "/spans/%d" % index},
            "cost_micro_usd": None, "cost_basis": None,
        }
        spans.append(row)
    return run, spans, run_summary


def pinned_agentscope_backend_src():
    configured = os.environ.get("AGENTSCOPE_SRC")
    candidates = []
    if configured:
        candidates.append(Path(configured) / "backend" / "src")
    candidates.append(
        Path(__file__).resolve().parents[3]
        / "agentscope-api-inspect"
        / "backend"
        / "src"
    )
    for candidate in candidates:
        if (candidate / "agentscope" / "ingest" / "pipeline.py").is_file():
            return candidate.resolve()
    raise unittest.SkipTest("pinned AgentScope checkout is unavailable")


def pinned_agentscope_projections(raw):
    backend_src = pinned_agentscope_backend_src()
    try:
        import pydantic  # noqa: F401
    except ImportError as exc:
        raise unittest.SkipTest("pydantic is unavailable: %s" % exc) from exc
    backend_src_text = str(backend_src)
    if backend_src_text in sys.path:
        sys.path.remove(backend_src_text)
    sys.path.insert(0, backend_src_text)

    from agentscope.api import views
    from agentscope.ingest import pipeline
    from agentscope.store.db import DB_FILENAME, connect
    from agentscope.store.migrate import apply_migrations
    from agentscope.store.repos import findings, imports, runs, spans

    if not Path(pipeline.__file__).resolve().is_relative_to(backend_src):
        raise AssertionError("AgentScope ingestion did not load from the pinned checkout")

    with tempfile.TemporaryDirectory(prefix="fleet-bridge-agentscope-") as tmp:
        data_dir = Path(tmp)
        conn = connect(data_dir / DB_FILENAME)
        try:
            apply_migrations(conn)
            receipt = pipeline.import_bytes(conn, data_dir, raw, "synthetic.json")
            imported = imports.get_import(conn, receipt.import_id)
            run_row = runs.get_run(conn, receipt.run_ids[0])
            span_rows = spans.list_spans_page(conn, receipt.run_ids[0], None, 200)
            finding_rows = findings.list_findings(conn, receipt.run_ids[0], None, 100_000)
            return {
                "report": views.import_report(imported, receipt.run_ids).model_dump(mode="json"),
                "run": views.run_detail(run_row, imported.quality).model_dump(mode="json"),
                "run_summary": views.run_summary(run_row).model_dump(mode="json"),
                "spans": [views.span_view(row).model_dump(mode="json") for row in span_rows],
                "findings": [views.finding_view(row).model_dump(mode="json") for row in finding_rows],
            }
        finally:
            conn.close()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_loopback_literal_only(self):
        self.assertEqual(validate_server("http://127.0.0.1:8765").hostname, "127.0.0.1")
        self.assertEqual(validate_server("http://[::1]:8765").hostname, "::1")
        self.assertTrue(validate_server("http://127.0.0.2:8765").hostname.startswith("127."))
        for url in (
            "http://localhost:8765", "http://example.com:8765", "http://127.0.0.1.evil:8765",
            "https://127.0.0.1:8765", "http://user@127.0.0.1:8765",
            "http://127.0.0.1:8765/path", "http://127.0.0.1:8765?x=1",
            "http://127.0.0.1:99999", "http://[::1%25lo]:8765",
        ):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                validate_server(url)

    def test_predicts_target_ids_from_exact_canonical_upload_bytes(self):
        doc = canonical_doc()
        raw = canonical_bytes(doc)
        digest = hashlib.sha256(raw).hexdigest()
        expected = {
            "import_id": "imp_" + hashlib.sha256((digest + "|import|").encode()).hexdigest()[:16],
            "runs": {TRACE_A: "run_" + hashlib.sha256((digest + "|run|runs/0").encode()).hexdigest()[:16]},
            "spans": {TRACE_A: {
                native: "spn_" + hashlib.sha256((digest + "|span|runs/0/" + native).encode()).hexdigest()[:16]
                for native in (ROOT, CHILD)
            }},
        }
        self.assertEqual(predict_target_ids(raw), expected)

    def test_same_span_id_in_distinct_traces_gets_distinct_target_ids(self):
        first = canonical_bytes(canonical_doc("a" * 32, False))
        second = canonical_bytes(canonical_doc("b" * 32, False))
        first_ids, second_ids = predict_target_ids(first), predict_target_ids(second)
        self.assertNotEqual(first_ids["runs"], second_ids["runs"])
        self.assertNotEqual(first_ids["spans"]["a" * 32][ROOT], second_ids["spans"]["b" * 32][ROOT])

    def test_artifact_allowlist_rejects_prompt_and_unknown_nested_data(self):
        doc = canonical_doc()
        doc["spans"][0]["attributes"]["gen_ai.input.messages"] = "SYNTHETIC-CANARY"
        with self.assertRaises(BridgeError):
            validate_artifact(doc, canonical_bytes(doc))
        doc = canonical_doc()
        doc["spans"][0]["unexpected"] = {"prompt": "SYNTHETIC-CANARY"}
        with self.assertRaises(BridgeError):
            validate_artifact(doc, canonical_bytes(doc))
        doc = canonical_doc()
        doc["spans"][0]["attributes"]["agent.name"] = "Bearer SYNTHETIC-CANARY"
        with self.assertRaises(BridgeError):
            validate_artifact(doc, canonical_bytes(doc))

    def test_approved_error_messages_bypass_identifier_grammar_but_remain_exact(self):
        for message in bridge.SAFE_ERROR_MESSAGES:
            with self.subTest(message=message):
                doc = canonical_doc()
                doc["spans"][0]["attributes"]["error.message"] = message
                self.assertTrue(validate_artifact(doc, canonical_bytes(doc)))

                rejected = canonical_doc()
                rejected["spans"][0]["attributes"]["error.message"] = message + " extra"
                with self.assertRaises(BridgeError):
                    validate_artifact(rejected, canonical_bytes(rejected))

                bridge._validate_otlp_attrs(
                    [{"key": "error.message", "value": {"stringValue": message}}],
                    "synthetic test",
                )

    def test_pinned_ingestion_projections_accept_findings_and_duration_warning_pointer(self):
        doc = canonical_doc()
        doc["spans"][0]["duration_ms"] = 1
        raw = canonical_bytes(doc)
        self.assertTrue(validate_artifact(doc, raw))

        projections = pinned_agentscope_projections(raw)
        report = projections["report"]
        self.assertGreater(report["stats"]["findings"], 0)
        self.assertEqual(report["stats"]["findings"], len(projections["findings"]))
        self.assertIn(
            {"pointer": "/spans/0/duration_ms"},
            [warning["source_ref"] for warning in report["warnings"]],
        )
        self.assertTrue(
            verify_readback(
                raw,
                report,
                projections["run"],
                projections["spans"],
                persisted_report=report,
                run_summary=projections["run_summary"],
            )
        )

        out_of_range = json.loads(json.dumps(report))
        duration_warning = next(
            warning
            for warning in out_of_range["warnings"]
            if warning["source_ref"] == {"pointer": "/spans/0/duration_ms"}
        )
        duration_warning["source_ref"]["pointer"] = "/spans/2/duration_ms"
        with self.assertRaises(BridgeError):
            verify_readback(
                raw,
                out_of_range,
                projections["run"],
                projections["spans"],
                run_summary=projections["run_summary"],
            )

        negative_findings = json.loads(json.dumps(report))
        negative_findings["stats"]["findings"] = -1
        with self.assertRaises(BridgeError):
            verify_readback(
                raw,
                negative_findings,
                projections["run"],
                projections["spans"],
                run_summary=projections["run_summary"],
            )

    def test_pinned_ingestion_projection_preserves_exact_approved_error_message(self):
        doc = canonical_doc()
        message = "Tool failed; details withheld"
        doc["spans"][0]["attributes"]["error.message"] = message
        raw = canonical_bytes(doc)
        self.assertTrue(validate_artifact(doc, raw))

        projections = pinned_agentscope_projections(raw)
        self.assertEqual(projections["spans"][0]["attributes"]["error.message"], message)
        self.assertTrue(
            verify_readback(
                raw,
                projections["report"],
                projections["run"],
                projections["spans"],
                run_summary=projections["run_summary"],
            )
        )

    def test_delivery_refuses_without_explicit_synthetic_only_acknowledgment(self):
        raw = json.dumps(source_spans(), sort_keys=True).encode()
        with patch.object(bridge, "_deliver_document") as send:
            with self.assertRaises(BridgeError):
                deliver_snapshot(
                    self.conn,
                    raw,
                    "copilot",
                    "http://127.0.0.1:8765",
                    "synthetic-token",
                )
        send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_allowlisted_values_reject_prose_secrets_and_absolute_paths(self):
        canaries = (
            ("agent.name", "User prompt contains SYNTHETIC-CANARY"),
            ("session.id", "ghp_SYNTHETIC_SECRET_CANARY123456789"),
            ("state.key", "C:/Users/Kyle/private.txt"),
            ("gen_ai.request.model", "api_key=SYNTHETIC-CANARY"),
        )
        for key, value in canaries:
            with self.subTest(key=key, value=value):
                doc = canonical_doc()
                doc["spans"][0]["attributes"][key] = value
                with self.assertRaises(BridgeError):
                    validate_artifact(doc, canonical_bytes(doc))

                spans = source_spans()
                spans[0]["attributes"][key] = value
                with patch.object(bridge, "_deliver_document") as send:
                    with self.assertRaises(BridgeError):
                        deliver_snapshot(
                            self.conn, json.dumps(spans).encode(), "copilot",
                            "http://127.0.0.1:8765", "synthetic-token",
                        )
                send.assert_not_called()
                self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_source_privacy_failure_happens_before_local_persistence(self):
        spans = source_spans()
        spans[0]["attributes"]["gen_ai.input.messages"] = "SYNTHETIC-CANARY"
        with self.assertRaises(BridgeError):
            deliver_snapshot(self.conn, json.dumps(spans).encode(), "copilot", "http://127.0.0.1:8765", "fake-token")
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_snapshot_is_bounded_and_immutable_after_read(self):
        from agentscope_importer.bridge import snapshot_source
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"snapshot")
            path = handle.name
        try:
            raw = snapshot_source(path, 32)
            with open(path, "wb") as handle:
                handle.write(b"changed")
            self.assertEqual(raw, b"snapshot")
            with self.assertRaises(BridgeError):
                snapshot_source(path, 3)
        finally:
            os.unlink(path)

    def test_local_receipt_and_delivery_share_one_snapshot_and_replay(self):
        raw = json.dumps(source_spans(), sort_keys=True).encode()
        with patch.object(bridge, "_deliver_document", return_value={
            "outcome": "partial", "quality": "partial", "already_imported": False,
            "verified": True, "upload_sha256": "a" * 64, "upload_bytes": 1,
        }) as send:
            first = deliver_snapshot(self.conn, raw, "copilot", "http://127.0.0.1:8765", "fake-token")
            second = deliver_snapshot(self.conn, raw, "copilot", "http://127.0.0.1:8765", "fake-token")
        self.assertTrue(first["verified"])
        self.assertEqual(first["source_snapshot_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(first["fleet_receipt"]["quality"], "complete")
        self.assertEqual(first["fleet_receipt"]["provenance"]["content_sha256"],
                         ledger.get_receipt(self.conn, first["fleet_receipt"]["import_id"])["provenance"]["content_sha256"])
        self.assertIn(first["run_mappings"][0]["fleet_run_id"], first["fleet_receipt"]["run_ids"])
        self.assertEqual(first["run_mappings"][0]["native_trace_id"], TRACE_A)
        expected_target = predict_target_ids(canonical_bytes(canonical_doc()))
        self.assertEqual(first["run_mappings"][0]["agentscope_import_id"], expected_target["import_id"])
        self.assertEqual(first["run_mappings"][0]["agentscope_run_id"], expected_target["runs"][TRACE_A])
        self.assertTrue(second["fleet_receipt"]["already_imported"])
        self.assertEqual(first["fleet_receipt"]["run_ids"], second["fleet_receipt"]["run_ids"])
        self.assertEqual(send.call_count, 2)

    def test_multi_run_delivery_accounts_for_independent_partial_failure(self):
        spans = source_spans("a" * 32, False) + source_spans("b" * 32, False)
        raw = json.dumps(spans, sort_keys=True).encode()
        def send(document, server, token):
            doc = json.loads(document.decode())
            if doc["run"]["native_id"] == "b" * 32:
                raise BridgeError("synthetic readback mismatch", "readback_failed")
            return {"outcome": "partial", "quality": "partial", "already_imported": False, "verified": True}
        with patch.object(bridge, "_deliver_document", side_effect=send) as mocked:
            result = deliver_snapshot(self.conn, raw, "copilot", "http://127.0.0.1:8765", "fake-token")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result["outcome"], "partial")
        self.assertFalse(result["verified"])
        self.assertEqual(len(result["deliveries"]), 1)
        self.assertEqual(result["failures"][0]["outcome"], "readback_failed")

    def test_multi_run_delivery_stops_on_auth_failure_and_marks_pending_runs(self):
        spans = (
            source_spans("a" * 32, False)
            + source_spans("b" * 32, False)
            + source_spans("c" * 32, False)
        )
        raw = json.dumps(spans, sort_keys=True).encode()
        attempted = []

        def send(document, server, token):
            trace_id = json.loads(document.decode())["run"]["native_id"]
            attempted.append(trace_id)
            if trace_id == "b" * 32:
                raise BridgeError("AgentScope authentication failed", "authentication_failed")
            return {
                "outcome": "delivered", "quality": "complete",
                "already_imported": False, "verified": True,
            }

        with patch.object(bridge, "_deliver_document", side_effect=send):
            result = deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765", "fake-token"
            )
        self.assertEqual(attempted, ["a" * 32, "b" * 32])
        self.assertEqual(len(result["deliveries"]), 1)
        self.assertEqual(result["failures"][0]["native_trace_id"], "b" * 32)
        self.assertEqual(result["failures"][0]["outcome"], "authentication_failed")
        self.assertEqual(len(result["unattempted"]), 1)
        self.assertEqual(result["unattempted"][0]["native_trace_id"], "c" * 32)
        self.assertEqual(result["unattempted"][0]["outcome"], "not_attempted")
        self.assertEqual(len(result["run_mappings"]), 3)

    def test_local_receipt_summary_preserves_warning_multiplicity(self):
        receipt = {
            "import_id": "imp_test", "quality": "warnings", "run_ids": [],
            "already_imported": False, "stats": {"warnings": 2},
            "warnings": [
                {"code": "duplicate_span"}, {"code": "duplicate_span"},
            ],
            "tokens_in": 0, "tokens_out": 0, "provenance": {},
        }
        summary = bridge._receipt_summary(receipt)
        self.assertEqual(summary["warning_count"], 2)
        self.assertEqual(summary["warning_codes"], ["duplicate_span", "duplicate_span"])

    def test_delivery_summary_distinguishes_unknown_and_explicit_zero_tokens(self):
        successful = {
            "outcome": "delivered", "quality": "complete",
            "already_imported": False, "verified": True,
        }
        cases = (
            (source_spans(with_llm=False), None, None),
            (source_spans(), 17, 9),
        )
        zero_spans = source_spans()
        zero_spans[1]["attributes"]["gen_ai.usage.input_tokens"] = 0
        zero_spans[1]["attributes"]["gen_ai.usage.output_tokens"] = 0
        cases += ((zero_spans, 0, 0),)
        for spans, expected_in, expected_out in cases:
            with self.subTest(expected_in=expected_in, expected_out=expected_out):
                raw = json.dumps(spans, sort_keys=True).encode()
                with patch.object(bridge, "_deliver_document", return_value=successful):
                    result = deliver_snapshot(
                        self.conn, raw, "copilot", "http://127.0.0.1:8765", "fake-token"
                    )
                self.assertEqual(result["fleet_receipt"]["tokens_in"], expected_in)
                self.assertEqual(result["fleet_receipt"]["tokens_out"], expected_out)

    def test_readback_verifies_all_semantic_fields_and_report(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        self.assertTrue(verify_readback(raw, report, run, spans, report, run_summary))

    def test_readback_rejects_conflicting_llm_and_tool_aliases(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        llm_rows = [dict(row) for row in spans]
        llm_rows[1]["input_tokens"] = 999
        with self.assertRaises(BridgeError):
            verify_readback(raw, report, run, llm_rows, run_summary=run_summary)

        tool_span = {
            "traceId": TRACE_A, "spanId": "3" * 16, "parentSpanId": ROOT,
            "name": "tool.exec", "startTimeUnixNano": BASE + 1_700_000_000,
            "endTimeUnixNano": BASE + 1_800_000_000,
            "attributes": {
                "gen_ai.tool.name": "web-search",
                "gen_ai.tool.call.id": "call-123",
            },
            "status": {"code": 1},
        }
        raw_with_tool = canonical_bytes(
            map_spans(source_spans() + [tool_span], "copilot")["documents"][0][1]
        )
        report = report_for(raw_with_tool)
        run, spans, run_summary = readback_for(raw_with_tool)
        tool_index = next(i for i, row in enumerate(spans) if row["native_id"] == "3" * 16)
        for field, value in (("tool_name", "different-tool"), ("call_id", "different-call")):
            with self.subTest(field=field):
                changed = [dict(row) for row in spans]
                changed[tool_index][field] = value
                with self.assertRaises(BridgeError):
                    verify_readback(
                        raw_with_tool, report, run, changed, run_summary=run_summary
                    )

    def test_readback_rejects_wrong_upload_even_with_same_native_ids(self):
        expected_raw = canonical_bytes(canonical_doc())
        substituted = canonical_doc()
        substituted["spans"][1]["llm"]["model"] = "other-model"
        actual_raw = canonical_bytes(substituted)
        actual_report = report_for(actual_raw)
        actual_run, actual_spans, actual_summary = readback_for(actual_raw)
        with self.assertRaises(BridgeError):
            verify_readback(expected_raw, actual_report, actual_run, actual_spans, run_summary=actual_summary)

    def test_readback_rejects_each_identity_parent_provenance_and_semantic_mismatch(self):
        raw = canonical_bytes(canonical_doc())
        for field, value in (
            ("id", "run_wrong"), ("status", "error"), ("quality", "complete"),
            ("graph_mode", "native"), ("capabilities", {}),
        ):
            with self.subTest(field=field):
                report = report_for(raw)
                run, spans, run_summary = readback_for(raw)
                run[field] = value
                with self.assertRaises(BridgeError):
                    verify_readback(raw, report, run, spans, run_summary=run_summary)

        for field, value in (
            ("id", "spn_wrong"), ("native_id", "f" * 16),
            ("parent_id", "spn_wrong"), ("seq", 7), ("kind", "other"),
            ("name", "user prompt"), ("status", "error"),
            ("started_us", 1), ("duration_us", 9),
            ("attributes", {"prompt": "SYNTHETIC-CANARY"}),
            ("tokens_in", 999), ("model", "other-model"),
            ("input_payload", "SYNTHETIC-CANARY"),
        ):
            with self.subTest(span_field=field):
                report = report_for(raw)
                run, spans, run_summary = readback_for(raw)
                spans[1][field] = value
                with self.assertRaises(BridgeError):
                    verify_readback(raw, report, run, spans, run_summary=run_summary)

    def test_readback_accepts_pinned_api_projection_and_optional_run_identity(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        self.assertNotIn("native_id", run)
        self.assertNotIn("run_id", spans[0])
        self.assertTrue(verify_readback(raw, report, run, spans, report, run_summary))
        run["native_id"] = "f" * 32
        with self.assertRaises(BridgeError):
            verify_readback(raw, report, run, spans, run_summary=run_summary)

    def test_readback_accepts_only_exact_safe_span_source_pointers(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        for index, span in enumerate(spans):
            span["source_ref"] = {"pointer": "/spans/%d" % index}
        self.assertTrue(verify_readback(raw, report, run, spans, run_summary=run_summary))

        for source_ref in (
            {"pointer": "/spans/99"},
            {"pointer": "/Users/Kyle/private.txt"},
            {"pointer": "/spans/0", "note": "SYNTHETIC-CANARY"},
        ):
            with self.subTest(source_ref=source_ref):
                changed = [dict(span) for span in spans]
                changed[0]["source_ref"] = source_ref
                with self.assertRaises(BridgeError):
                    verify_readback(raw, report, run, changed, run_summary=run_summary)

    def test_import_report_warning_count_and_source_reference_are_validated(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        report["warnings"] = [{
            "code": "graph.mode", "message": "safe warning",
            "source_ref": {"pointer": "/graph"},
        }]
        report["stats"]["warnings"] = 1
        run, spans, run_summary = readback_for(raw)
        self.assertTrue(verify_readback(raw, report, run, spans, run_summary=run_summary))

        report["stats"]["warnings"] = 0
        with self.assertRaises(BridgeError):
            verify_readback(raw, report, run, spans, run_summary=run_summary)

        report["stats"]["warnings"] = 1
        report["warnings"][0]["source_ref"] = {"pointer": "/Users/Kyle/private.txt"}
        with self.assertRaises(BridgeError):
            verify_readback(raw, report, run, spans, run_summary=run_summary)

    def test_run_summary_timestamp_and_identity_are_verified(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        for field, value in (("id", "run_wrong"), ("started_us", 1), ("ended_us", 1), ("graph_mode", "native")):
            with self.subTest(field=field):
                changed = dict(run_summary, **{field: value})
                with self.assertRaises(BridgeError):
                    verify_readback(raw, report, run, spans, run_summary=changed)

    def test_persisted_report_must_match_post_report_except_replay_flag(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        persisted = report_for(raw, already=False)
        run, spans, run_summary = readback_for(raw)
        replay = dict(report, already_imported=True)
        self.assertTrue(verify_readback(raw, replay, run, spans, persisted, run_summary))
        persisted["warnings"] = [{"code": "different", "message": "different"}]
        with self.assertRaises(BridgeError):
            verify_readback(raw, report, run, spans, persisted, run_summary)

    def test_event_bearing_snapshot_is_refused_before_local_persistence(self):
        spans = source_spans()
        spans[0]["events"] = [{"name": "event", "attributes": {"agent.name": "copilot"}}]
        with self.assertRaises(BridgeError):
            deliver_snapshot(self.conn, json.dumps(spans).encode(), "copilot", "http://127.0.0.1:8765", "fake-token")
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_otlp_flattened_resource_and_empty_scope_metadata_are_validated(self):
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [{
                    "key": "service.name", "value": {"stringValue": "synthetic-fleet"},
                }]},
                "scopeSpans": [{"scope": {}, "spans": [{
                    "traceId": TRACE_A, "spanId": ROOT, "name": "agent.run",
                    "startTimeUnixNano": str(BASE), "endTimeUnixNano": str(BASE + 1_000_000),
                    "attributes": [], "status": {"code": 1},
                }]}],
            }],
        }
        source_kind, _, documents = bridge._parse_source_snapshot(
            json.dumps(payload).encode(), "copilot"
        )
        self.assertEqual(source_kind, "otlp")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["run"]["native_id"], TRACE_A)

    def test_malformed_input_fails_closed_and_unsupported_input_never_transmits(self):
        malformed = source_spans()
        malformed[0]["status"] = {"code": 2, "message": ["SYNTHETIC-CANARY"]}
        with self.assertRaises(BridgeError):
            deliver_snapshot(self.conn, json.dumps(malformed).encode(), "copilot", "http://127.0.0.1:8765", "fake-token")
        invalid_canonical = canonical_doc()
        invalid_canonical["spans"][1]["llm"]["model"] = "\ud800"
        with self.assertRaises(BridgeError):
            deliver_snapshot(
                self.conn, json.dumps(invalid_canonical).encode(), "copilot",
                "http://127.0.0.1:8765", "fake-token",
            )
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

        for raw in (b"[" * 1500 + b"0" + b"]" * 1500, b'{"schema_version":"0.1","field":"canary"}'):
            with self.subTest(snapshot_len=len(raw)), patch.object(bridge, "_deliver_document") as send:
                result = deliver_snapshot(self.conn, raw, "copilot", "http://127.0.0.1:8765", "fake-token")
                self.assertEqual(result["outcome"], "rejected")
                self.assertFalse(result["verified"])
                send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 2)

    def test_deliver_document_uses_actual_api_projection_shapes(self):
        raw = canonical_bytes(canonical_doc())
        report = report_for(raw)
        persisted = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        responses = [(201, report), (200, persisted), (200, run), (200, [run_summary]), (200, spans)]
        with patch.object(bridge, "request_json", side_effect=responses) as request:
            result = bridge._deliver_document(raw, "http://127.0.0.1:8765", "synthetic-token")
        self.assertTrue(result["verified"])
        self.assertTrue(result["verification_limits"])
        self.assertEqual(request.call_count, 5)
        self.assertTrue(request.call_args_list[0].kwargs["body"].find(raw) >= 0)

    def test_readback_rejects_wrong_import_run_span_ids_and_missing_spans(self):
        raw = canonical_bytes(canonical_doc())
        ids = predict_target_ids(raw)
        bad_report = report_for(raw)
        bad_report["import_id"] = "imp_wrong"
        run, spans, run_summary = readback_for(raw)
        with self.assertRaises(BridgeError):
            verify_readback(raw, bad_report, run, spans, run_summary=run_summary)
        bad_report = report_for(raw)
        bad_report["run_ids"] = ["run_wrong"]
        with self.assertRaises(BridgeError):
            verify_readback(raw, bad_report, run, spans, run_summary=run_summary)
        bad_report = report_for(raw)
        bad_report["stats"]["spans"] += 1
        with self.assertRaises(BridgeError):
            verify_readback(raw, bad_report, run, spans, run_summary=run_summary)
        spans[0]["id"] = "spn_wrong"
        with self.assertRaises(BridgeError):
            verify_readback(raw, report_for(raw), run, spans, run_summary=run_summary)
        with self.assertRaises(BridgeError):
            verify_readback(raw, report_for(raw), run, spans[:1], run_summary=run_summary)
        self.assertIn(ids["import_id"], report_for(raw)["import_id"])

    def test_span_pagination_fetches_plain_list_pages_at_api_boundaries(self):
        for total in (50, 51, 200, 201):
            with self.subTest(total=total):
                records = [
                    {"id": "spn_%03d" % index, "native_id": "%016x" % index}
                    for index in range(total)
                ]
                requests = []

                def get_page(url, _token):
                    query = bridge.urllib.parse.parse_qs(bridge.urllib.parse.urlsplit(url).query)
                    limit = int(query.get("limit", ["50"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                    requests.append((limit, offset))
                    return 200, records[offset:offset + limit]

                with patch.object(bridge, "request_json", side_effect=get_page):
                    result = bridge._read_span_pages(
                        "http://127.0.0.1:8765", "run_id", "fake-token"
                    )
                self.assertEqual(len(result), total)
                self.assertTrue(requests)
                self.assertEqual(requests[0], (200, 0))
                self.assertEqual(len(requests), 2 if total >= 200 else 1)
                if total >= 200:
                    self.assertEqual(requests[1], (200, 200))

    def test_span_pagination_rejects_repeated_full_page_without_progress(self):
        page = [
            {"id": "spn_%03d" % index, "native_id": "%016x" % index}
            for index in range(200)
        ]
        with patch.object(bridge, "request_json", side_effect=[(200, page), (200, page)]) as get:
            with self.assertRaises(BridgeError):
                bridge._read_span_pages("http://127.0.0.1:8765", "run_id", "fake-token")
        self.assertEqual(get.call_count, 2)

    def test_run_summary_readback_paginates_with_bounded_offsets(self):
        first_page = [{"id": "run_%03d" % index} for index in range(bridge.RUN_PAGE_SIZE)]
        target = {"id": "run_target"}
        with patch.object(bridge, "request_json", side_effect=[(200, first_page), (200, [target])]) as get:
            result = bridge._read_run_summary("http://127.0.0.1:8765", "run_target", "fake-token")
        self.assertEqual(result, target)
        self.assertEqual(get.call_count, 2)
        self.assertIn("offset=200", get.call_args_list[1].args[0])

    def test_multipart_contains_exact_bytes_in_single_file_field(self):
        raw = canonical_bytes(canonical_doc())
        boundary, body = bridge._multipart(raw)
        self.assertIn(b'name="file"; filename="trace.json"', body)
        self.assertIn(raw, body)
        self.assertTrue(body.endswith(("\r\n--%s--\r\n" % boundary).encode()))
        self.assertEqual(body.count(raw), 1)

    def test_transport_does_not_retry_4xx_or_leak_token(self):
        class FakeOpener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                self.request = request
                raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)
        opener = FakeOpener()
        with patch.object(bridge, "_opener", return_value=opener):
            with self.assertRaises(BridgeError) as caught:
                bridge.request_json("http://127.0.0.1:8765/api/v1/runs", "synthetic-token")
        self.assertEqual(opener.calls, 1)
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertNotIn("synthetic-token", opener.request.full_url)
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer synthetic-token")

    def test_transport_retries_only_transient_server_errors_three_times(self):
        class FakeOpener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        opener = FakeOpener()
        with patch.object(bridge, "_opener", return_value=opener):
            with self.assertRaises(BridgeError) as caught:
                bridge.request_json("http://127.0.0.1:8765/api/v1/runs", "synthetic-token")
        self.assertEqual(opener.calls, 3)
        self.assertEqual(caught.exception.outcome, "server_unavailable")

    def test_transport_rejects_oversized_response_body(self):
        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                return b"x" * size
        class FakeOpener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                return FakeResponse()
        opener = FakeOpener()
        with patch.object(bridge, "MAX_RESPONSE_BYTES", 8), patch.object(bridge, "_opener", return_value=opener):
            with self.assertRaises(BridgeError) as caught:
                bridge.request_json("http://127.0.0.1:8765/api/v1/runs", "synthetic-token")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(caught.exception.outcome, "readback_failed")

    def test_opener_disables_proxies_and_redirects(self):
        with patch.object(bridge.urllib.request, "getproxies_environment", return_value={
            "http": "http://127.0.0.2:9999",
        }):
            opener = bridge._opener()
        proxy_handlers = [handler for handler in opener.handlers if isinstance(handler, bridge.urllib.request.ProxyHandler)]
        redirect_handlers = [handler for handler in opener.handlers if isinstance(handler, bridge._NoRedirect)]
        # urllib omits an empty ProxyHandler; this prevents environment proxy defaults.
        self.assertFalse(proxy_handlers)
        self.assertTrue(redirect_handlers)

    def test_redirect_response_is_rejected_without_following_or_retry(self):
        class FakeOpener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                headers = Message()
                headers["Location"] = "http://example.com/"
                raise urllib.error.HTTPError(
                    request.full_url, 302, "redirect", headers, None
                )
        opener = FakeOpener()
        with patch.object(bridge, "_opener", return_value=opener):
            with self.assertRaises(BridgeError) as caught:
                bridge.request_json("http://127.0.0.1:8765/api/v1/runs", "synthetic-token")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(caught.exception.outcome, "rejected")

    def test_duplicate_receipt_preserves_run_ids_and_graph_derivation(self):
        span = {"traceId": "t", "spanId": "s", "name": "agent.run",
                "startTimeUnixNano": 1, "endTimeUnixNano": 2}
        first = import_spans(self.conn, [span], "test")
        second = import_spans(self.conn, [span], "test")
        self.assertEqual(first["run_ids"], second["run_ids"])
        self.assertEqual(second["provenance"].get("graph_derivation"), "tree-from-recorded-parentage")

    def test_out_of_range_timestamp_is_unknown_not_exception(self):
        from agentscope_importer.mapper import ns_to_iso
        self.assertIsNone(ns_to_iso(10**40))


if __name__ == "__main__":
    unittest.main()
