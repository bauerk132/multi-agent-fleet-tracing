"""M7-B step 1 tests (unittest): Claude Code adapter vocabulary and flatten.

Covers: vocabulary checks (non-empty allowlists, subsets of the bridge
allowlists, disjointness from STRIPPED_KEYS, read-only span-type map),
malformed-line counting in the flatten step (test 14), flatten robustness
(bad resource and span shapes, bad identity fields, bytes decoding, NaN and
Infinity, nesting), strict OTLP/JSON AnyValue decoding with numeric-only
counters, span timestamps, resource bounds (line size, span cap, attribute
cap), resource attribute copy per span, events replaced by events_count, and
BOM handling (one leading BOM removed for str and UTF-8 bytes alike). Run from
observability/:
    python -m unittest tests.test_claude_code_plugin -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys_path = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, sys_path)

from agentscope_importer import bridge, mapper  # noqa: E402
from claude_code_plugin import adapter as adapter_module  # noqa: E402
from claude_code_plugin._vocab import (  # noqa: E402
    OUTPUT_ATTRIBUTE_KEYS,
    SPAN_TYPE_TO_V1,
)
from claude_code_plugin import _sanitize  # noqa: E402
from claude_code_plugin import _vocab  # noqa: E402
from claude_code_plugin.adapter import flatten_lines  # noqa: E402


class ClaudeCodeVocabBridgeTests(unittest.TestCase):
    def test_output_keys_subset_of_bridge_allowlist(self):
        # Test 4: every key the adapter may emit is accepted by the bridge.
        self.assertTrue(OUTPUT_ATTRIBUTE_KEYS, "OUTPUT_ATTRIBUTE_KEYS must not be empty")
        extra = set(OUTPUT_ATTRIBUTE_KEYS) - set(bridge.ATTRIBUTE_ALLOWLIST)
        self.assertEqual(
            extra, set(),
            f"adapter emits keys the bridge would refuse: {sorted(extra)}",
        )

    def test_output_names_subset_of_bridge_span_names(self):
        # Test 5: every fleet span name the adapter may emit is canonical.
        self.assertTrue(SPAN_TYPE_TO_V1, "SPAN_TYPE_TO_V1 must not be empty")
        names = set(SPAN_TYPE_TO_V1.values())
        self.assertEqual(
            names, {"agent.run", "llm.call", "tool.exec", "hook.exec"},
            "SPAN_TYPE_TO_V1 must map to the four fleet span names",
        )
        self.assertEqual(dict(SPAN_TYPE_TO_V1), {
            "claude_code.interaction": "agent.run",
            "claude_code.llm_request": "llm.call",
            "claude_code.tool": "tool.exec",
            "claude_code.hook": "hook.exec",
        })
        extra = names - set(bridge.SPAN_NAMES)
        self.assertEqual(
            extra, set(),
            f"adapter emits span names the bridge would refuse: {sorted(extra)}",
        )

    def test_output_keys_disjoint_from_stripped_keys(self):
        # Keys the bridge strips (content, identities, host or user data) must
        # never be emitted by the adapter.
        overlap = set(OUTPUT_ATTRIBUTE_KEYS) & set(bridge.STRIPPED_KEYS)
        self.assertEqual(
            overlap, set(),
            f"adapter emits keys the bridge strips: {sorted(overlap)}",
        )

    def test_span_type_map_is_read_only(self):
        # The map is shared module state: assignment, deletion and in-place
        # update must all fail, and the mapping must be unchanged afterwards.
        before = dict(SPAN_TYPE_TO_V1)
        with self.assertRaises(TypeError):
            SPAN_TYPE_TO_V1["claude_code.interaction"] = "tool.exec"
        with self.assertRaises(TypeError):
            del SPAN_TYPE_TO_V1["claude_code.hook"]
        with self.assertRaises(TypeError):
            dict.update(SPAN_TYPE_TO_V1, {"claude_code.new": "agent.run"})
        self.assertEqual(dict(SPAN_TYPE_TO_V1), before)


# Synthetic helpers: fake hex IDs and the documented span name only.
def _span(name="claude_code.interaction"):
    return {
        "traceId": "a" * 32,
        "spanId": "b" * 16,
        "parentSpanId": "",
        "name": name,
        "attributes": [],
        "events": [],
        "status": {"code": 1},
    }


def _line(resource_spans):
    return json.dumps({"resourceSpans": resource_spans})


def _good_line():
    return _line([{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "claude-code"}},
        ]},
        "scopeSpans": [{"spans": [_span()]}],
    }])


class FlattenMalformedLinesTests(unittest.TestCase):
    def test_malformed_lines_counted_not_raised(self):
        # Test 14: bad lines are counted, never raised; good spans still flatten.
        # Fixture is SYNTHETIC: fake hex IDs, documented span name only.
        good = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-code"}},
                ]},
                "scopeSpans": [{"spans": [{
                    "traceId": "a" * 32,
                    "spanId": "b" * 16,
                    "parentSpanId": "",
                    "name": "claude_code.interaction",
                    "attributes": [
                        {"key": "span.type", "value": {"stringValue": "claude_code.interaction"}},
                    ],
                    "events": [],
                    "status": {"code": 1},
                }]}],
            }],
        }
        lines = [
            json.dumps(good),
            "",                      # blank: skipped, not counted
            "{not json",             # malformed: invalid JSON
            json.dumps([1, 2]),      # malformed: not an object
            json.dumps({"other": 1}),  # malformed: no resourceSpans list
            json.dumps(good),
        ]

        spans, counters = flatten_lines(lines)

        self.assertEqual(counters, {
            "lines_read": 5, "malformed_lines": 3, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["name"], "claude_code.interaction")
        self.assertEqual(spans[0]["traceId"], "a" * 32)
        self.assertEqual(spans[0]["attributes"]["span.type"], "claude_code.interaction")
        self.assertEqual(spans[0]["resource_attributes"]["service.name"], "claude-code")

    def test_bare_str_or_bytes_lines_raises_type_error(self):
        # A caller passing one string/bytes instead of a list of lines is a bug.
        with self.assertRaises(TypeError):
            flatten_lines(_good_line())
        with self.assertRaises(TypeError):
            flatten_lines(_good_line().encode("utf-8"))

    def test_bad_resource_shape_skipped_not_raised(self):
        # A non-dict resource is counted; its scopeSpans still flatten with
        # empty resource attributes. Later good lines are unaffected.
        for bad_resource in ("oops", [1, 2]):
            with self.subTest(resource=repr(bad_resource)):
                bad = _line([{"resource": bad_resource, "scopeSpans": [{"spans": [_span()]}]}])
                spans, counters = flatten_lines([bad, _good_line()])
                self.assertEqual(counters, {
                    "lines_read": 2, "malformed_lines": 0, "spans": 2, "skipped_items": 1,
                    "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
                    "stream_truncated": 0,
                })
                self.assertEqual(spans[0]["resource_attributes"], {})
                self.assertEqual(spans[1]["resource_attributes"]["service.name"], "claude-code")

    def test_non_list_scope_spans_and_spans_counted(self):
        # Each present-but-wrong-type value is skipped and counted once.
        bad_lines = [
            _line([{"resource": {}, "scopeSpans": {"spans": [_span()]}}]),
            _line([{"resource": {}, "scopeSpans": "nope"}]),
            _line([{"resource": {}, "scopeSpans": [{"spans": {"x": 1}}]}]),
            _line([{"resource": {}, "scopeSpans": [{"spans": "nope"}]}]),
        ]
        spans, counters = flatten_lines(bad_lines + [_good_line()])
        self.assertEqual(counters, {
            "lines_read": 5, "malformed_lines": 0, "spans": 1, "skipped_items": 4,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 1)

    def test_non_dict_resource_span_entries_counted(self):
        # Non-dict resourceSpans entries (including None inside a list) are counted.
        line = _line([1, "x", None])
        spans, counters = flatten_lines([line, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 1, "skipped_items": 3,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 1)

    def test_non_string_identity_skips_span(self):
        # name, traceId, spanId and parentSpanId must be str or None. Any other
        # type skips that span (counted once); the valid span still flattens.
        bad_name = _span()
        bad_name["name"] = {"x": 1}
        bad_span_id = _span()
        bad_span_id["spanId"] = 7
        bad_trace_id = _span()
        bad_trace_id["traceId"] = ["a" * 32]
        bad_parent = _span()
        bad_parent["parentSpanId"] = True
        valid = _span()
        del valid["parentSpanId"]
        line = _line([{
            "resource": {},
            "scopeSpans": [{"spans": [bad_name, bad_span_id, bad_trace_id, bad_parent, valid]}],
        }])
        spans, counters = flatten_lines([line])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 4,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 1)
        for span in spans:
            # Must not raise: names reaching the vocab lookup are hashable.
            SPAN_TYPE_TO_V1.get(span["name"])

    def test_deeply_nested_line_is_malformed(self):
        # ~5000 levels of nesting raises RecursionError in json.loads: malformed.
        deep = "[" * 5000 + "]" * 5000
        spans, counters = flatten_lines([_good_line(), deep, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 3, "malformed_lines": 1, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 2)

    def test_non_text_line_is_malformed(self):
        # None and int items count as read and malformed; later lines still flatten.
        spans, counters = flatten_lines([_good_line(), None, 123, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 4, "malformed_lines": 2, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 2)

    def test_bytes_line_decoded_or_malformed(self):
        # Valid UTF-8 bytes flatten; invalid UTF-8 bytes are malformed.
        spans, counters = flatten_lines([
            _good_line().encode("utf-8"),
            b"\xff\xfe\x80 not utf-8",
            _good_line().encode("utf-8"),
        ])
        self.assertEqual(counters, {
            "lines_read": 3, "malformed_lines": 1, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 2)

    def test_nan_infinity_line_is_malformed(self):
        # Non-finite JSON constants and out-of-range floats make the whole line
        # malformed under strict parsing. Good lines around them still flatten.
        template = (
            '{"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [{'
            '"name": "claude_code.interaction", '
            '"traceId": "' + "a" * 32 + '", "spanId": "' + "b" * 16 + '", '
            '"parentSpanId": "", "attributes": [], "events": [], '
            '"status": {"code": %s}}]}]}]}'
        )
        bad_lines = [template % token for token in ("NaN", "Infinity", "-Infinity", "1e1000")]
        spans, counters = flatten_lines([_good_line()] + bad_lines + [_good_line()])
        self.assertEqual(counters, {
            "lines_read": 6, "malformed_lines": 4, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 2)


def _kv(key, value):
    return {"key": key, "value": value}


class StrictAnyValueTests(unittest.TestCase):
    def test_string_int_value_decoded_and_bridge_accepts(self):
        # intValue given as a digit string decodes to int and the bridge accepts it.
        span = _span()
        span["attributes"] = [_kv("gen_ai.usage.input_tokens", {"intValue": "42"})]
        spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(counters["dropped_attributes"], 0)
        value = spans[0]["attributes"]["gen_ai.usage.input_tokens"]
        self.assertIs(type(value), int)
        self.assertEqual(value, 42)
        # Raises BridgeError if the bridge refuses the decoded value.
        bridge._attribute_value("gen_ai.usage.input_tokens", value, "span")

    def test_invalid_any_value_dropped_and_counted(self):
        # Valid items are kept; each invalid item is dropped and counted once.
        span = _span()
        span["attributes"] = [
            # valid
            _kv("a.str", {"stringValue": "ok"}),
            _kv("a.int", {"intValue": 7}),
            _kv("a.int_str", {"intValue": "-9223372036854775808"}),
            _kv("a.int_max", {"intValue": "9223372036854775807"}),
            _kv("a.dbl", {"doubleValue": 2.5}),
            _kv("a.bool", {"boolValue": True}),
            # invalid
            _kv("i.multi", {"stringValue": "x", "intValue": 1}),
            _kv("i.int_true", {"intValue": True}),
            _kv("i.int_1e3", {"intValue": "1e3"}),
            _kv("i.int_overflow", {"intValue": "9223372036854775808"}),
            _kv("i.bool_str", {"boolValue": "false"}),
            _kv("i.str_obj", {"stringValue": {"x": 1}}),
            _kv("i.array", {"arrayValue": {"values": []}}),
            _kv("i.bytes", {"bytesValue": "AAAA"}),
            _kv("i.dbl_str", {"doubleValue": "1.5"}),
            _kv("i.empty", {}),
            "not-a-dict",
            {"key": 5, "value": {"stringValue": "x"}},
        ]
        spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 12, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(spans[0]["attributes"], {
            "a.str": "ok",
            "a.int": 7,
            "a.int_str": -9223372036854775808,
            "a.int_max": 9223372036854775807,
            "a.dbl": 2.5,
            "a.bool": True,
        })

    def test_attributes_not_a_list_counted(self):
        # A present non-list attributes value is counted once and treated as empty.
        # Resource attributes count once per resource entry, span attributes once
        # per span; a missing or None attributes value is not counted.
        span_dict = _span()
        span_dict["attributes"] = {"gen_ai.usage.input_tokens": {"intValue": 1}}
        span_dict_2 = _span()
        span_dict_2["attributes"] = {"x": 1}
        span_none = _span()
        span_none["attributes"] = None
        line = _line([{
            "resource": {"attributes": "oops"},
            "scopeSpans": [{"spans": [span_dict, span_dict_2, span_none]}],
        }])
        spans, counters = flatten_lines([line])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 3, "skipped_items": 3,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 3)
        for span in spans:
            self.assertEqual(span["attributes"], {})
            self.assertEqual(span["resource_attributes"], {})

    def test_resource_drops_counted_once_per_resource_entry(self):
        # One bad resource attribute, two spans under the same resource: counted once.
        line = _line([{
            "resource": {"attributes": [
                _kv("service.name", {"stringValue": "claude-code"}),
                _kv("bad", {}),
            ]},
            "scopeSpans": [{"spans": [_span(), _span()]}],
        }])
        spans, counters = flatten_lines([line])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(spans[0]["resource_attributes"], {"service.name": "claude-code"})


def _timed_span(span_id, parent_id="", **times):
    span = _span()
    span["spanId"] = span_id
    span["parentSpanId"] = parent_id
    span.update(times)
    return span


class FlattenTimestampTests(unittest.TestCase):
    def test_timestamps_kept_and_parent_ordered_first(self):
        # Child listed first, parent second. Times are digit strings; flatten
        # must keep them as ints under the bridge/mapper key names, so the
        # mapper orders the parent (start 1000) before the child (start 2000).
        child = _timed_span("11" * 8, "ff" * 8, startTimeUnixNano="2000", endTimeUnixNano="3000")
        parent = _timed_span("ff" * 8, "", startTimeUnixNano="1000", endTimeUnixNano="9000")
        line = _line([{"resource": {}, "scopeSpans": [{"spans": [child, parent]}]}])

        spans, _counters = flatten_lines([line])
        by_id = {span["spanId"]: span for span in spans}
        self.assertEqual(by_id["ff" * 8]["startTimeUnixNano"], 1000)
        self.assertEqual(by_id["ff" * 8]["endTimeUnixNano"], 9000)
        self.assertEqual(by_id["11" * 8]["startTimeUnixNano"], 2000)
        self.assertEqual(by_id["11" * 8]["endTimeUnixNano"], 3000)
        self.assertIs(type(by_id["11" * 8]["startTimeUnixNano"]), int)

        # Flatten records feed mapper.map_trace directly. Raw status (which may
        # carry free-text status.message) passes through flatten unchanged and
        # is cleaned and bounded in the later mapping step. Flatten records also
        # carry events_count and resource_attributes, which are not in
        # bridge.RAW_SPAN_KEYS, so these records are not meant for
        # bridge._validate_flat_spans.
        document, _notes, dropped = mapper.map_trace("a" * 32, spans, "claude-code")
        self.assertIsNone(dropped)
        by_native = {span["native_id"]: span for span in document["spans"]}
        parent_doc = by_native["ff" * 8]
        child_doc = by_native["11" * 8]
        self.assertEqual(parent_doc["seq"], 0)
        self.assertLess(parent_doc["seq"], child_doc["seq"])
        self.assertEqual(child_doc["parent_native_id"], "ff" * 8)
        self.assertIsNotNone(parent_doc["started_at"])
        self.assertIsNotNone(child_doc["started_at"])
        self.assertAlmostEqual(parent_doc["duration_ms"], 0.008)
        self.assertAlmostEqual(child_doc["duration_ms"], 0.001)

    def test_invalid_timestamp_set_none_and_counted(self):
        # One bad start value per span: each is None and counted once.
        bad_values = [-1, True, 1.5, "12x", "", 2 ** 64, [1], {}]
        bad_spans = [
            _timed_span("%016x" % (index + 1), "", startTimeUnixNano=value)
            for index, value in enumerate(bad_values)
        ]
        missing = _timed_span("c" * 16, "")  # no time keys at all: not counted
        int_five = _timed_span("d" * 16, "", startTimeUnixNano=5)
        str_five = _timed_span("e" * 16, "", startTimeUnixNano="5")
        max_valid = _timed_span("f" * 16, "", startTimeUnixNano="18446744073709551615")
        line = _line([{
            "resource": {},
            "scopeSpans": [{"spans": bad_spans + [missing, int_five, str_five, max_valid]}],
        }])

        spans, counters = flatten_lines([line])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 12, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 8, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        by_id = {span["spanId"]: span for span in spans}
        for index in range(len(bad_values)):
            span = by_id["%016x" % (index + 1)]
            self.assertIsNone(span["startTimeUnixNano"], f"bad value {bad_values[index]!r}")
            self.assertIsNone(span["endTimeUnixNano"])
        self.assertIsNone(by_id["c" * 16]["startTimeUnixNano"])
        self.assertIsNone(by_id["c" * 16]["endTimeUnixNano"])
        self.assertEqual(by_id["d" * 16]["startTimeUnixNano"], 5)
        self.assertIs(type(by_id["d" * 16]["startTimeUnixNano"]), int)
        self.assertEqual(by_id["e" * 16]["startTimeUnixNano"], 5)
        self.assertIs(type(by_id["e" * 16]["startTimeUnixNano"]), int)
        self.assertEqual(by_id["f" * 16]["startTimeUnixNano"], 2 ** 64 - 1)


class BomHandlingTests(unittest.TestCase):
    def test_bom_handled_consistently(self):
        # One leading BOM is removed for str and UTF-8 bytes alike. Two BOMs
        # make the line malformed for both.
        line = _good_line()
        spans, counters = flatten_lines(["\ufeff" + line])
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters["malformed_lines"], 0)
        str_result = (len(spans), counters["malformed_lines"])

        spans, counters = flatten_lines([b"\xef\xbb\xbf" + line.encode("utf-8")])
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters["malformed_lines"], 0)
        self.assertEqual((len(spans), counters["malformed_lines"]), str_result)

        spans, counters = flatten_lines(["\ufeff\ufeff" + line])
        self.assertEqual(len(spans), 0)
        self.assertEqual(counters["malformed_lines"], 1)

        spans, counters = flatten_lines([b"\xef\xbb\xbf\xef\xbb\xbf" + line.encode("utf-8")])
        self.assertEqual(len(spans), 0)
        self.assertEqual(counters["malformed_lines"], 1)


def _padded_line(total_bytes, filler="x"):
    """Synthetic one-span receiver line padded to exactly ``total_bytes`` UTF-8 bytes."""
    def build(pad):
        obj = {
            "resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [_span()]}]}],
            "pad": pad,
        }
        return json.dumps(obj, ensure_ascii=False)

    per_char = len(filler.encode("utf-8"))
    empty = len(build("").encode("utf-8"))
    count, remainder = divmod(total_bytes - empty, per_char)
    assert remainder == 0 and count >= 0, "cannot hit the requested byte size"
    return build(filler * count)


class ResourceLimitTests(unittest.TestCase):
    def test_oversized_line_is_malformed(self):
        # Limit 1000 bytes. Oversize lines (str and bytes items, and a multi-byte
        # line whose char count is under the limit but byte count is over) are
        # counted as read and malformed and never parsed. A line exactly at the
        # limit is accepted.
        limit = 1000
        multibyte = json.dumps(
            {"resourceSpans": [], "pad": "é" * 700}, ensure_ascii=False,
        )
        self.assertLess(len(multibyte), limit)
        self.assertGreater(len(multibyte.encode("utf-8")), limit)

        exact = _padded_line(limit)
        self.assertEqual(len(exact.encode("utf-8")), limit)

        with mock.patch.object(adapter_module, "MAX_LINE_BYTES", limit):
            lines = [
                _good_line(),
                _padded_line(limit + 1),                    # str over limit
                exact,                                      # exactly at limit: accepted
                _padded_line(limit + 1).encode("utf-8"),    # bytes over limit
                multibyte,                                  # chars under, bytes over
                _good_line(),
            ]
            spans, counters = flatten_lines(lines)

        self.assertEqual(counters, {
            "lines_read": 6, "malformed_lines": 3, "spans": 3, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 3)
        self.assertEqual({span["name"] for span in spans}, {"claude_code.interaction"})

    def test_span_cap_counts_overflow(self):
        # MAX_SPANS 3 across the whole stream: the first three spans are kept,
        # every later span (same line or later line) is skipped and counted.
        with mock.patch.object(adapter_module, "MAX_SPANS", 3):
            spans, counters = flatten_lines([
                _line([{"resource": {}, "scopeSpans": [{"spans": [_span() for _ in range(5)]}]}]),
                _line([{"resource": {}, "scopeSpans": [{"spans": [_span(), _span()]}]}]),
            ])
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 3, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 4,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 3)

    def test_attribute_cap_drops_extras(self):
        # MAX_ATTRIBUTES_PER_SPAN 4: only the first four items are decoded; each
        # extra item counts as one dropped attribute. Same for resource attributes.
        with mock.patch.object(adapter_module, "MAX_ATTRIBUTES_PER_SPAN", 4):
            span = _span()
            span["attributes"] = [_kv("k%d" % i, {"stringValue": str(i)}) for i in range(10)]
            spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
            self.assertEqual(spans[0]["attributes"], {"k0": "0", "k1": "1", "k2": "2", "k3": "3"})
            self.assertEqual(counters, {
                "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
                "dropped_attributes": 6, "invalid_timestamps": 0, "spans_over_limit": 0,
                "stream_truncated": 0,
            })

            resource_attrs = [_kv("r%d" % i, {"stringValue": "v"}) for i in range(6)]
            spans, counters = flatten_lines([_line([{
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [{"spans": [_span()]}],
            }])])
            self.assertEqual(sorted(spans[0]["resource_attributes"]), ["r0", "r1", "r2", "r3"])
            self.assertEqual(counters, {
                "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
                "dropped_attributes": 2, "invalid_timestamps": 0, "spans_over_limit": 0,
                "stream_truncated": 0,
            })


class PostCapResourceTests(unittest.TestCase):
    # ADJ-R2-P2-02: once the span cap is reached, a later resource entry must
    # not have its resource attributes decoded at all.

    def _cap_lines(self):
        first = _line([{"resource": {}, "scopeSpans": [{"spans": [_span()]}]}])
        # The invalid item would add 1 to dropped_attributes if it were decoded.
        second = _line([{
            "resource": {"attributes": [_kv("bad", {})]},
            "scopeSpans": [{"spans": [_span()]}],
        }])
        return [first, second]

    def test_no_resource_decode_after_span_cap(self):
        original = adapter_module._decode_any_value
        with mock.patch.object(adapter_module, "MAX_SPANS", 1):
            with mock.patch.object(adapter_module, "_decode_any_value", wraps=original) as spy:
                spans, counters = flatten_lines(self._cap_lines())
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 1,
            "stream_truncated": 0,
        })
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(spans[0]["resource_attributes"], {})

    def test_resource_decoded_before_cap(self):
        spans, counters = flatten_lines(self._cap_lines())
        self.assertEqual(len(spans), 2)
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })


class ResourceCopyAndEventRecordTests(unittest.TestCase):
    def test_resource_attributes_not_shared(self):
        # P2-01: each span gets its own copy of the resource attributes, so
        # mutating one span's copy must not change a sibling span's copy.
        line = _line([{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "claude-code"}},
            ]},
            "scopeSpans": [{"spans": [_span(), _span()]}],
        }])
        spans, _counters = flatten_lines([line])
        self.assertEqual(len(spans), 2)
        self.assertIsNot(spans[0]["resource_attributes"], spans[1]["resource_attributes"])

        spans[0]["resource_attributes"].clear()
        spans[0]["resource_attributes"]["extra.key"] = "x"
        self.assertEqual(spans[1]["resource_attributes"], {"service.name": "claude-code"})

    def test_events_replaced_by_count(self):
        # P2-02: raw events (which can hold exception text) are not carried in
        # the span record. Only events_count (len of the list) remains.
        # Fixture is SYNTHETIC: obvious fake marker and stack-like text.
        span = _span()
        span["events"] = [
            {"name": "user_prompt", "timeUnixNano": "1000", "attributes": []},
            {"name": "tool_result", "timeUnixNano": "2000", "attributes": []},
            {"name": "exception", "timeUnixNano": "3000", "attributes": [
                _kv("exception.message", {"stringValue": "SYNTH_RAW_EVENT_TEXT"}),
                _kv("exception.stacktrace", {
                    "stringValue": 'Traceback (most recent call last):\n  File "synthetic.py", line 1',
                }),
            ]},
        ]
        line = _line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])
        spans, counters = flatten_lines([line])
        self.assertEqual(len(spans), 1)
        record = spans[0]
        self.assertEqual(record["events_count"], 3)
        self.assertNotIn("events", record)
        self.assertNotIn("SYNTH_RAW_EVENT_TEXT", json.dumps(record))
        self.assertNotIn("SYNTH_RAW_EVENT_TEXT", repr(counters))
        self.assertNotIn("synthetic.py", json.dumps(record))

    def test_events_count_zero_when_absent_or_not_list(self):
        no_events = _span()
        del no_events["events"]
        not_list = _span()
        not_list["events"] = {"name": "exception"}
        line = _line([{"resource": {}, "scopeSpans": [{"spans": [no_events, not_list]}]}])
        spans, _counters = flatten_lines([line])
        self.assertEqual(len(spans), 2)
        for record in spans:
            self.assertEqual(record["events_count"], 0)
            self.assertNotIn("events", record)


class StreamBudgetTests(unittest.TestCase):
    def test_stream_budget_stops_and_flags(self):
        # Budget for 3 good lines: the 4th item overflows and stops the stream.
        # Items 5 and 6 are never pulled. Same result for str and bytes items.
        line = _good_line()
        size = len(line.encode("utf-8"))
        for kind in ("str", "bytes"):
            with self.subTest(kind=kind):
                pulled = []

                def source():
                    for _ in range(6):
                        pulled.append(1)
                        yield line if kind == "str" else line.encode("utf-8")

                with mock.patch.object(adapter_module, "MAX_TOTAL_BYTES", 3 * size):
                    spans, counters = flatten_lines(source())
                self.assertEqual(len(spans), 3)
                self.assertEqual(counters, {
                    "lines_read": 3, "malformed_lines": 0, "spans": 3, "skipped_items": 0,
                    "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
                    "stream_truncated": 1,
                })
                self.assertEqual(len(pulled), 4)

    def test_stream_budget_exact_boundary_accepted(self):
        # A total exactly equal to the budget is accepted; nothing is flagged.
        line = _good_line()
        size = len(line.encode("utf-8"))
        with mock.patch.object(adapter_module, "MAX_TOTAL_BYTES", 3 * size):
            spans, counters = flatten_lines([line, line, line])
        self.assertEqual(len(spans), 3)
        self.assertEqual(counters, {
            "lines_read": 3, "malformed_lines": 0, "spans": 3, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_stream_budget_charges_blank_and_malformed(self):
        # A blank item is charged too. Budget size+1: "\n" (1 byte) fits, then
        # the second good line overflows and is never parsed.
        line = _good_line()
        size = len(line.encode("utf-8"))
        with mock.patch.object(adapter_module, "MAX_TOTAL_BYTES", size + 1):
            spans, counters = flatten_lines([line, "\n", line])
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 1,
        })
        # A 4-byte blank line is charged: with budget 2*size it pushes the
        # second good line over. Uncharged, both good lines would fit.
        with mock.patch.object(adapter_module, "MAX_TOTAL_BYTES", 2 * size):
            spans, counters = flatten_lines([line, "   \n", line])
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters["stream_truncated"], 1)

    def test_stream_budget_charges_non_text_items(self):
        # Each non-text item is charged the minimum of 1 byte, so an endless
        # stream of them still ends. Budget 3: three counted, then stop.
        with mock.patch.object(adapter_module, "MAX_TOTAL_BYTES", 3):
            spans, counters = flatten_lines([None] * 5)
        self.assertEqual(spans, [])
        self.assertEqual(counters, {
            "lines_read": 3, "malformed_lines": 3, "spans": 0, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 1,
        })

    def test_lone_surrogate_str_sizes_without_raising(self):
        # A str with a lone surrogate cannot be encoded as strict UTF-8. Sizing
        # must not raise: the line is malformed and the stream keeps going.
        spans, counters = flatten_lines(["\ud800", _good_line()])
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 1, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_stream_budget_default_matches_contract(self):
        from agentscope_importer import contract
        self.assertEqual(adapter_module.MAX_TOTAL_BYTES, 16 * 1024 * 1024)
        self.assertEqual(adapter_module.MAX_TOTAL_BYTES, contract.MAX_BYTES_PER_IMPORT)


def _surrogate_line(field, value):
    """Receiver line with ``value`` placed in ``field``, serialized as receiver.py does."""
    span = _span()
    resource_attrs = [_kv("service.name", {"stringValue": "claude-code"})]
    scope = {"spans": [span]}
    if field in ("traceId", "spanId", "parentSpanId", "name"):
        span[field] = value
    elif field == "attribute key":
        span["attributes"] = [_kv(value, {"stringValue": "x"})]
    elif field == "attribute stringValue":
        span["attributes"] = [_kv("a.key", {"stringValue": value})]
    elif field == "resource attribute value":
        resource_attrs = [_kv("service.name", {"stringValue": value})]
    elif field == "status message":
        span["status"] = {"code": 2, "message": value}
    elif field == "status key":
        span["status"] = {"code": 1, value: "x"}
    elif field == "scope name":
        scope["scope"] = {"name": value}
    else:
        raise AssertionError(field)
    obj = {"resourceSpans": [{"resource": {"attributes": resource_attrs}, "scopeSpans": [scope]}]}
    return json.dumps(obj)


class LoneSurrogateTests(unittest.TestCase):
    def test_escaped_lone_surrogate_line_is_malformed(self):
        # A JSON-escaped lone surrogate parses to an unpaired code point that
        # later crashes UTF-8 encoding. The whole line is malformed; the good
        # line around it still flattens.
        fields = [
            "traceId", "spanId", "parentSpanId", "name", "attribute key",
            "attribute stringValue", "resource attribute value", "status message",
            "status key", "scope name",
        ]
        values = ["\ud800", "\udfff", "\ude00\ud83d"]
        for field in fields:
            for value in values:
                for kind in ("str", "bytes"):
                    with self.subTest(field=field, value=repr(value), kind=kind):
                        line = _surrogate_line(field, value)
                        self.assertRegex(line, r"\\u[dD][89a-fA-F]")
                        item = line if kind == "str" else line.encode("utf-8")
                        spans, counters = flatten_lines([item, _good_line()])
                        self.assertEqual(counters, {
                            "lines_read": 2, "malformed_lines": 1, "spans": 1,
                            "skipped_items": 0, "dropped_attributes": 0,
                            "invalid_timestamps": 0, "spans_over_limit": 0,
                            "stream_truncated": 0,
                        })
                        self.assertEqual(len(spans), 1)
                        json.dumps(spans, ensure_ascii=False).encode("utf-8")
                        mapper.run_id_for("0" * 64, spans[0]["traceId"])

    def test_paired_and_backslash_escapes_accepted(self):
        # A correctly paired surrogate escape (an emoji) decodes to a valid code
        # point and is kept. A literal backslash followed by ud800 is an escaped
        # backslash, not a surrogate escape, and is also kept.
        emoji_line = json.dumps({"resourceSpans": [{"resource": {}, "scopeSpans": [
            {"spans": [_span("\U0001F600")]},
        ]}]})
        self.assertIn("\\ud83d\\ude00", emoji_line)
        literal = chr(92) + "ud800"
        literal_line = json.dumps({"resourceSpans": [{"resource": {}, "scopeSpans": [
            {"spans": [_span(literal)]},
        ]}]})
        spans, counters = flatten_lines([emoji_line, literal_line])
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(spans[0]["name"], "\U0001F600")
        self.assertEqual(spans[1]["name"], literal)


def _good_with_extra_key(extra_member):
    """A valid resourceSpans line with one more top-level member appended as text."""
    base = _good_line()
    assert base.endswith("}")
    return base[:-1] + ", " + extra_member + "}"


class SurrogateWalkTests(unittest.TestCase):
    # The surrogate check walks the decoded keys and strings. It never
    # re-serializes the payload, so json.dumps must not run on the line path.

    def test_no_json_dumps_used(self):
        # Build every input before patching json.dumps, because the patch also
        # affects this test's own json.dumps calls.
        ok_line = _surrogate_line("attribute stringValue", "\U0001F600")
        bad_line = _surrogate_line("attribute stringValue", "\ud800")
        self.assertRegex(ok_line, r"\\ud83d\\ude00")
        self.assertRegex(bad_line, r"\\ud800")
        for kind in ("str", "bytes"):
            ok_item = ok_line if kind == "str" else ok_line.encode("utf-8")
            bad_item = bad_line if kind == "str" else bad_line.encode("utf-8")
            with self.subTest(kind=kind):
                with mock.patch.object(
                    adapter_module.json, "dumps",
                    side_effect=AssertionError("json.dumps must not be called"),
                ):
                    spans, counters = flatten_lines([ok_item])
                    bad_spans, bad_counters = flatten_lines([bad_item])
                self.assertEqual(len(spans), 1)
                self.assertEqual(counters["malformed_lines"], 0)
                self.assertEqual(len(bad_spans), 0)
                self.assertEqual(bad_counters["malformed_lines"], 1)

    def test_lone_surrogate_in_object_key_rejected(self):
        line = _good_with_extra_key('"\\udc00": 1')
        self.assertIn("\\udc00", line)
        spans, counters = flatten_lines([line])
        self.assertEqual(counters["malformed_lines"], 1)
        self.assertEqual(len(spans), 0)

    def test_deep_nesting_walk(self):
        depth = 2000
        nest_lone = "[" * depth + '"\\ud800"' + "]" * depth
        nest_pair = "[" * depth + '"\\ud83d\\ude00"' + "]" * depth
        lone_line = _good_with_extra_key('"pad": ' + nest_lone)
        pair_line = _good_with_extra_key('"pad": ' + nest_pair)
        spans, counters = flatten_lines([lone_line])
        self.assertEqual(counters["malformed_lines"], 1)
        self.assertEqual(len(spans), 0)
        spans, counters = flatten_lines([pair_line])
        self.assertEqual(counters["malformed_lines"], 0)
        self.assertEqual(len(spans), 1)


class AttributeDedupAndCapTests(unittest.TestCase):
    # Duplicate keys: first valid value wins, later duplicates are dropped and
    # counted. The cap counts only kept keys and applies after decoding.

    def test_duplicate_keys_first_wins_and_counted(self):
        span = _span()
        span["attributes"] = [
            _kv("a", {"stringValue": "1"}),
            _kv("a", {"stringValue": "2"}),
            _kv("b", {"stringValue": "x"}),
        ]
        spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(spans[0]["attributes"], {"a": "1", "b": "x"})
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_duplicate_invalid_then_valid_same_key(self):
        # An invalid item does not claim its key: the later valid one is kept.
        span = _span()
        span["attributes"] = [
            _kv("a", {}),
            _kv("a", {"stringValue": "2"}),
        ]
        spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(spans[0]["attributes"], {"a": "2"})
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_cap_applies_after_decode(self):
        # Invalid items at the front do not use up the cap: four valid unique
        # keys are kept after six invalid items.
        with mock.patch.object(adapter_module, "MAX_ATTRIBUTES_PER_SPAN", 4):
            span = _span()
            span["attributes"] = (
                [_kv("j%d" % i, {}) for i in range(6)]
                + [_kv("k%d" % i, {"stringValue": str(i)}) for i in range(4)]
            )
            spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(spans[0]["attributes"], {"k0": "0", "k1": "1", "k2": "2", "k3": "3"})
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 6, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_cap_counts_remaining_without_decoding(self):
        # Cap 2: only the first two valid items are decoded. The other three
        # are counted as dropped without any decode call.
        with mock.patch.object(adapter_module, "MAX_ATTRIBUTES_PER_SPAN", 2):
            span = _span()
            span["attributes"] = [_kv("k%d" % i, {"stringValue": str(i)}) for i in range(5)]
            original = adapter_module._decode_any_value
            with mock.patch.object(adapter_module, "_decode_any_value", wraps=original) as spy:
                spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(spans[0]["attributes"], {"k0": "0", "k1": "1"})
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 3, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_resource_attributes_dedup(self):
        # Same duplicate rule on resource attributes: first value wins.
        line = _line([{
            "resource": {"attributes": [
                _kv("service.name", {"stringValue": "claude-code"}),
                _kv("service.name", {"stringValue": "other"}),
            ]},
            "scopeSpans": [{"spans": [_span()]}],
        }])
        spans, counters = flatten_lines([line])
        self.assertEqual(spans[0]["resource_attributes"], {"service.name": "claude-code"})
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })


class RegressionGapTests(unittest.TestCase):
    ZERO_COUNTERS = {
        "lines_read": 0, "malformed_lines": 0, "spans": 0, "skipped_items": 0,
        "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
        "stream_truncated": 0,
    }

    def test_invalid_end_time_counted(self):
        # A present but non-digit endTimeUnixNano is None and counted once; the
        # valid start time is kept.
        span = _timed_span("a" * 16, "", startTimeUnixNano="1000", endTimeUnixNano="abc")
        spans, counters = flatten_lines([_line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 1, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["startTimeUnixNano"], 1000)
        self.assertIsNone(spans[0]["endTimeUnixNano"])

    def test_double_overflow_dropped(self):
        # Behavior predicted from the code and asserted here:
        # (a) wire text 1e309 is a float literal; _strict_float maps it to inf
        #     and raises, so the WHOLE line is malformed (not just the attribute).
        # (b) a huge integer literal as doubleValue parses as an int; float()
        #     raises OverflowError inside _decode_any_value, so only that
        #     attribute is dropped (dropped_attributes 1) and the line is kept.
        template = json.dumps({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [
            dict(_span(), attributes=[_kv("a.dbl", {"doubleValue": "PLACEHOLDER"})]),
        ]}]}]})
        float_line = template.replace('"PLACEHOLDER"', "1e309")
        self.assertIn("1e309", float_line)
        spans, counters = flatten_lines([_good_line(), float_line, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 3, "malformed_lines": 1, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(len(spans), 2)

        with self.subTest("huge integer literal as doubleValue: attribute dropped, line kept"):
            big = json.dumps({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [
                dict(_span(), attributes=[_kv("a.dbl", {"doubleValue": 10 ** 400})]),
            ]}]}]})
            spans, counters = flatten_lines([big])
            self.assertEqual(counters, {
                "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
                "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
                "stream_truncated": 0,
            })
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0]["attributes"], {})

    def test_int_underflow_dropped(self):
        # One below int64 minimum is rejected; the int64 minimum itself is kept.
        below = _line([{"resource": {}, "scopeSpans": [{"spans": [
            dict(_span(), attributes=[_kv("a.int", {"intValue": "-9223372036854775809"})]),
        ]}]}])
        spans, counters = flatten_lines([below])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertEqual(spans[0]["attributes"], {})

        at_min = _line([{"resource": {}, "scopeSpans": [{"spans": [
            dict(_span(), attributes=[_kv("a.int", {"intValue": "-9223372036854775808"})]),
        ]}]}])
        spans, counters = flatten_lines([at_min])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        self.assertIs(type(spans[0]["attributes"]["a.int"]), int)
        self.assertEqual(spans[0]["attributes"]["a.int"], -(2 ** 63))

    def test_bom_only_lines(self):
        # A line that is only U+FEFF is blank after one BOM is removed. It is
        # skipped and not counted, so every counter stays 0.
        for item in ("\ufeff", "\ufeff".encode("utf-8")):
            with self.subTest(kind=type(item).__name__):
                spans, counters = flatten_lines([item])
                self.assertEqual(spans, [])
                self.assertEqual(counters, self.ZERO_COUNTERS)

    def test_secret_marker_never_leaks(self):
        # Synthetic marker. Each hostile line must be handled without raising,
        # and the marker must never reach counters or kept span records.
        marker = "SYNTH_SECRET_TOKEN_0001"
        limit = 4096
        good = _good_line()
        self.assertLess(len(good.encode("utf-8")), limit)

        invalid_json = '{"x": "' + marker + '"'  # unterminated: malformed
        bogus_any = _line([{"resource": {}, "scopeSpans": [{"spans": [
            dict(_span(), attributes=[_kv("a.bogus", {"bogusValue": marker})]),
        ]}]}])  # attribute dropped, span kept
        bad_trace = _line([{"resource": {}, "scopeSpans": [{"spans": [
            dict(_span(), traceId={"v": marker}),
        ]}]}])  # non-str identity: span skipped
        oversize = json.dumps({"resourceSpans": [], "pad": marker * 300})
        self.assertGreater(len(oversize.encode("utf-8")), limit)
        surrogate_span = _span()
        surrogate_span["name"] = "\ud800"
        surrogate_span["attributes"] = [_kv("a.key", {"stringValue": marker})]
        surrogate = _line([{"resource": {}, "scopeSpans": [{"spans": [surrogate_span]}]}])
        self.assertRegex(surrogate, r"\\u[dD]800")  # JSON-escaped lone surrogate
        events_span = _span()
        events_span["events"] = [{"name": marker, "timeUnixNano": "1000", "attributes": []}]
        events = _line([{"resource": {}, "scopeSpans": [{"spans": [events_span]}]}])

        lines = [invalid_json, bogus_any, bad_trace, oversize, surrogate, events, good]
        with mock.patch.object(adapter_module, "MAX_LINE_BYTES", limit):
            for kind in ("str", "bytes"):
                with self.subTest(kind=kind):
                    items = lines if kind == "str" else [text.encode("utf-8") for text in lines]
                    spans, counters = flatten_lines(items)
                    self.assertEqual(counters, {
                        "lines_read": 7, "malformed_lines": 3, "spans": 3, "skipped_items": 1,
                        "dropped_attributes": 1, "invalid_timestamps": 0, "spans_over_limit": 0,
                        "stream_truncated": 0,
                    })
                    self.assertEqual(len(spans), 3)
                    self.assertNotIn(marker, repr(counters))
                    self.assertNotIn(marker, json.dumps(spans, ensure_ascii=True))


class InputGuardAndWhitespaceTests(unittest.TestCase):
    # ADJ-R2-P3-01: every bytes-like bare input is a caller bug, not a stream.
    # ADJ-R2-P3-03: only RFC 8259 JSON whitespace is stripped from a line.

    def test_bytearray_and_memoryview_lines_rejected(self):
        data = _good_line().encode("utf-8")
        for make in (bytearray, memoryview):
            with self.subTest(kind=make.__name__):
                with self.assertRaises(TypeError):
                    flatten_lines(make(data))

    def test_non_json_whitespace_is_not_stripped(self):
        # json.loads rejects these leading characters, so each line is malformed.
        for ws in ("\u00a0", "\u2028", "\u2029", "\x0b", "\x85"):
            line = ws + _good_line()
            with self.subTest(ws=repr(ws), kind="str"):
                spans, counters = flatten_lines([line])
                self.assertEqual(counters, {
                    "lines_read": 1, "malformed_lines": 1, "spans": 0, "skipped_items": 0,
                    "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
                    "stream_truncated": 0,
                })
                self.assertEqual(spans, [])
            with self.subTest(ws=repr(ws), kind="bytes"):
                spans, counters = flatten_lines([line.encode("utf-8")])
                self.assertEqual(counters, {
                    "lines_read": 1, "malformed_lines": 1, "spans": 0, "skipped_items": 0,
                    "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
                    "stream_truncated": 0,
                })
                self.assertEqual(spans, [])

    def test_form_feed_only_line_is_malformed(self):
        # A form feed is not JSON whitespace: the line is read and malformed, not blank.
        spans, counters = flatten_lines(["\x0c"])
        self.assertEqual(spans, [])
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 1, "spans": 0, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })

    def test_json_whitespace_still_stripped(self):
        # Space, tab, CR and LF around a good line are stripped; a line made only
        # of them is blank and not counted.
        spans, counters = flatten_lines([" \t" + _good_line() + "\r\n"])
        self.assertEqual(len(spans), 1)
        self.assertEqual(counters, {
            "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })
        spans, counters = flatten_lines([" \t\r\n"])
        self.assertEqual(spans, [])
        self.assertEqual(counters, {
            "lines_read": 0, "malformed_lines": 0, "spans": 0, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
            "stream_truncated": 0,
        })


# M7-B step 3 S1: sanitization helpers. Hostile values are synthetic markers
# only. The JWT is built from obviously fake parts.
SYNTH_JWT = "eyJ" + "A" * 12 + "." + "B" * 12 + "." + "C" * 10

HOSTILE_TEXT = (
    "synth.user@example.test",
    "sk-ant-SYNTH_SECRET_0001",
    "Bearer SYNTH_SECRET_0001",
    "ghp_" + "A" * 36,
    "AKIA" + "0" * 16,
    SYNTH_JWT,
    "-----BEGIN RSA PRIVATE KEY-----",
    "C:\\Users\\synthetic\\x",
    "/home/synthetic/.ssh/id_rsa",
    "\\\\server\\share",
    "token=SYNTH",
    "password: x",
    "A" * 257,
    "\x1b[31m",
    "\ud800",
    "",
    "Bash(rm -rf /)",
    "text with spaces",
    "[REDACTED]",
)

BENIGN_TEXT = (
    "Read",
    "Edit",
    "Bash",
    "WebFetch",
    "mcp__github__get_issue",
    "claude-opus-4-1-20250805",
    "claude-sonnet-4-5",
    "toolu_01ABCdef",
    "tokenizer-v2",
    "gen_ai.usage",
    "Task.v2:beta",
    "fleet@v2",
    "x+y",
    "session-42",
    "v1.2.3",
    "node:18",
    "A",
    "A" * 256,
)

NON_STRING_INPUTS = (None, 1, 1.5, float("nan"), b"Read", ["Read"], {"a": 1}, object())


class Step3SanitizeTests(unittest.TestCase):
    # S1 item (1): values copied from the bridge are equal to the bridge.
    def test_patterns_and_flags_match_bridge(self):
        for name in ("EMAIL_RE", "SECRET_VALUE_RE", "ABSOLUTE_PATH_RE", "SAFE_METADATA_RE"):
            with self.subTest(name=name):
                ours = getattr(_sanitize, name)
                theirs = getattr(bridge, name)
                self.assertEqual(ours.pattern, theirs.pattern)
                self.assertEqual(ours.flags, theirs.flags)

    def test_error_enums_match_bridge(self):
        self.assertIsInstance(_sanitize.SAFE_ERROR_TYPES, frozenset)
        self.assertIsInstance(_sanitize.SAFE_ERROR_MESSAGES, frozenset)
        self.assertEqual(_sanitize.SAFE_ERROR_TYPES, frozenset(bridge.SAFE_ERROR_TYPES))
        self.assertEqual(_sanitize.SAFE_ERROR_MESSAGES, frozenset(bridge.SAFE_ERROR_MESSAGES))
        self.assertEqual(len(_sanitize.SAFE_ERROR_TYPES), 6)
        self.assertEqual(len(_sanitize.SAFE_ERROR_MESSAGES), 4)

    def test_max_chars_matches_bridge(self):
        self.assertEqual(_sanitize.MAX_SAFE_STRING_CHARS, 256)
        self.assertEqual(_sanitize.MAX_SAFE_STRING_CHARS, bridge.MAX_SAFE_STRING_CHARS)

    def test_real_exporter_messages_and_types_are_in_sets(self):
        # Drift guard: every message/type the bridge's real exporter emits is
        # an approved member here, so the adapter can reuse them unchanged.
        for kind in ("llm", "tool", "other"):
            with self.subTest(kind=kind):
                self.assertIn(bridge._real_exporter_error_message(kind),
                              _sanitize.SAFE_ERROR_MESSAGES)
                self.assertIn(bridge._real_exporter_error_type(kind),
                              _sanitize.SAFE_ERROR_TYPES)

    def test_sanitize_imports_stdlib_only(self):
        import ast
        source = Path(_sanitize.__file__).read_text(encoding="ascii")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative imports")
                modules.add((node.module or "").split(".")[0])
        self.assertLessEqual(modules, {"__future__", "math", "re"})
        self.assertTrue(source.isascii())

    # S1 item (2): hostile values are rejected, benign accepted, and the
    # safe_text acceptance matches bridge._safe_text exactly.
    def test_hostile_text_rejected_by_safe_text_and_bridge(self):
        for value in HOSTILE_TEXT:
            with self.subTest(value=repr(value)[:60]):
                self.assertFalse(_sanitize.safe_text(value))
                with self.assertRaises(bridge.BridgeError):
                    bridge._safe_text(value, "synthetic")

    def test_benign_text_accepted_by_safe_text_and_bridge(self):
        for value in BENIGN_TEXT:
            with self.subTest(value=value[:60]):
                self.assertTrue(_sanitize.safe_text(value))
                self.assertEqual(bridge._safe_text(value, "synthetic"), value)

    def test_safe_text_matches_bridge_on_corpus(self):
        for value in HOSTILE_TEXT + BENIGN_TEXT:
            with self.subTest(value=repr(value)[:60]):
                try:
                    bridge._safe_text(value, "synthetic")
                    bridge_accepts = True
                except bridge.BridgeError:
                    bridge_accepts = False
                self.assertEqual(_sanitize.safe_text(value), bridge_accepts)

    def test_safe_text_non_string_inputs_are_false(self):
        for value in NON_STRING_INPUTS:
            with self.subTest(value=repr(value)[:40]):
                self.assertIs(_sanitize.safe_text(value), False)

    def test_safe_text_length_boundary(self):
        self.assertTrue(_sanitize.safe_text("A" * 256))
        self.assertFalse(_sanitize.safe_text("A" * 257))

    # S1 item (3): safe_count.
    def test_safe_count_accepts(self):
        self.assertEqual(_sanitize.safe_count(0), 0)
        self.assertEqual(_sanitize.safe_count(42), 42)
        self.assertEqual(_sanitize.safe_count(2 ** 63 - 1), 2 ** 63 - 1)
        self.assertEqual(_sanitize.safe_count(0.0), 0)
        self.assertIs(type(_sanitize.safe_count(12.0)), int)
        self.assertEqual(_sanitize.safe_count(12.0), 12)
        self.assertEqual(_sanitize.safe_count(float(2 ** 62)), 2 ** 62)

    def test_safe_count_rejects(self):
        rejected = (
            -1, -1.0, True, False, "12", 1.5, float("nan"), float("inf"),
            float("-inf"), None, 2 ** 63, float(2 ** 63), [], b"1",
        )
        for value in rejected:
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(_sanitize.safe_count(value))

    # S1 item (4): safe_uuid.
    def test_safe_uuid_accepts_canonical_either_case(self):
        self.assertTrue(_sanitize.safe_uuid("123e4567-e89b-12d3-a456-426614174000"))
        self.assertTrue(_sanitize.safe_uuid("123E4567-E89B-12D3-A456-426614174000"))
        self.assertTrue(_sanitize.safe_uuid("123e4567-E89B-12d3-A456-426614174000"))

    def test_safe_uuid_rejects_everything_else(self):
        rejected = (
            "{123e4567-e89b-12d3-a456-426614174000}",
            "123e4567-e89b-12d3-a456-42661417400",
            "123e4567-e89b-12d3-a456-4266141740000",
            "123e4567-e89b-12d3-a456-426614174000 token=SYNTH",
            "123e4567-e89b-12d3-a456-426614174000\n",
            "g23e4567-e89b-12d3-a456-426614174000",
            "123e4567e89b12d3a456426614174000",
            "",
            None,
            b"123e4567-e89b-12d3-a456-426614174000",
        )
        for value in rejected:
            with self.subTest(value=repr(value)[:60]):
                self.assertIs(_sanitize.safe_uuid(value), False)

    # S1 item (5): safe_model and safe_tool_name.
    def test_safe_model_rules(self):
        self.assertTrue(_sanitize.safe_model("claude-opus-4-1-20250805"))
        self.assertFalse(_sanitize.safe_model(
            "arn:aws:bedrock:us-east-1:000000000000:foundation-model"))
        self.assertFalse(_sanitize.safe_model(
            "ARN:aws:bedrock:us-east-1:000000000000:foundation-model"))
        self.assertFalse(_sanitize.safe_model("sk-ant-SYNTH_SECRET_0001"))
        self.assertFalse(_sanitize.safe_model("A" * 257))
        self.assertFalse(_sanitize.safe_model(""))
        self.assertFalse(_sanitize.safe_model(None))

    def test_safe_tool_name_rules(self):
        self.assertTrue(_sanitize.safe_tool_name("Read"))
        self.assertTrue(_sanitize.safe_tool_name("Bash"))
        rejected = (
            "mcp__privsrv__x",
            "mcp__github__get_issue",
            "C:\\Users\\synthetic\\secret.txt",
            "ghp_" + "A" * 36,
            "Bash(rm -rf /)",
            "",
            None,
        )
        for value in rejected:
            with self.subTest(value=repr(value)[:60]):
                self.assertIs(_sanitize.safe_tool_name(value), False)

    def test_helpers_never_raise(self):
        helpers = (
            _sanitize.safe_text, _sanitize.safe_count, _sanitize.safe_uuid,
            _sanitize.safe_model, _sanitize.safe_tool_name,
        )
        for helper in helpers:
            for value in NON_STRING_INPUTS + HOSTILE_TEXT:
                with self.subTest(helper=helper.__name__, value=repr(value)[:40]):
                    helper(value)


# M7-B step 3 S2: attribute sources, strip lists and constants in _vocab.
class Step3VocabTests(unittest.TestCase):
    SESSION_TYPES = (
        "claude_code.interaction",
        "claude_code.llm_request",
        "claude_code.tool",
        "claude_code.hook",
    )

    def _rows(self):
        for span_type, rows in _vocab.ATTRIBUTE_SOURCES.items():
            for row in rows:
                yield span_type, row

    def test_every_output_key_is_allowed_and_not_fleet(self):
        for span_type, (output_key, _sources, _kind) in self._rows():
            with self.subTest(span_type=span_type, output_key=output_key):
                self.assertIn(output_key, _vocab.OUTPUT_ATTRIBUTE_KEYS)
                self.assertFalse(output_key.startswith("fleet."))
                self.assertIn(output_key, bridge.ATTRIBUTE_ALLOWLIST)

    def test_no_source_key_is_stripped_or_folded_input(self):
        for span_type, (output_key, sources, _kind) in self._rows():
            for source in sources:
                with self.subTest(span_type=span_type, source=source):
                    self.assertFalse(_vocab.is_stripped(source))
                    self.assertNotIn(source, _vocab.FOLD_INPUT_KEYS)

    def test_every_kind_is_known(self):
        self.assertIsInstance(_vocab.ATTRIBUTE_KINDS, frozenset)
        self.assertEqual(
            _vocab.ATTRIBUTE_KINDS,
            frozenset({"uuid", "seq", "model", "count", "tool_name", "text"}),
        )
        for span_type, (output_key, _sources, kind) in self._rows():
            with self.subTest(span_type=span_type, output_key=output_key):
                self.assertIn(kind, _vocab.ATTRIBUTE_KINDS)

    def test_sources_keys_match_span_map_and_folds_are_disjoint(self):
        self.assertEqual(set(_vocab.ATTRIBUTE_SOURCES), set(SPAN_TYPE_TO_V1))
        self.assertIsInstance(_vocab.FOLD_TYPES, frozenset)
        self.assertEqual(
            _vocab.FOLD_TYPES,
            frozenset({"claude_code.tool.execution", "claude_code.tool.blocked_on_user"}),
        )
        self.assertEqual(set(_vocab.FOLD_TYPES) & set(SPAN_TYPE_TO_V1), set())

    def test_session_id_row_present_for_all_four_types(self):
        for span_type in self.SESSION_TYPES:
            with self.subTest(span_type=span_type):
                rows = _vocab.ATTRIBUTE_SOURCES[span_type]
                self.assertIn(
                    ("session.id", ("session.id",), "uuid"), rows,
                )

    def test_attribute_sources_are_read_only_tuples(self):
        self.assertIsInstance(_vocab.ATTRIBUTE_SOURCES, type(SPAN_TYPE_TO_V1))
        with self.assertRaises(TypeError):
            _vocab.ATTRIBUTE_SOURCES["claude_code.hook"] = ()
        with self.assertRaises(TypeError):
            del _vocab.ATTRIBUTE_SOURCES["claude_code.hook"]
        for span_type, rows in _vocab.ATTRIBUTE_SOURCES.items():
            with self.subTest(span_type=span_type):
                self.assertIsInstance(rows, tuple)
                for row in rows:
                    self.assertIsInstance(row, tuple)
                    self.assertEqual(len(row), 3)
                    self.assertIsInstance(row[1], tuple)

    def test_strip_containers_are_read_only(self):
        self.assertIsInstance(_vocab.STRIP_KEYS, frozenset)
        self.assertIsInstance(_vocab.STRIP_PREFIXES, tuple)
        self.assertIsInstance(_vocab.STRIP_SUFFIXES, tuple)
        self.assertIsInstance(_vocab.CONTENT_KEYS, frozenset)
        self.assertFalse(hasattr(_vocab.STRIP_KEYS, "add"))
        self.assertFalse(hasattr(_vocab.CONTENT_KEYS, "add"))

    def test_strip_keys_contain_identity_and_host_keys(self):
        required = {
            "user.email", "user.id", "user.account_uuid", "user.account_id",
            "organization.id", "ccr.session.id", "host.name", "host.id",
            "terminal.type", "user_prompt",
        }
        self.assertEqual(required - set(_vocab.STRIP_KEYS), set())

    def test_is_stripped_true_cases(self):
        for key in (
            "vcs.repository.url", "workflow.name", "user.anything",
            "host.arch", "organization.name", "tool_input_truncated",
            "user_prompt_original_length",
        ):
            with self.subTest(key=key):
                self.assertIs(_vocab.is_stripped(key), True)

    def test_is_stripped_false_cases(self):
        for key in ("session.id", "input_tokens", "tool_name", None, 5):
            with self.subTest(key=repr(key)):
                self.assertIs(_vocab.is_stripped(key), False)

    def test_content_keys_are_all_stripped(self):
        for key in _vocab.CONTENT_KEYS:
            with self.subTest(key=key):
                self.assertTrue(_vocab.is_stripped(key))

    def test_agent_name_observation_source_and_producer(self):
        self.assertEqual(_vocab.AGENT_NAME, "claude-code")
        self.assertEqual(_vocab.OBSERVATION_SOURCE, "otel-export")
        self.assertEqual(_vocab.PRODUCER, "claude-code")
        self.assertTrue(_sanitize.safe_text(_vocab.AGENT_NAME))
        self.assertTrue(_sanitize.safe_text(_vocab.OBSERVATION_SOURCE))
        self.assertIsNotNone(bridge.PRODUCER_RE.fullmatch(_vocab.PRODUCER))

    def test_redacted_prompt_constant(self):
        self.assertEqual(_vocab.REDACTED_PROMPT, "<REDACTED>")


# S3: flatten passes the raw present "status" value through whatever its type.
CLEAN_ONE_SPAN_COUNTERS = {
    "lines_read": 1, "malformed_lines": 0, "spans": 1, "skipped_items": 0,
    "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
    "stream_truncated": 0,
}


def _status_line(status_value=None, drop_key=False):
    # One clean synthetic span; the "status" key is set to status_value or removed.
    span_dict = _span()
    if drop_key:
        del span_dict["status"]
    else:
        span_dict["status"] = status_value
    return _line([{"resource": {}, "scopeSpans": [{"spans": [span_dict]}]}])


class RawStatusPassthroughTests(unittest.TestCase):
    def _assert_raw_status(self, status_value):
        spans, counters = flatten_lines([_status_line(status_value)])
        self.assertEqual(counters, CLEAN_ONE_SPAN_COUNTERS)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["status"], status_value)
        self.assertIs(type(spans[0]["status"]), type(status_value))

    def test_str_status_is_raw(self):
        self._assert_raw_status("oops")

    def test_list_status_is_raw(self):
        self._assert_raw_status([])

    def test_int_status_is_raw(self):
        self._assert_raw_status(2)

    def test_bool_status_is_raw(self):
        self._assert_raw_status(True)

    def test_dict_status_is_raw(self):
        self._assert_raw_status({"code": 2, "message": "x"})

    def test_null_status_is_none(self):
        self._assert_raw_status(None)

    def test_absent_status_is_none(self):
        spans, counters = flatten_lines([_status_line(drop_key=True)])
        self.assertEqual(counters, CLEAN_ONE_SPAN_COUNTERS)
        self.assertEqual(len(spans), 1)
        self.assertIsNone(spans[0]["status"])


S4_TRACE = "0" * 31 + "1"
S4_SPAN_1 = "0" * 15 + "1"
S4_LLM_ERROR_MSG = "LLM call failed; details withheld"
S4_TOOL_ERROR_MSG = "Tool failed; details withheld"
S4_OTHER_ERROR_MSG = "Error; details withheld"
S4_MARKER_MESSAGE = "failed C:\\Users\\synth token=SYNTH_SECRET_0001"
S4_ALLOWED_OUTPUT_KEYS = frozenset({
    "traceId", "spanId", "parentSpanId", "name", "startTimeUnixNano",
    "endTimeUnixNano", "attributes", "status",
})
S4_FORBIDDEN_OUTPUT_KEYS = frozenset({
    "resource", "events", "scope", "kind", "resource_attributes", "events_count",
})


def _sid(number):
    return format(number, "016x")


def _rec(name="claude_code.llm_request", span_id=S4_SPAN_1, trace_id=S4_TRACE,
         parent=None, status=None, attributes=None, events_count=0):
    # A flatten_lines-shaped span record (see adapter.flatten_lines docstring).
    return {
        "name": name,
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent,
        "startTimeUnixNano": 1000,
        "endTimeUnixNano": 2000,
        "attributes": dict(attributes or {}),
        "resource_attributes": {"service.name": "claude-code"},
        "events_count": events_count,
        "status": status,
    }


def _map_spans(records):
    from claude_code_plugin import mapping
    return mapping.map_spans(records)


class Step3MapSpansCoreTests(unittest.TestCase):
    """M7-B step 3 S4: mapping core (routing, ids, duplicates, projection, status)."""

    def test_four_documented_types_map_to_v1_names(self):
        records = [
            _rec("claude_code.interaction", _sid(1)),
            _rec("claude_code.llm_request", _sid(2)),
            _rec("claude_code.tool", _sid(3)),
            _rec("claude_code.hook", _sid(4)),
        ]
        spans, counters = _map_spans(records)
        self.assertEqual([s["name"] for s in spans], ["agent.run", "llm.call", "tool.exec", "hook.exec"])
        self.assertEqual(counters["spans_in"], 4)
        self.assertEqual(counters["spans_out"], 4)
        self.assertEqual(counters["unknown_span_types"], 0)

    def test_unknown_and_fold_names_are_dropped_and_counted(self):
        dropped_names = [
            "agent.run",
            "claude_code.tool.execution",
            "claude_code.tool.blocked_on_user",
            "foo",
            "claude_code.Tool",
            "claude_code.tool ",
            "",
            None,
            5,
            ["claude_code.tool"],
        ]
        records = [_rec(name, _sid(i + 1)) for i, name in enumerate(dropped_names)]
        spans, counters = _map_spans(records)
        self.assertEqual(spans, [])
        self.assertEqual(counters["spans_in"], len(dropped_names))
        self.assertEqual(counters["spans_out"], 0)
        self.assertEqual(counters["unknown_span_types"], len(dropped_names))

    def test_span_type_attribute_is_ignored_routing_uses_name(self):
        spoof = _rec("claude_code.tool", _sid(1), attributes={"span.type": "agent.run"})
        reverse = _rec("agent.run", _sid(2), attributes={"span.type": "claude_code.tool"})
        spans, counters = _map_spans([spoof, reverse])
        self.assertEqual([s["name"] for s in spans], ["tool.exec"])
        self.assertEqual(counters["unknown_span_types"], 1)

    def test_bad_identities_drop_the_span_and_count_invalid_ids(self):
        records = [
            _rec(trace_id="0" * 31),                          # 31 hex trace
            _rec(trace_id="g" * 32),                          # non-hex trace
            _rec(trace_id="SYNTH_SECRET_0001"),               # marker string
            _rec(span_id="0" * 15),                           # 15 hex span
            _rec(parent="0" * 15),                            # 15 hex parent
            _rec(parent="0" * 17),                            # 17 hex parent
            _rec(parent=5),                                   # non-str parent
            "not a dict",                                     # non-dict record
        ]
        spans, counters = _map_spans(records)
        self.assertEqual(spans, [])
        self.assertEqual(counters["invalid_ids"], 8)
        self.assertEqual(counters["spans_in"], 8)
        self.assertNotIn("SYNTH_SECRET_0001", repr(counters))

    def test_empty_and_none_parent_are_roots_and_omitted(self):
        records = [_rec(parent=None, span_id=_sid(1)), _rec(parent="", span_id=_sid(2))]
        spans, counters = _map_spans(records)
        self.assertEqual(counters["invalid_ids"], 0)
        self.assertEqual(len(spans), 2)
        for span in spans:
            self.assertNotIn("parentSpanId", span)

    def test_valid_parent_is_kept(self):
        spans, _ = _map_spans([_rec(parent=_sid(9), span_id=_sid(1))])
        self.assertEqual(spans[0]["parentSpanId"], _sid(9))

    def test_duplicate_span_first_wins(self):
        first = _rec("claude_code.llm_request", _sid(1))
        second = _rec("claude_code.tool", _sid(1))
        spans, counters = _map_spans([first, second])
        self.assertEqual([s["name"] for s in spans], ["llm.call"])
        self.assertEqual(counters["duplicate_spans"], 1)
        self.assertEqual(counters["spans_out"], 1)

    def test_same_span_id_in_two_traces_are_both_kept(self):
        other_trace = "0" * 30 + "02"
        spans, counters = _map_spans([
            _rec(span_id=_sid(1), trace_id=S4_TRACE),
            _rec(span_id=_sid(1), trace_id=other_trace),
        ])
        self.assertEqual(len(spans), 2)
        self.assertEqual(counters["duplicate_spans"], 0)

    def test_output_keys_are_the_projection_only(self):
        record = _rec(
            "claude_code.tool", _sid(1), parent=_sid(2),
            status={"code": 2, "message": S4_MARKER_MESSAGE},
            attributes={"tool_name": "Read", "span.type": "x"},
            events_count=3,
        )
        spans, _ = _map_spans([record])
        self.assertEqual(len(spans), 1)
        keys = set(spans[0])
        self.assertTrue(keys <= S4_ALLOWED_OUTPUT_KEYS, keys)
        self.assertFalse(keys & S4_FORBIDDEN_OUTPUT_KEYS, keys)
        self.assertEqual(spans[0]["startTimeUnixNano"], 1000)
        self.assertEqual(spans[0]["endTimeUnixNano"], 2000)

    def test_missing_times_are_omitted(self):
        record = _rec(span_id=_sid(1))
        record["startTimeUnixNano"] = None
        record["endTimeUnixNano"] = None
        spans, _ = _map_spans([record])
        self.assertNotIn("startTimeUnixNano", spans[0])
        self.assertNotIn("endTimeUnixNano", spans[0])

    def test_constant_attributes_on_every_span(self):
        spans, _ = _map_spans([_rec(span_id=_sid(1)), _rec("claude_code.hook", _sid(2))])
        for span in spans:
            self.assertEqual(span["attributes"], {"agent.name": "claude-code", "observation.source": "otel-export"})

    def test_ok_and_unknown_status(self):
        records = [
            _rec(span_id=_sid(1), status={"code": 1}),
            _rec(span_id=_sid(2), status={"code": 0}),
            _rec(span_id=_sid(3), status={}),
            _rec(span_id=_sid(4), status={"code": None}),
            _rec(span_id=_sid(5), status=None),
        ]
        spans, counters = _map_spans(records)
        self.assertEqual([s["status"] for s in spans], ["ok", "unknown", "unknown", "unknown", "unknown"])
        self.assertEqual(counters["invalid_status"], 0)

    def test_error_status_uses_safe_message_only(self):
        cases = [
            ("claude_code.llm_request", S4_LLM_ERROR_MSG, "LLMError"),
            ("claude_code.tool", S4_TOOL_ERROR_MSG, "ToolError"),
            ("claude_code.hook", S4_OTHER_ERROR_MSG, "Unknown"),
        ]
        for index, (name, message, error_type) in enumerate(cases):
            record = _rec(name, _sid(index + 1), status={"code": 2, "message": S4_MARKER_MESSAGE})
            spans, _ = _map_spans([record])
            self.assertEqual(spans[0]["status"], {"code": 2, "message": message})
            self.assertEqual(spans[0]["attributes"]["error.type"], error_type)

    def test_marker_from_status_message_never_reaches_output_or_counters(self):
        records = [
            _rec("claude_code.tool", _sid(1), status={"code": 2, "message": S4_MARKER_MESSAGE}),
            _rec("claude_code.tool", _sid(2), status={"code": 2, "message": S4_MARKER_MESSAGE, "extra": "SYNTH_SECRET_0001"}),
        ]
        spans, counters = _map_spans(records)
        self.assertEqual(len(spans), 2)
        self.assertNotIn("SYNTH_SECRET_0001", json.dumps(spans, ensure_ascii=True))
        self.assertNotIn("SYNTH_SECRET_0001", json.dumps(counters, ensure_ascii=True))
        self.assertNotIn("SYNTH_SECRET_0001", repr(counters))

    def test_invalid_status_codes_and_shapes_count_invalid_status(self):
        invalid_statuses = [
            {"code": True},
            {"code": "2"},
            {"code": 7},
            {"code": 2.0},
            "oops",
            [],
        ]
        records = [_rec(span_id=_sid(i + 1), status=value) for i, value in enumerate(invalid_statuses)]
        spans, counters = _map_spans(records)
        self.assertEqual(len(spans), len(invalid_statuses))
        self.assertEqual(counters["invalid_status"], len(invalid_statuses))
        for span in spans:
            self.assertEqual(span["status"], "unknown")
            self.assertNotIn("error.type", span["attributes"])

    def test_success_true_false_rule_for_llm_and_tool(self):
        records = [
            _rec("claude_code.llm_request", _sid(1), attributes={"success": True}),          # ok
            _rec("claude_code.llm_request", _sid(2), attributes={"success": False}),         # error
            _rec("claude_code.llm_request", _sid(3), status={"code": 1},
                 attributes={"success": False}),                                              # error
            _rec("claude_code.llm_request", _sid(4), status={"code": 0},
                 attributes={"success": True}),                                               # ok
            _rec("claude_code.tool", _sid(5), attributes={"success": False}),                 # error
            _rec("claude_code.tool", _sid(6), attributes={"success": True}),                  # ok
            _rec("claude_code.llm_request", _sid(7), status={"code": 2},
                 attributes={"success": True}),                                               # error, code wins
            _rec("claude_code.llm_request", _sid(8), status={"code": 7},
                 attributes={"success": True}),                                               # invalid, then ok
            _rec("claude_code.interaction", _sid(9), attributes={"success": False}),         # no rule: unknown
        ]
        spans, counters = _map_spans(records)
        statuses = [s["status"] for s in spans]
        self.assertEqual(statuses[0], "ok")
        self.assertEqual(statuses[1], {"code": 2, "message": S4_LLM_ERROR_MSG})
        self.assertEqual(statuses[2], {"code": 2, "message": S4_LLM_ERROR_MSG})
        self.assertEqual(statuses[3], "ok")
        self.assertEqual(statuses[4], {"code": 2, "message": S4_TOOL_ERROR_MSG})
        self.assertEqual(statuses[5], "ok")
        self.assertEqual(statuses[6], {"code": 2, "message": S4_LLM_ERROR_MSG})
        self.assertEqual(statuses[7], "ok")
        self.assertEqual(statuses[8], "unknown")
        self.assertEqual(spans[1]["attributes"]["error.type"], "LLMError")
        self.assertEqual(spans[4]["attributes"]["error.type"], "ToolError")
        self.assertNotIn("error.type", spans[8]["attributes"])
        self.assertEqual(counters["invalid_status"], 1)

    def test_events_dropped_sums_kept_span_counts_only(self):
        records = [
            _rec("claude_code.llm_request", _sid(1), events_count=2),
            _rec("claude_code.tool", _sid(2), events_count=3),
            _rec("claude_code.hook", _sid(3), events_count=-1),
            _rec("claude_code.hook", _sid(4), events_count=True),
            _rec("claude_code.hook", _sid(5), events_count="4"),
            _rec("claude_code.hook", _sid(6), events_count=1.5),
            _rec("claude_code.hook", _sid(7), events_count=None),
            _rec("claude_code.llm_request", _sid(1), events_count=100),   # duplicate: not counted
            _rec("foo", _sid(8), events_count=100),                       # unknown: not counted
        ]
        del records[4]["events_count"]
        spans, counters = _map_spans(records)
        self.assertEqual(len(spans), 7)
        self.assertEqual(counters["events_dropped"], 5)
        self.assertEqual(counters["duplicate_spans"], 1)
        self.assertEqual(counters["unknown_span_types"], 1)

    def test_output_order_is_input_order(self):
        records = [_rec(span_id=_sid(3)), _rec("foo", _sid(9)), _rec(span_id=_sid(1)), _rec(span_id=_sid(2))]
        spans, _ = _map_spans(records)
        self.assertEqual([s["spanId"] for s in spans], [_sid(3), _sid(1), _sid(2)])

    def test_non_dict_records_count_as_invalid_ids(self):
        spans, counters = _map_spans([None, "x", 5, []])
        self.assertEqual(spans, [])
        self.assertEqual(counters["invalid_ids"], 4)
        self.assertEqual(counters["spans_in"], 4)

    def test_empty_input_gives_all_counters_as_ints(self):
        spans, counters = _map_spans([])
        self.assertEqual(spans, [])
        self.assertEqual(counters, {
            "spans_in": 0, "spans_out": 0, "unknown_span_types": 0, "invalid_ids": 0,
            "duplicate_spans": 0, "invalid_status": 0, "events_dropped": 0,
        })

    def test_every_output_span_and_the_whole_list_pass_bridge_validation(self):
        records = [
            _rec("claude_code.interaction", _sid(1)),
            _rec("claude_code.llm_request", _sid(2), parent=_sid(1),
                 status={"code": 2, "message": S4_MARKER_MESSAGE}, events_count=1),
            _rec("claude_code.tool", _sid(3), parent=_sid(1), status={"code": 1}),
            _rec("claude_code.hook", _sid(4), parent=_sid(3), attributes={"success": False}),
        ]
        spans, _ = _map_spans(records)
        self.assertEqual(len(spans), 4)
        for span in spans:
            bridge._validate_flat_spans([span])
        bridge._validate_flat_spans(spans)

    def test_flatten_lines_then_map_spans_end_to_end(self):
        span = _span("claude_code.tool")
        span["status"] = {"code": 2, "message": S4_MARKER_MESSAGE}
        span["events"] = [{"name": "x"}]
        line = _line([{"resource": {}, "scopeSpans": [{"spans": [span]}]}])
        flat, flat_counters = flatten_lines([line])
        spans, counters = _map_spans(flat)
        self.assertEqual(flat_counters["spans"], 1)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["name"], "tool.exec")
        self.assertEqual(spans[0]["status"], {"code": 2, "message": S4_TOOL_ERROR_MSG})
        self.assertEqual(counters["events_dropped"], 1)
        self.assertNotIn("SYNTH_SECRET_0001", json.dumps(spans, ensure_ascii=True))
        bridge._validate_flat_spans(spans)

    def test_status_messages_and_error_types_are_in_the_sanitize_sets(self):
        from claude_code_plugin import mapping
        self.assertIn(S4_LLM_ERROR_MSG, _sanitize.SAFE_ERROR_MESSAGES)
        self.assertIn(S4_TOOL_ERROR_MSG, _sanitize.SAFE_ERROR_MESSAGES)
        self.assertIn(S4_OTHER_ERROR_MSG, _sanitize.SAFE_ERROR_MESSAGES)
        self.assertEqual(mapping.SAFE_MSG["llm"], S4_LLM_ERROR_MSG)
        self.assertEqual(mapping.SAFE_MSG["tool"], S4_TOOL_ERROR_MSG)
        self.assertEqual(mapping.SAFE_MSG["other"], S4_OTHER_ERROR_MSG)
        for kind in ("llm", "tool", "other"):
            self.assertIn(mapping.SAFE_MSG[kind], _sanitize.SAFE_ERROR_MESSAGES)
            self.assertEqual(mapping.SAFE_MSG[kind], bridge._real_exporter_error_message(kind))
        for error_type in mapping.ERROR_TYPE.values():
            self.assertIn(error_type, _sanitize.SAFE_ERROR_TYPES)
        self.assertEqual(mapping.ERROR_TYPE, {"llm": "LLMError", "tool": "ToolError", "other": "Unknown"})

    def test_id_patterns_match_bridge_by_value(self):
        from claude_code_plugin import mapping
        self.assertEqual(mapping.TRACE_ID_RE.pattern, bridge.TRACE_ID_RE.pattern)
        self.assertEqual(mapping.TRACE_ID_RE.flags, bridge.TRACE_ID_RE.flags)
        self.assertEqual(mapping.SPAN_ID_RE.pattern, bridge.SPAN_ID_RE.pattern)
        self.assertEqual(mapping.SPAN_ID_RE.flags, bridge.SPAN_ID_RE.flags)


if __name__ == "__main__":
    unittest.main()
