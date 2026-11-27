"""Live-mode delivery tests for the AgentScope bridge (M7-B adapter).

Covers the live acknowledgment path: Copilot exporter span names are
renamed to the canonical vocabulary, unapproved attributes are dropped
and counted, and real observations are never delivered with a
synthetic-only acknowledgment. Claude Code observations are accepted
live only as the claude_code_plugin adapter's JSON span array (no
normalization, strict agent.name/observation.source checks); every
other Claude Code shape is refused before any import or delivery.
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
from claude_code_plugin import mapping as cc_mapping  # noqa: E402
from claude_code_plugin import _vocab  # noqa: E402
from tests.test_claude_code_plugin import _s8_lines  # noqa: E402
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


def copilot_otlp(spans):
    """Wrap flat Copilot exporter spans in one OTLP/JSON resourceSpans payload."""
    def attrs(values):
        return [
            {"key": key, "value": {"intValue": str(value)} if isinstance(value, int) else {"stringValue": value}}
            for key, value in values.items()
        ]
    return {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"scope": {}, "spans": [
        dict(
            span, attributes=attrs(span["attributes"]),
            startTimeUnixNano=str(span["startTimeUnixNano"]),
            endTimeUnixNano=str(span["endTimeUnixNano"]),
        )
        for span in spans
    ]}]}]}


def live_canonical_doc(trace=TRACE_A):
    kind, _, documents, _ = bridge._parse_source_snapshot(
        json.dumps(copilot_spans(trace)).encode(), "copilot", live=True
    )
    assert kind == "spans"
    return documents[0]


def claude_code_spans():
    spans, _counters = cc_mapping.convert_lines(_s8_lines())
    return spans


def claude_code_raw():
    return cc_mapping.serialize(claude_code_spans())


CC_RESULT = {
    "outcome": "partial", "quality": "partial",
    "already_imported": False, "verified": True,
}


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


class LiveClaudeCodeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def assert_refused(self, raw, producer="claude-code", message=None):
        with patch.object(bridge, "_deliver_document") as send:
            with self.assertRaises(BridgeError) as caught:
                deliver_snapshot(
                    self.conn, raw, producer, "http://127.0.0.1:8765",
                    "fake-token", live=True,
                )
        send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)
        if message is not None:
            self.assertIn(message, str(caught.exception))

    def test_live_claude_code_delivery_accepts_adapter_spans(self):
        raw = claude_code_raw()
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            result = deliver_snapshot(
                self.conn, raw, "claude-code", "http://127.0.0.1:8765",
                "fake-token", live=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.kwargs["live"])
        self.assertFalse(send.call_args.kwargs["synthetic_only"])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["live_normalization"]["renamed_spans"], 0)
        self.assertEqual(result["live_normalization"]["dropped_attributes"], 0)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)

    def test_live_claude_code_ledger_imports_exactly_delivered_bytes(self):
        raw = claude_code_raw()
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT), \
                patch.object(bridge, "import_bytes", wraps=bridge.import_bytes) as imported:
            deliver_snapshot(
                self.conn, raw, "claude-code", "http://127.0.0.1:8765",
                "fake-token", live=True,
            )
        self.assertEqual(imported.call_count, 1)
        self.assertEqual(imported.call_args.args[1], claude_code_raw())
        self.assertEqual(imported.call_args.args[1], raw)

    def test_claude_code_parse_is_not_copilot_normalized(self):
        raw = claude_code_raw()
        kind, parsed, documents, normalization = bridge._parse_source_snapshot(
            raw, "claude-code", live=False
        )
        self.assertEqual(kind, "spans")
        self.assertEqual(
            [span["name"] for span in parsed],
            [span["name"] for span in claude_code_spans()],
        )
        self.assertEqual(normalization["renamed_spans"], 0)
        self.assertEqual(normalization["dropped_attributes"], 0)
        self.assertTrue(documents)
        for document in documents:
            self.assertEqual(document["run"]["metadata"]["producer"], "claude-code")

    def test_refuses_raw_receiver_jsonl(self):
        raw = "\n".join(_s8_lines()).encode("utf-8")
        self.assert_refused(raw)

    def test_refuses_otlp_payload(self):
        payload = json.loads(_s8_lines()[0])
        self.assertIn("resourceSpans", payload)
        self.assert_refused(json.dumps(payload).encode("utf-8"))

    def test_refuses_canonical_document(self):
        doc = bridge._parse_source_snapshot(claude_code_raw(), "claude-code")[2][0]
        # Pins the input-kind rule itself: the marker loop would also
        # refuse a dict, but with a message that does not point at the
        # adapter.
        self.assert_refused(canonical_bytes(doc), message="python -m claude_code_plugin convert")

    def test_refuses_copilot_span_name_even_with_adapter_markers(self):
        # Live Claude Code must not run Copilot normalization: a Copilot
        # exporter name would be renamed (and accepted) if it did.
        spans = claude_code_spans()
        spans[0]["name"] = "invoke_agent"
        self.assert_refused(cc_mapping.serialize(spans), message="unapproved span name")

    def test_live_copilot_refuses_canonical_document_of_other_producer(self):
        doc = bridge._parse_source_snapshot(
            json.dumps(copilot_spans()).encode(), "hermes", live=True
        )[2][0]
        self.assert_refused(
            canonical_bytes(doc), producer="copilot",
            message="document producer must match the requested producer",
        )

    def test_refuses_span_missing_agent_name(self):
        spans = claude_code_spans()
        spans[0]["attributes"].pop("agent.name")
        self.assert_refused(cc_mapping.serialize(spans))

    def test_refuses_span_missing_observation_source(self):
        spans = claude_code_spans()
        spans[0]["attributes"].pop("observation.source")
        self.assert_refused(cc_mapping.serialize(spans))

    def test_refuses_span_with_wrong_agent_name(self):
        spans = claude_code_spans()
        spans[0]["attributes"]["agent.name"] = "copilot"
        self.assert_refused(cc_mapping.serialize(spans))

    def test_refuses_copilot_shaped_spans_as_claude_code(self):
        self.assert_refused(json.dumps(copilot_spans(), sort_keys=True).encode())

    def test_refuses_empty_span_array(self):
        self.assert_refused(b"[]")

    def test_other_producers_still_refused_live(self):
        raw = claude_code_raw()
        for producer in ("hermes", "fleet-tracing"):
            with self.subTest(producer=producer):
                self.assert_refused(raw, producer=producer)

    def test_live_claude_code_document_accepted_by_real_agentscope_pipeline(self):
        raw = canonical_bytes(bridge._parse_source_snapshot(claude_code_raw(), "claude-code")[2][0])
        projections = pinned_agentscope_projections(raw)
        report = projections["report"]
        self.assertEqual(report["import_id"], bridge.predict_target_ids(raw)["import_id"])
        self.assertIn(report["quality"], ("partial", "complete"))


class LiveAcknowledgmentTypeTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def assert_ack_type_refused(self, raw, producer, **flags):
        with patch.object(bridge, "_deliver_document") as send, \
                patch.object(bridge, "import_bytes", wraps=bridge.import_bytes) as imported:
            with self.assertRaises(BridgeError) as caught:
                deliver_snapshot(
                    self.conn, raw, producer, "http://127.0.0.1:8765",
                    "fake-token", **flags,
                )
        self.assertIn("must be True or False", str(caught.exception))
        imported.assert_not_called()
        send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_non_boolean_acknowledgments_refused_before_import(self):
        copilot_raw = json.dumps(copilot_spans(), sort_keys=True).encode()
        cases = [
            ("claude-code live=1", claude_code_raw(), "claude-code", {"live": 1}),
            ("claude-code live=yes", claude_code_raw(), "claude-code", {"live": "yes"}),
            ("claude-code live=1.0", claude_code_raw(), "claude-code", {"live": 1.0}),
            ("copilot live=1", copilot_raw, "copilot", {"live": 1}),
            ("claude-code synthetic_only=1", claude_code_raw(), "claude-code",
             {"synthetic_only": 1}),
            ("copilot synthetic_only=true", copilot_raw, "copilot",
             {"synthetic_only": "true"}),
            ("claude-code live=None", claude_code_raw(), "claude-code", {"live": None}),
        ]
        for label, raw, producer, flags in cases:
            with self.subTest(case=label):
                self.assert_ack_type_refused(raw, producer, **flags)

    def test_refusal_does_not_echo_acknowledgment_value(self):
        with patch.object(bridge, "_deliver_document") as send:
            with self.assertRaises(BridgeError) as caught:
                deliver_snapshot(
                    self.conn, claude_code_raw(), "claude-code",
                    "http://127.0.0.1:8765", "fake-token",
                    live="CANARY-ACK-7f3a",
                )
        self.assertNotIn("CANARY-ACK-7f3a", str(caught.exception))
        send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_live_true_still_accepted_for_claude_code(self):
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            result = deliver_snapshot(
                self.conn, claude_code_raw(), "claude-code",
                "http://127.0.0.1:8765", "fake-token", live=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["mode"], "live")
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)


class LiveDroppedRunTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def assert_dropped_refused(self, raw, producer):
        with patch.object(bridge, "_deliver_document") as send, \
                patch.object(bridge, "import_bytes", wraps=bridge.import_bytes) as imported:
            with self.assertRaises(BridgeError) as caught:
                deliver_snapshot(
                    self.conn, raw, producer, "http://127.0.0.1:8765",
                    "fake-token", live=True,
                )
        self.assertIn("refused before import", str(caught.exception))
        imported.assert_not_called()
        send.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)
        return str(caught.exception)

    def test_copilot_duplicate_span_id_refused_before_import(self):
        spans = copilot_spans()
        spans[2]["spanId"] = CHILD
        self.assert_dropped_refused(json.dumps(spans, sort_keys=True).encode(), "copilot")

    def test_copilot_parent_cycle_refused_before_import(self):
        spans = copilot_spans()
        spans[0]["parentSpanId"] = CHILD
        self.assert_dropped_refused(json.dumps(spans, sort_keys=True).encode(), "copilot")

    def test_claude_code_duplicate_span_id_refused_before_import(self):
        spans = claude_code_spans()
        # hook.exec (...60008) takes the spanId of llm.call (...60002) in the same trace.
        spans[2]["spanId"] = "a1b2c3d4e5f60002"
        self.assert_dropped_refused(cc_mapping.serialize(spans), "claude-code")

    def test_claude_code_parent_cycle_refused_before_import(self):
        spans = claude_code_spans()
        # agent.run (...60001) and llm.call (...60002) become each other's parent.
        spans[0]["parentSpanId"] = "a1b2c3d4e5f60002"
        self.assert_dropped_refused(cc_mapping.serialize(spans), "claude-code")

    def test_refusal_does_not_echo_span_id(self):
        spans = claude_code_spans()
        spans[2]["spanId"] = "a1b2c3d4e5f60002"
        message = self.assert_dropped_refused(cc_mapping.serialize(spans), "claude-code")
        self.assertNotIn("a1b2c3d4e5f60002", message)

    def test_copilot_otlp_duplicate_span_id_refused_before_import(self):
        spans = copilot_spans()
        spans[2]["spanId"] = CHILD
        self.assert_dropped_refused(json.dumps(copilot_otlp(spans)).encode(), "copilot")

    def test_copilot_jsonl_duplicate_span_id_refused_before_import(self):
        spans = copilot_spans()
        spans[2]["spanId"] = CHILD
        raw = "\n".join(json.dumps(span, sort_keys=True) for span in spans).encode()
        self.assert_dropped_refused(raw, "copilot")

    def test_synthetic_delivery_of_duplicate_span_id_keeps_existing_behaviour(self):
        # The strict check is live-only: synthetic delivery still imports
        # (an invalid ledger row) and returns "rejected" without sending.
        spans = copilot_spans()
        spans[2]["spanId"] = CHILD
        for span, name in zip(spans, ("agent.run", "llm.call", "tool.exec")):
            span["name"] = name
        spans[0]["attributes"].pop("gen_ai.system")
        raw = json.dumps(spans, sort_keys=True).encode()
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            result = deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765",
                "fake-token", synthetic_only=True,
            )
        send.assert_not_called()
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)

    def test_live_claude_code_clean_snapshot_still_delivered(self):
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            result = deliver_snapshot(
                self.conn, claude_code_raw(), "claude-code",
                "http://127.0.0.1:8765", "fake-token", live=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["mode"], "live")
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)


class LiveClaudeCodeShapeTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def assert_refused(self, raw, producer="claude-code", message=None):
        with patch.object(bridge, "_deliver_document") as send, \
                patch.object(bridge, "import_bytes", wraps=bridge.import_bytes) as imported:
            with self.assertRaises(BridgeError) as caught:
                deliver_snapshot(
                    self.conn, raw, producer, "http://127.0.0.1:8765",
                    "fake-token", live=True,
                )
        send.assert_not_called()
        imported.assert_not_called()
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)
        if message is not None:
            self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def test_extra_span_fields_refused(self):
        extras = [
            ("events", [{"name": "x", "attributes": {}}]),
            ("resource", {}),
            ("scope", {}),
            ("kind", 1),
        ]
        for field, value in extras:
            with self.subTest(field=field):
                spans = claude_code_spans()
                spans[1][field] = value
                # events is refused earlier by the general validator (its own
                # message), so only the refusal itself is asserted for it.
                message = None if field == "events" else "field the adapter never emits"
                self.assert_refused(cc_mapping.serialize(spans), message=message)

    def test_span_names_outside_adapter_refused(self):
        for name in ("state.write", "agent.handoff", "subagent.spawn", "approval.request"):
            with self.subTest(name=name):
                spans = claude_code_spans()
                spans[1]["name"] = name
                self.assert_refused(
                    cc_mapping.serialize(spans),
                    message="span name is not one the adapter emits",
                )

    def test_attributes_outside_adapter_refused(self):
        for key, value in (
            ("copilot.agent.type", "x"),
            ("hermes.attempt", 1),
            ("approval.kind", "x"),
            ("synthetic", True),
        ):
            with self.subTest(key=key):
                spans = claude_code_spans()
                spans[1]["attributes"][key] = value
                self.assert_refused(
                    cc_mapping.serialize(spans),
                    message="attribute the adapter never emits",
                )

    def test_refusal_does_not_echo_key_or_value(self):
        spans = claude_code_spans()
        spans[1]["attributes"]["copilot.agent.type"] = "CANARY-VAL-91c2"
        message = self.assert_refused(
            cc_mapping.serialize(spans),
            message="attribute the adapter never emits",
        )
        self.assertNotIn("copilot.agent.type", message)
        self.assertNotIn("CANARY-VAL-91c2", message)

    def test_live_copilot_synthetic_span_attribute_refused(self):
        spans = copilot_spans()
        spans[1]["attributes"]["synthetic"] = "true"
        self.assert_refused(
            json.dumps(spans, sort_keys=True).encode(),
            producer="copilot", message="synthetic marker",
        )

    def test_live_copilot_synthetic_span_attribute_refused_in_otlp(self):
        spans = copilot_spans()
        spans[1]["attributes"]["synthetic"] = "true"
        self.assert_refused(
            json.dumps(copilot_otlp(spans)).encode(),
            producer="copilot", message="synthetic marker",
        )

    def test_live_copilot_synthetic_false_refused(self):
        spans = copilot_spans()
        spans[1]["attributes"]["synthetic"] = "false"
        self.assert_refused(
            json.dumps(spans, sort_keys=True).encode(),
            producer="copilot", message="synthetic marker",
        )

    def test_live_copilot_synthetic_resource_attribute_not_carried(self):
        # Live OTLP normalization keeps only service.name and service.version
        # on the resource, so a resource-level synthetic marker never reaches
        # a canonical document and the delivery is not refused for it.
        payload = copilot_otlp(copilot_spans())
        payload["resourceSpans"][0]["resource"]["attributes"] = [
            {"key": "synthetic", "value": {"stringValue": "true"}},
        ]
        raw = json.dumps(payload).encode()
        _kind, _parsed, documents, _norm = bridge._parse_source_snapshot(
            raw, "copilot", live=True
        )
        for document in documents:
            self.assertNotIn("synthetic", json.dumps(document, sort_keys=True))
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765",
                "fake-token", live=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)

    def test_synthetic_only_delivery_still_accepts_synthetic_marker(self):
        spans = copilot_spans()
        for span, name in zip(spans, ("agent.run", "llm.call", "tool.exec")):
            span["name"] = name
        spans[0]["attributes"].pop("gen_ai.system")
        spans[1]["attributes"]["synthetic"] = "true"
        raw = json.dumps(spans, sort_keys=True).encode()
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            result = deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765",
                "fake-token", synthetic_only=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["mode"], "synthetic")
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)

    def test_live_claude_code_clean_snapshot_delivered_once(self):
        with patch.object(bridge, "_deliver_document", return_value=CC_RESULT) as send:
            result = deliver_snapshot(
                self.conn, claude_code_raw(), "claude-code",
                "http://127.0.0.1:8765", "fake-token", live=True,
            )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["mode"], "live")
        self.assertEqual(ledger.counts(self.conn)["imports"], 1)

    def test_live_constants_pinned_to_adapter(self):
        self.assertEqual(
            bridge.LIVE_CLAUDE_CODE_SPAN_NAMES,
            frozenset(_vocab.SPAN_TYPE_TO_V1.values()),
        )
        self.assertEqual(
            bridge.LIVE_CLAUDE_CODE_ATTRIBUTE_KEYS,
            frozenset(_vocab.OUTPUT_ATTRIBUTE_KEYS),
        )

    def test_adapter_output_within_live_constants(self):
        for span in claude_code_spans():
            with self.subTest(name=span.get("name")):
                self.assertLessEqual(set(span), bridge.LIVE_CLAUDE_CODE_SPAN_KEYS)
                self.assertLessEqual(
                    set(span["attributes"]), bridge.LIVE_CLAUDE_CODE_ATTRIBUTE_KEYS
                )


if __name__ == "__main__":
    unittest.main()
