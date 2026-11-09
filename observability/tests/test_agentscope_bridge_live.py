"""Live-mode delivery tests for the AgentScope bridge (M7-B adapter).

Covers the live acknowledgment path: Copilot exporter span names are
renamed to the canonical vocabulary, unapproved attributes are dropped
and counted, and real observations are never delivered with a
synthetic-only acknowledgment.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope_importer import bridge, ledger  # noqa: E402
from agentscope_importer.bridge import BridgeError, deliver_snapshot  # noqa: E402
from agentscope_importer.mapper import canonical_bytes  # noqa: E402
from tests.test_agentscope_bridge import (  # noqa: E402
    BASE, CHILD, ROOT, TRACE_A, pinned_agentscope_projections, readback_for,
    report_for,
)


def copilot_spans(trace=TRACE_A):
    """Raw Copilot exporter spans: invoke_agent root, chat, execute_tool.

    Carries one unapproved attribute (gen_ai.system) that live mode must
    drop and count, and real token counts (120 in / 340 out).
    """
    return [
        {
            "traceId": trace, "spanId": ROOT, "name": "invoke_agent",
            "startTimeUnixNano": BASE, "endTimeUnixNano": BASE + 2_000_000_000,
            "attributes": {"agent.name": "copilot", "gen_ai.system": "copilot-chat"},
            "status": {"code": 1},
        },
        {
            "traceId": trace, "spanId": CHILD, "parentSpanId": ROOT,
            "name": "chat",
            "startTimeUnixNano": BASE + 500_000_000,
            "endTimeUnixNano": BASE + 1_500_000_000,
            "attributes": {
                "gen_ai.request.model": "gpt-5",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 340,
            },
            "status": {"code": 1},
        },
        {
            "traceId": trace, "spanId": "3" * 16, "parentSpanId": ROOT,
            "name": "execute_tool",
            "startTimeUnixNano": BASE + 1_600_000_000,
            "endTimeUnixNano": BASE + 1_900_000_000,
            "attributes": {
                "gen_ai.tool.name": "web-search",
                "gen_ai.tool.call.id": "call-123",
            },
            "status": {"code": 1},
        },
    ]


def live_canonical_doc(trace=TRACE_A):
    kind, _, documents, _ = bridge._parse_source_snapshot(
        json.dumps(copilot_spans(trace)).encode(), "copilot", live=True
    )
    assert kind == "spans"
    return documents[0]


class LiveBridgeTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_live_renames_copilot_names_and_counts_sanitized_attributes(self):
        kind, _, documents, normalization = bridge._parse_source_snapshot(
            json.dumps(copilot_spans()).encode(), "copilot", live=True
        )
        self.assertEqual(kind, "spans")
        self.assertEqual(normalization["renamed_spans"], 3)
        self.assertEqual(normalization["dropped_attributes"], 1)
        doc = documents[0]
        self.assertEqual(
            [span["name"] for span in doc["spans"]],
            ["agent.run", "llm.call", "tool.exec"],
        )
        self.assertNotIn("gen_ai.system", doc["spans"][0]["attributes"])
        self.assertEqual(doc["spans"][1]["llm"]["input_tokens"], 120)
        self.assertEqual(doc["spans"][1]["llm"]["output_tokens"], 340)
        self.assertEqual(doc["spans"][2]["tool"]["name"], "web-search")

    def test_live_delivery_accepts_copilot_spans(self):
        raw = json.dumps(copilot_spans(), sort_keys=True).encode()
        with patch.object(bridge, "_deliver_document", return_value={
            "outcome": "partial", "quality": "partial",
            "already_imported": False, "verified": True,
        }) as send:
            result = deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765",
                "fake-token", live=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.kwargs["live"])
        self.assertFalse(send.call_args.kwargs["synthetic_only"])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["live_normalization"]["renamed_spans"], 3)
        self.assertEqual(result["live_normalization"]["dropped_attributes"], 1)
        self.assertEqual(result["outcome"], "partial")
        self.assertTrue(result["verified"])

    def test_live_refuses_without_acknowledgment(self):
        raw = json.dumps(copilot_spans(), sort_keys=True).encode()
        with patch.object(bridge, "_deliver_document") as send:
            with self.assertRaises(BridgeError):
                deliver_snapshot(
                    self.conn, raw, "copilot", "http://127.0.0.1:8765",
                    "fake-token",
                )
        send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_live_refuses_conflicting_acknowledgments(self):
        raw = json.dumps(copilot_spans(), sort_keys=True).encode()
        with self.assertRaises(BridgeError):
            deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765",
                "fake-token", synthetic_only=True, live=True,
            )
        with self.assertRaises(BridgeError):
            bridge._deliver_document(
                raw, "http://127.0.0.1:8765", "fake-token",
                synthetic_only=True, live=True,
            )

    def test_live_refuses_unknown_copilot_span_name(self):
        spans = copilot_spans()
        spans[1]["name"] = "do_something_weird"
        with self.assertRaises(BridgeError):
            bridge._parse_source_snapshot(
                json.dumps(spans).encode(), "copilot", live=True
            )

    def test_live_still_refuses_unsafe_attribute_values(self):
        spans = copilot_spans()
        spans[1]["attributes"]["gen_ai.request.model"] = "sk-secret-model"
        with self.assertRaises(BridgeError):
            bridge._parse_source_snapshot(
                json.dumps(spans).encode(), "copilot", live=True
            )

    def test_live_requires_copilot_producer(self):
        raw = json.dumps(copilot_spans(), sort_keys=True).encode()
        with self.assertRaises(BridgeError):
            deliver_snapshot(
                self.conn, raw, "hermes", "http://127.0.0.1:8765",
                "fake-token", live=True,
            )

    def test_live_deliver_document_uses_actual_api_projection_shapes(self):
        raw = canonical_bytes(live_canonical_doc())
        report = report_for(raw)
        persisted = report_for(raw)
        run, spans, run_summary = readback_for(raw)
        responses = [(201, report), (200, persisted), (200, run), (200, [run_summary]), (200, spans)]
        with patch.object(bridge, "request_json", side_effect=responses) as request:
            result = bridge._deliver_document(
                raw, "http://127.0.0.1:8765", "live-token", live=True
            )
        self.assertTrue(result["verified"])
        self.assertEqual(request.call_count, 5)
        self.assertTrue(request.call_args_list[0].kwargs["body"].find(raw) >= 0)

    def test_live_document_accepted_by_real_agentscope_pipeline(self):
        raw = canonical_bytes(live_canonical_doc())
        projections = pinned_agentscope_projections(raw)
        report = projections["report"]
        self.assertEqual(report["import_id"], bridge.predict_target_ids(raw)["import_id"])
        self.assertIn(report["quality"], ("partial", "complete"))
        self.assertEqual(len(projections["spans"]), 3)
        self.assertEqual(
            projections["run"]["id"],
            bridge.predict_target_ids(raw)["runs"][TRACE_A],
        )


if __name__ == "__main__":
    unittest.main()
