"""Real-exporter compatibility tests for the AgentScope bridge (M7-B).

Covers the genuine GitHub Copilot OpenTelemetry file-exporter shape:
JSONL of mixed span + metric + log records, [seconds, nanoseconds]
time pairs, model-suffixed chat and embeddings span names
("chat <model>", "embeddings <model>"), SDK fields
(instrumentationScope, traceFlags, dropped*Count, resource, links),
failed spans carrying exception events, and non-error spans with
status code 0 (run2: usage tokens, finish reasons, log records).

All fixtures here are SYNTHETIC reproductions of the observed shape:
fake hex trace/span ids, synthetic session/conversation values,
synthetic error strings, and a sentinel marker in place of any stack
trace. No real telemetry, session id, server address, local path, or
stack trace appears in this file.

Note on encodings: a one-record JSONL input parses as a single JSON
value, so the bridge reports it "unsupported" (pre-existing detection
order, same as the M5 importer). Single-span cases below therefore
use the JSON list form; multi-record cases use JSONL.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope_importer import bridge, ledger  # noqa: E402
from agentscope_importer.bridge import BridgeError, deliver_snapshot  # noqa: E402
from agentscope_importer.importer import import_bytes  # noqa: E402
from agentscope_importer.mapper import canonical_bytes  # noqa: E402
from tests.test_agentscope_bridge_live import live_canonical_doc  # noqa: E402

TRACE_1 = "aa" * 16
TRACE_2 = "cc" * 16
SPAN_1 = "bb" * 8
SPAN_2 = "dd" * 8
SEC = 1700000000
MODEL = "gemini-3.8-flash-high"
# Sentinel proving the exception.stacktrace field is dropped; it is not
# a real stack trace and must never appear in any canonical document.
STACKTRACE_SENTINEL = "SENTINEL-STACKTRACE-DO-NOT-IMPORT"


def exporter_span(trace=TRACE_1, span=SPAN_1, name="chat " + MODEL,
                  start=(SEC, 178000000), end=(SEC + 4, 178869400),
                  duration=(4, 869400), status_code=2,
                  status_message="synthetic exporter failure",
                  error_type="Error", model=MODEL, operation="chat",
                  with_event=True, extra_attrs=None, **overrides):
    """One synthetic real-exporter span record (observed shape)."""
    attrs = {
        "gen_ai.operation.name": operation,
        "gen_ai.provider.name": "github",
        "gen_ai.request.model": model,
        "gen_ai.conversation.id": "synthetic-conversation",
        "gen_ai.request.max_tokens": 8192,
        "gen_ai.request.temperature": 0.1,
        "gen_ai.request.top_p": 1,
        "copilot_chat.request.max_prompt_tokens": 1048576,
        "copilot_chat.session_id": "synthetic-session",
        "gen_ai.agent.name": "copilotLanguageModelWrapper",
        "copilot_chat.request.options": '{"stream":true}',
        "copilot_chat.request.shape": '{"api":"messages","messageCount":3}',
        "error.type": error_type,
        "gen_ai.response.finish_reasons": ["error"],
    }
    if extra_attrs:
        attrs.update(extra_attrs)
    record = {
        "traceId": trace,
        "spanId": span,
        "name": name,
        "kind": 2,
        "startTime": list(start),
        "endTime": list(end),
        "duration": list(duration),
        "ended": True,
        "traceFlags": 1,
        "droppedAttributesCount": 0,
        "droppedEventsCount": 0,
        "droppedLinksCount": 0,
        "links": [],
        "instrumentationScope": {"name": "copilot-chat", "version": "0.73.0"},
        "resource": {"attributes": {
            "service.name": "copilot-chat",
            "service.version": "0.73.0",
            "session.id": "synthetic-resource-session",
        }},
        "attributes": attrs,
        "status": {"code": status_code, "message": status_message},
        "events": [],
    }
    if with_event:
        record["events"] = [{
            "name": "exception",
            "attributes": {
                "exception.type": "Error",
                "exception.message": "synthetic exporter failure",
                "exception.stacktrace": STACKTRACE_SENTINEL,
            },
            "time": [end[0], 178809100],
            "droppedAttributesCount": 0,
        }]
    record.update(overrides)
    return record


def metric_record(descriptor="gen_ai.client.operation.duration"):
    return {
        "resource": {"attributes": {"service.name": "copilot-chat"}},
        "scopeMetrics": [{
            "scope": {"name": "copilot-chat", "version": "0.73.0"},
            "metrics": [{"descriptor": {"name": descriptor, "type": "HISTOGRAM"}}],
        }],
    }


EMB_MODEL = "metis-1024-I16-Binary"
CHAT_MODEL_2 = "nes-callisto-003"
TRACE_3 = "11" * 16
TRACE_4 = "22" * 16
TRACE_5 = "33" * 16
TRACE_6 = "44" * 16
TRACE_7 = "55" * 16
SPAN_3 = "66" * 8
SPAN_4 = "77" * 8
SPAN_5 = "88" * 8
SPAN_6 = "99" * 8
SPAN_7 = "ab" * 8


def embeddings_span(trace=TRACE_1, span=SPAN_1, model=EMB_MODEL,
                    start=(SEC, 680000000), end=(SEC + 1, 206322000),
                    duration=(0, 526322000), **overrides):
    """One synthetic real-exporter embeddings span (run2 shape)."""
    record = {
        "traceId": trace,
        "spanId": span,
        "name": "embeddings " + model,
        "kind": 2,
        "startTime": list(start),
        "endTime": list(end),
        "duration": list(duration),
        "ended": True,
        "traceFlags": 1,
        "droppedAttributesCount": 0,
        "droppedEventsCount": 0,
        "droppedLinksCount": 0,
        "links": [],
        "instrumentationScope": {"name": "copilot-chat", "version": "0.73.0"},
        "resource": {"attributes": {
            "service.name": "copilot-chat",
            "service.version": "0.73.0",
            "session.id": "synthetic-resource-session",
        }},
        "attributes": {
            "gen_ai.operation.name": "embeddings",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": model,
            "gen_ai.embeddings.input_count": 1,
        },
        "status": {"code": 0},
        "events": [],
    }
    record.update(overrides)
    return record


def run2_chat_span(trace=TRACE_1, span=SPAN_1, model=CHAT_MODEL_2,
                   start=(SEC, 616000000), end=(SEC + 1, 240817900),
                   duration=(0, 624817900),
                   input_tokens=1128, output_tokens=191, **overrides):
    """One synthetic run2 chat span: status 0, usage tokens, no event."""
    record = {
        "traceId": trace,
        "spanId": span,
        "name": "chat " + model,
        "kind": 2,
        "startTime": list(start),
        "endTime": list(end),
        "duration": list(duration),
        "ended": True,
        "traceFlags": 1,
        "droppedAttributesCount": 0,
        "droppedEventsCount": 0,
        "droppedLinksCount": 0,
        "links": [],
        "instrumentationScope": {"name": "copilot-chat", "version": "0.73.0"},
        "resource": {"attributes": {
            "service.name": "copilot-chat",
            "service.version": "0.73.0",
            "session.id": "synthetic-resource-session",
        }},
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "github",
            "gen_ai.request.model": model,
            "gen_ai.request.max_tokens": 2048,
            "gen_ai.request.temperature": 0,
            "gen_ai.request.top_p": 1,
            "copilot_chat.request.max_prompt_tokens": 12285,
            "gen_ai.agent.name": "synthetic-agent",
            "copilot_chat.request.options": '{"stream":true}',
            "copilot_chat.request.shape": '{"api":"messages","messageCount":2}',
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.response.model": model,
            "gen_ai.response.id": "synthetic-response-id",
            "gen_ai.response.finish_reasons": ["stop"],
            "gen_ai.usage.cache_read.input_tokens": 86,
            "copilot_chat.time_to_first_token": 638,
            "gen_ai.request.stream": True,
            "gen_ai.response.time_to_first_chunk": 0.428,
            "copilot_chat.server_request_id": "synthetic-request-id",
        },
        "status": {"code": 0},
        "events": [],
    }
    record.update(overrides)
    return record


def log_record(model="nes-callisto-003", **overrides):
    """One synthetic OTLP log record (run2 shape, no scopeLogs wrapper)."""
    record = {
        "hrTime": [SEC + 1, 240000000],
        "hrTimeObserved": [SEC + 1, 240000000],
        "body": "GenAI inference: synthetic-model",
        "instrumentationScope": {"name": "copilot-chat", "version": "0.73.0"},
        "droppedAttributesCount": 0,
        "attributes": {
            "event.name": "gen_ai.client.inference.operation.details",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "gen_ai.response.id": "synthetic-response-id",
            "gen_ai.response.finish_reasons": ["stop"],
            "gen_ai.usage.input_tokens": 1128,
            "gen_ai.usage.output_tokens": 191,
            "gen_ai.request.temperature": 0,
        },
        "resource": {"attributes": {"service.name": "copilot-chat"}},
    }
    record.update(overrides)
    return record


def to_jsonl(records):
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


def parse_live_list(records):
    """Live-parse a JSON list of records (kind "spans")."""
    return bridge._parse_source_snapshot(
        json.dumps(records).encode(), "copilot", live=True
    )


def parse_live_jsonl(records):
    """Live-parse JSONL records (kind "jsonl"). Needs 2+ records."""
    return bridge._parse_source_snapshot(to_jsonl(records), "copilot", live=True)


class RealExporterNormalizationTests(unittest.TestCase):
    def test_model_suffixed_chat_span_normalizes_to_llm_call(self):
        kind, _, documents, normalization = parse_live_list([exporter_span()])
        self.assertEqual(kind, "spans")
        self.assertEqual(len(documents), 1)
        self.assertEqual(normalization["renamed_spans"], 1)
        self.assertEqual(normalization["model_suffixed_renames"], 1)
        self.assertEqual(normalization["spans_accepted"], 1)
        self.assertEqual(normalization["spans_rejected"], 0)
        span = documents[0]["spans"][0]
        self.assertEqual(span["name"], "llm.call")
        self.assertEqual(span["kind"], "llm")
        self.assertEqual(span["llm"]["model"], MODEL)
        self.assertEqual(span["attributes"]["gen_ai.provider.name"], "github")
        self.assertEqual(span["attributes"]["gen_ai.request.model"], MODEL)
        self.assertEqual(span["attributes"]["fleet.span"], "llm.call")
        self.assertEqual(span["attributes"]["fleet.trace"], TRACE_1)

    def test_two_element_timestamps_convert_to_unix_nanos(self):
        kind, _, documents, _ = parse_live_list([exporter_span()])
        span = documents[0]["spans"][0]
        self.assertEqual(span["started_at"], "2023-11-14T22:13:20.178000Z")
        self.assertEqual(span["ended_at"], "2023-11-14T22:13:24.178869Z")
        self.assertAlmostEqual(span["duration_ms"], 4000.8694)
        self.assertEqual(documents[0]["run"]["started_at"], span["started_at"])
        self.assertEqual(documents[0]["run"]["ended_at"], span["ended_at"])

    def test_mixed_metrics_and_spans_jsonl(self):
        records = [
            metric_record(),
            exporter_span(),
            metric_record("gen_ai.client.token.usage"),
            exporter_span(trace=TRACE_2, span=SPAN_2),
            metric_record(),
        ]
        kind, _, documents, normalization = parse_live_jsonl(records)
        self.assertEqual(kind, "jsonl")
        self.assertEqual(normalization["records_total"], 5)
        self.assertEqual(normalization["metric_records"], 3)
        self.assertEqual(normalization["metrics_skipped"], 3)
        self.assertEqual(normalization["metric_descriptors"], 3)
        self.assertEqual(normalization["spans_accepted"], 2)
        self.assertEqual(normalization["spans_rejected"], 0)
        self.assertEqual(len(documents), 2)
        self.assertEqual(
            sorted(doc["run"]["native_id"] for doc in documents),
            [TRACE_1, TRACE_2],
        )

    def test_error_status_preserved_with_bounded_classes(self):
        _, _, documents, normalization = parse_live_list([exporter_span()])
        self.assertEqual(normalization["error_types_mapped"], 1)
        self.assertEqual(normalization["error_messages_sanitized"], 1)
        span = documents[0]["spans"][0]
        self.assertEqual(span["status"], "error")
        self.assertEqual(span["attributes"]["error.type"], "LLMError")
        self.assertEqual(span["error"]["message"], "LLM call failed; details withheld")
        self.assertEqual(documents[0]["run"]["status"], "error")
        self.assertNotIn("synthetic exporter failure", json.dumps(documents))

    def test_exception_stacktrace_never_enters_documents(self):
        _, _, documents, normalization = parse_live_list([exporter_span()])
        self.assertEqual(normalization["exception_events_sanitized"], 1)
        self.assertEqual(normalization["events_dropped"], 0)
        blob = json.dumps(documents)
        self.assertNotIn(STACKTRACE_SENTINEL, blob)
        self.assertNotIn("stacktrace", blob)
        self.assertNotIn("exception", blob)
        self.assertEqual(documents[0].get("events", []), [])

    def test_unknown_attributes_dropped_and_counted(self):
        # 14 fixture attributes: 4 kept (provider, model, error.type,
        # operation.name), 10 dropped; plus 1 dropped resource session.id.
        # (operation.name became allowlisted for run2 embeddings
        # provenance; it was dropped before that change.)
        _, _, documents, normalization = parse_live_list([exporter_span()])
        self.assertEqual(normalization["dropped_attributes"], 11)
        attrs = documents[0]["spans"][0]["attributes"]
        self.assertEqual(attrs["gen_ai.operation.name"], "chat")
        for key in (
            "gen_ai.conversation.id",
            "gen_ai.request.max_tokens", "gen_ai.request.temperature",
            "gen_ai.request.top_p", "copilot_chat.request.max_prompt_tokens",
            "copilot_chat.session_id", "gen_ai.agent.name",
            "copilot_chat.request.options", "copilot_chat.request.shape",
            "gen_ai.response.finish_reasons",
        ):
            self.assertNotIn(key, attrs)
        self.assertNotIn("session.id", attrs)

    def test_unsafe_attribute_value_refuses_snapshot(self):
        span = exporter_span(extra_attrs={"gen_ai.request.model": "sk-secret-model"})
        with self.assertRaises(BridgeError):
            parse_live_list([span])

    def test_malformed_timestamps_reject_only_their_record(self):
        bad_starts = [
            [SEC], [SEC, 178000000, 0], ["1700000000", 0], [SEC, -1],
            [SEC, 1000000000], [SEC + 0.5, 0], [-1, 0],
            [SEC, True], "1700000000",
        ]
        for bad in bad_starts:
            with self.subTest(bad=bad):
                good = exporter_span(trace=TRACE_1, span=SPAN_1)
                bad_span = exporter_span(trace=TRACE_2, span=SPAN_2)
                bad_span["startTime"] = bad
                _, _, documents, normalization = parse_live_jsonl(
                    [good, bad_span]
                )
                self.assertEqual(len(documents), 1)
                self.assertEqual(documents[0]["run"]["native_id"], TRACE_1)
                self.assertEqual(normalization["spans_accepted"], 1)
                self.assertEqual(normalization["spans_rejected"], 1)
                self.assertEqual(
                    normalization["rejected"][0]["reason"], "malformed_timestamp"
                )

    def test_unsupported_span_name_rejects_only_its_record(self):
        bad = exporter_span(
            trace=TRACE_2, span=SPAN_2, name="chat some-other-model"
        )
        _, _, documents, normalization = parse_live_jsonl(
            [exporter_span(), bad]
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(normalization["spans_accepted"], 1)
        self.assertEqual(normalization["spans_rejected"], 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unsupported_span_name"
        )
        weird = exporter_span(trace=TRACE_2, span=SPAN_2, name="do_something_weird")
        _, _, documents, normalization = parse_live_jsonl(
            [exporter_span(), weird]
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unsupported_span_name"
        )

    def test_flat_unknown_span_name_still_refuses_snapshot(self):
        spans = [{
            "traceId": TRACE_1, "spanId": SPAN_1, "name": "do_something_weird",
            "startTimeUnixNano": SEC * 1_000_000_000,
            "endTimeUnixNano": (SEC + 1) * 1_000_000_000,
            "attributes": {}, "status": {"code": 1},
        }]
        with self.assertRaises(BridgeError):
            bridge._parse_source_snapshot(
                json.dumps(spans).encode(), "copilot", live=True
            )

    def test_duration_mismatch_rejects_record(self):
        span = exporter_span(duration=(5, 0))
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(documents, [])
        self.assertEqual(normalization["spans_rejected"], 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "duration_mismatch"
        )

    def test_end_before_start_rejects_record(self):
        span = exporter_span(end=(SEC - 1, 0), duration=(0, 0))
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(documents, [])
        self.assertEqual(
            normalization["rejected"][0]["reason"], "end_before_start"
        )

    def test_upstream_dropped_data_rejects_record(self):
        span = exporter_span(droppedAttributesCount=2)
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(documents, [])
        self.assertEqual(
            normalization["rejected"][0]["reason"], "upstream_dropped_data"
        )

    def test_nonempty_links_reject_record(self):
        span = exporter_span(links=[{"traceId": TRACE_1, "spanId": SPAN_1}])
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(documents, [])
        self.assertEqual(
            normalization["rejected"][0]["reason"], "span_links_not_approved"
        )

    def test_span_not_ended_rejects_record(self):
        span = exporter_span(ended=False)
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(documents, [])
        self.assertEqual(
            normalization["rejected"][0]["reason"], "span_not_ended"
        )

    def test_unapproved_error_type_rejects_record(self):
        span = exporter_span(error_type="SomethingElse")
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(documents, [])
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unapproved_error_type"
        )

    def test_safe_error_message_is_kept_verbatim(self):
        span = exporter_span(status_message="LLM call failed; details withheld")
        _, _, documents, normalization = parse_live_list([span])
        self.assertEqual(normalization["error_messages_sanitized"], 0)
        self.assertEqual(
            documents[0]["spans"][0]["error"]["message"],
            "LLM call failed; details withheld",
        )

    def test_duplicate_span_id_drops_run(self):
        records = [
            exporter_span(trace=TRACE_1, span=SPAN_1),
            exporter_span(trace=TRACE_1, span=SPAN_1),
        ]
        _, _, documents, normalization = parse_live_jsonl(records)
        self.assertEqual(normalization["spans_accepted"], 2)
        self.assertEqual(documents, [])

    def test_invalid_span_id_refuses_snapshot(self):
        span = exporter_span(span="not-hex")
        with self.assertRaises(BridgeError):
            parse_live_list([span])

    def test_empty_input_produces_no_documents(self):
        for raw in (b"", b"\n\n"):
            kind, _, documents, normalization = bridge._parse_source_snapshot(
                raw, "copilot", live=True
            )
            self.assertEqual(kind, "jsonl")
            self.assertEqual(documents, [])
            self.assertEqual(normalization["records_total"], 0)

    def test_metrics_only_input_produces_no_documents(self):
        _, _, documents, normalization = parse_live_jsonl(
            [metric_record(), metric_record()]
        )
        self.assertEqual(documents, [])
        self.assertEqual(normalization["metrics_skipped"], 2)
        self.assertEqual(normalization["metric_descriptors"], 2)
        self.assertEqual(normalization["spans_accepted"], 0)

    def test_malformed_metric_record_rejected(self):
        bad = {"resource": {}, "scopeMetrics": "not-a-list"}
        _, _, documents, normalization = parse_live_jsonl([bad, metric_record()])
        self.assertEqual(documents, [])
        self.assertEqual(normalization["metrics_skipped"], 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "malformed_metric_record"
        )
        extra = dict(metric_record())
        extra["unexpected"] = True
        _, _, documents, normalization = parse_live_jsonl([extra, metric_record()])
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unapproved_metric_field"
        )

    def test_unrecognized_and_non_dict_records_rejected(self):
        records = [exporter_span(), {"foo": "bar"}, "not-a-dict", 42]
        kind, _, documents, normalization = bridge._parse_source_snapshot(
            to_jsonl(records), "copilot", live=True
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(normalization["spans_accepted"], 1)
        self.assertEqual(normalization["spans_rejected"], 3)
        reasons = sorted(r["reason"] for r in normalization["rejected"])
        self.assertEqual(
            reasons, ["not_an_object", "not_an_object", "unrecognized_record"]
        )

    def test_deterministic_normalization(self):
        records = [metric_record(), exporter_span(),
                   exporter_span(trace=TRACE_2, span=SPAN_2)]
        _, _, docs_a, norm_a = parse_live_jsonl(records)
        _, _, docs_b, norm_b = parse_live_jsonl(records)
        self.assertEqual(
            [canonical_bytes(d) for d in docs_a],
            [canonical_bytes(d) for d in docs_b],
        )
        self.assertEqual(norm_a, norm_b)

    def test_canonical_document_never_modified_by_normalization(self):
        raw = canonical_bytes(live_canonical_doc())
        kind, parsed, documents, normalization = bridge._parse_source_snapshot(
            raw, "copilot", live=True
        )
        self.assertEqual(kind, "canonical")
        self.assertEqual(canonical_bytes(documents[0]), raw)
        self.assertEqual(normalization["renamed_spans"], 0)
        self.assertEqual(normalization["dropped_attributes"], 0)
        self.assertEqual(normalization["real_exporter_records"], 0)
        self.assertEqual(normalization["records_total"], 0)

    def test_non_live_mode_refuses_exporter_file(self):
        with self.assertRaises(BridgeError):
            bridge._parse_source_snapshot(
                to_jsonl([exporter_span(), metric_record()]),
                "copilot", live=False,
            )


class RealExporterRun2Tests(unittest.TestCase):
    """Second capture (run2): embeddings, status-0 chat, log records."""

    def test_embeddings_span_normalizes_to_llm_call_with_provenance(self):
        _, _, documents, normalization = parse_live_list([embeddings_span()])
        self.assertEqual(normalization["renamed_spans"], 1)
        self.assertEqual(normalization["model_suffixed_renames"], 1)
        self.assertEqual(normalization["embeddings_renames"], 1)
        self.assertEqual(normalization["spans_accepted"], 1)
        span = documents[0]["spans"][0]
        self.assertEqual(span["name"], "llm.call")
        self.assertNotEqual(span["name"], "tool.exec")
        self.assertEqual(span["kind"], "llm")
        self.assertEqual(span["llm"]["model"], EMB_MODEL)
        self.assertEqual(span["attributes"]["gen_ai.operation.name"], "embeddings")
        self.assertEqual(span["attributes"]["gen_ai.provider.name"], "openai")
        self.assertEqual(span["attributes"]["gen_ai.request.model"], EMB_MODEL)
        self.assertNotIn("gen_ai.embeddings.input_count", span["attributes"])
        # OTel code 0 is UNSET, not OK: the mapper truthfully reports
        # "unknown" (non-error). The run is "unknown" for the same reason.
        self.assertEqual(span["status"], "unknown")
        self.assertEqual(documents[0]["run"]["status"], "unknown")

    def test_embeddings_name_mismatch_rejects_record(self):
        wrong_model = embeddings_span(
            trace=TRACE_2, span=SPAN_2, name="embeddings other-model"
        )
        _, _, documents, normalization = parse_live_jsonl(
            [embeddings_span(), wrong_model]
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(normalization["spans_accepted"], 1)
        self.assertEqual(normalization["spans_rejected"], 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unsupported_span_name"
        )
        wrong_op = embeddings_span(trace=TRACE_2, span=SPAN_2)
        wrong_op["attributes"]["gen_ai.operation.name"] = "chat"
        _, _, documents, normalization = parse_live_jsonl(
            [embeddings_span(), wrong_op]
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unsupported_span_name"
        )

    def test_log_records_counted_and_excluded(self):
        records = [
            log_record(),
            run2_chat_span(),
            metric_record(),
            log_record(),
        ]
        kind, _, documents, normalization = parse_live_jsonl(records)
        self.assertEqual(kind, "jsonl")
        self.assertEqual(normalization["records_total"], 4)
        self.assertEqual(normalization["log_records"], 2)
        self.assertEqual(normalization["logs_skipped"], 2)
        self.assertEqual(normalization["metric_records"], 1)
        self.assertEqual(normalization["metrics_skipped"], 1)
        self.assertEqual(normalization["spans_accepted"], 1)
        self.assertEqual(len(documents), 1)
        blob = json.dumps(documents)
        self.assertNotIn("GenAI inference", blob)
        self.assertNotIn("gen_ai.client.inference.operation.details", blob)
        self.assertNotIn("synthetic-response-id", blob)

    def test_malformed_log_records_rejected(self):
        bad_time = log_record()
        bad_time["hrTime"] = [SEC, -1]
        _, _, documents, normalization = parse_live_jsonl(
            [run2_chat_span(), bad_time]
        )
        self.assertEqual(len(documents), 1)
        self.assertEqual(normalization["logs_skipped"], 0)
        self.assertEqual(normalization["spans_rejected"], 1)
        self.assertEqual(
            normalization["rejected"][0]["reason"], "malformed_timestamp"
        )
        extra = log_record()
        extra["unexpected"] = True
        _, _, documents, normalization = parse_live_jsonl(
            [run2_chat_span(), extra]
        )
        self.assertEqual(
            normalization["rejected"][0]["reason"], "unapproved_log_field"
        )
        bad_body = log_record()
        bad_body["body"] = 42
        _, _, documents, normalization = parse_live_jsonl(
            [run2_chat_span(), bad_body]
        )
        self.assertEqual(
            normalization["rejected"][0]["reason"], "malformed_log_record"
        )

    def test_logs_only_input_produces_no_documents(self):
        _, _, documents, normalization = parse_live_jsonl(
            [log_record(), log_record(), metric_record()]
        )
        self.assertEqual(documents, [])
        self.assertEqual(normalization["logs_skipped"], 2)
        self.assertEqual(normalization["metrics_skipped"], 1)
        self.assertEqual(normalization["spans_accepted"], 0)

    def test_status_zero_preserved_as_unset(self):
        # OTel status code 0 means UNSET: the canonical status is
        # "unknown" — truthful, and non-error (Gap E error mapping must
        # not touch status-0 spans).
        _, _, documents, normalization = parse_live_list([run2_chat_span()])
        self.assertEqual(normalization["error_types_mapped"], 0)
        self.assertEqual(normalization["error_messages_sanitized"], 0)
        span = documents[0]["spans"][0]
        self.assertEqual(span["status"], "unknown")
        self.assertNotEqual(span["status"], "error")
        self.assertNotIn("error", span)
        self.assertEqual(documents[0]["run"]["status"], "unknown")

    def test_usage_tokens_carried_finish_reasons_dropped(self):
        _, _, documents, normalization = parse_live_list([run2_chat_span()])
        span = documents[0]["spans"][0]
        self.assertEqual(span["llm"]["model"], CHAT_MODEL_2)
        self.assertEqual(span["llm"]["input_tokens"], 1128)
        self.assertEqual(span["llm"]["output_tokens"], 191)
        self.assertEqual(
            span["attributes"]["gen_ai.usage.cache_read.input_tokens"], 86
        )
        # Finish reasons are a list value: not allowlisted, dropped and
        # counted (the allowlist only ever carries scalars).
        self.assertNotIn("gen_ai.response.finish_reasons", span["attributes"])
        self.assertNotIn("synthetic-response-id", json.dumps(documents))
        self.assertNotIn("synthetic-request-id", json.dumps(documents))

    def test_run2_shape_end_to_end(self):
        records = [
            embeddings_span(trace=TRACE_1, span=SPAN_1),
            embeddings_span(trace=TRACE_2, span=SPAN_2),
            embeddings_span(trace=TRACE_3, span=SPAN_3),
            run2_chat_span(trace=TRACE_4, span=SPAN_4,
                           input_tokens=1128, output_tokens=191),
            run2_chat_span(trace=TRACE_5, span=SPAN_5,
                           input_tokens=1003, output_tokens=190),
            run2_chat_span(trace=TRACE_6, span=SPAN_6,
                           model="copilot-suggestions-prod-euporie-03",
                           input_tokens=890, output_tokens=5),
            embeddings_span(trace=TRACE_7, span=SPAN_7),
            metric_record(),
            metric_record("gen_ai.client.token.usage"),
            log_record(),
            log_record(),
            log_record(),
        ]
        kind, _, documents, normalization = parse_live_jsonl(records)
        self.assertEqual(kind, "jsonl")
        self.assertEqual(normalization["records_total"], 12)
        self.assertEqual(normalization["spans_accepted"], 7)
        self.assertEqual(normalization["spans_rejected"], 0)
        self.assertEqual(normalization["metric_records"], 2)
        self.assertEqual(normalization["metrics_skipped"], 2)
        self.assertEqual(normalization["log_records"], 3)
        self.assertEqual(normalization["logs_skipped"], 3)
        self.assertEqual(normalization["embeddings_renames"], 4)
        self.assertEqual(normalization["model_suffixed_renames"], 7)
        self.assertEqual(normalization["error_types_mapped"], 0)
        self.assertEqual(len(documents), 7)
        self.assertTrue(all(doc["run"]["status"] == "unknown" for doc in documents))
        self.assertTrue(all(
            doc["spans"][0]["name"] == "llm.call" for doc in documents
        ))
        tokens_in = sum(
            doc["spans"][0]["llm"].get("input_tokens", 0) for doc in documents
        )
        tokens_out = sum(
            doc["spans"][0]["llm"].get("output_tokens", 0) for doc in documents
        )
        self.assertEqual(tokens_in, 1128 + 1003 + 890)
        self.assertEqual(tokens_out, 191 + 190 + 5)
        ops = sorted(
            doc["spans"][0]["attributes"]["gen_ai.operation.name"]
            for doc in documents
        )
        self.assertEqual(ops, ["chat"] * 3 + ["embeddings"] * 4)

    def test_run2_determinism_and_invariance(self):
        records = [
            embeddings_span(), run2_chat_span(trace=TRACE_2, span=SPAN_2),
            log_record(), metric_record(),
        ]
        _, _, docs_a, norm_a = parse_live_jsonl(records)
        _, _, docs_b, norm_b = parse_live_jsonl(records)
        self.assertEqual(
            [canonical_bytes(d) for d in docs_a],
            [canonical_bytes(d) for d in docs_b],
        )
        self.assertEqual(norm_a, norm_b)


class RealExporterDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    def _delivery(self, raw, **kwargs):
        kwargs.setdefault("live", True)
        return deliver_snapshot(
            self.conn, raw, "copilot", "http://127.0.0.1:8765",
            "fake-token", **kwargs,
        )

    def test_live_delivery_imports_normalized_spans_to_ledger(self):
        raw = to_jsonl([
            exporter_span(),
            exporter_span(trace=TRACE_2, span=SPAN_2),
        ])
        with patch.object(bridge, "_deliver_document", return_value={
            "outcome": "delivered", "quality": "complete",
            "already_imported": False, "verified": True,
        }) as send:
            result = self._delivery(raw)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result["outcome"], "delivered")
        self.assertTrue(result["verified"])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["live_normalization"]["metrics_skipped"], 0)
        self.assertEqual(result["live_normalization"]["spans_accepted"], 2)
        receipt = result["fleet_receipt"]
        self.assertEqual(receipt["quality"], "complete")
        self.assertEqual(receipt["stats"]["runs"], 2)
        self.assertEqual(receipt["stats"]["spans"], 2)
        self.assertEqual(receipt["stats"]["detection_skipped_lines"], 0)
        for trace in (TRACE_1, TRACE_2):
            stored = ledger.existing_spans(self.conn, trace)
            self.assertEqual(len(stored), 1)
            row = next(iter(stored.values()))
            self.assertEqual(row["name"], "llm.call")
            self.assertEqual(row["kind"], "llm")
            self.assertEqual(row["status"], "error")
            blob = json.dumps(row["attributes"])
            self.assertNotIn("synthetic-session", blob)
            self.assertNotIn("synthetic-conversation", blob)
            self.assertNotIn("SENTINEL", blob)
            self.assertNotIn("synthetic exporter failure", blob)
            self.assertEqual(
                row["attributes"]["error.type"], "LLMError"
            )

    def test_live_delivery_counts_metrics_in_normalization(self):
        raw = to_jsonl([metric_record(), exporter_span(), metric_record()])
        with patch.object(bridge, "_deliver_document", return_value={
            "outcome": "delivered", "quality": "complete",
            "already_imported": False, "verified": True,
        }) as send:
            result = self._delivery(raw)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["live_normalization"]["metrics_skipped"], 2)
        self.assertEqual(result["fleet_receipt"]["stats"]["spans"], 1)
        self.assertEqual(result["fleet_receipt"]["quality"], "complete")

    def test_reimport_is_idempotent(self):
        _, parsed, _, _ = parse_live_jsonl(
            [exporter_span(), exporter_span(trace=TRACE_2, span=SPAN_2)]
        )
        import_raw = b"\n".join(
            json.dumps(span, sort_keys=True, separators=(",", ":")).encode()
            for span in parsed[0]
        )
        first = import_bytes(self.conn, import_raw, "copilot")
        second = import_bytes(self.conn, import_raw, "copilot")
        self.assertFalse(first["already_imported"])
        self.assertTrue(second["already_imported"])
        self.assertEqual(first["import_id"], second["import_id"])
        self.assertEqual(first["stats"], second["stats"])

    def test_synthetic_only_refuses_real_exporter_file(self):
        raw = to_jsonl([exporter_span(), exporter_span(trace=TRACE_2, span=SPAN_2)])
        with self.assertRaises(BridgeError):
            deliver_snapshot(
                self.conn, raw, "copilot", "http://127.0.0.1:8765",
                "fake-token", synthetic_only=True,
            )
        self.assertEqual(ledger.counts(self.conn)["imports"], 0)

    def test_live_delivery_requires_acknowledgment(self):
        raw = to_jsonl([exporter_span()])
        with patch.object(bridge, "_deliver_document") as send:
            with self.assertRaises(BridgeError):
                deliver_snapshot(
                    self.conn, raw, "copilot", "http://127.0.0.1:8765",
                    "fake-token",
                )
        send.assert_not_called()

    def test_metrics_only_delivery_is_rejected(self):
        raw = to_jsonl([metric_record(), metric_record()])
        with patch.object(bridge, "_deliver_document") as send:
            result = self._delivery(raw)
        send.assert_not_called()
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(result["verified"])
        self.assertEqual(result["live_normalization"]["metrics_skipped"], 2)


if __name__ == "__main__":
    unittest.main()
