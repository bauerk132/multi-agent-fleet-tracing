"""M7-B step 1 tests (unittest): Claude Code adapter vocabulary.

Covers: vocabulary allowlist subsets (tests 4 and 5), malformed-line
counting in the flatten step (test 14), and strict OTLP/JSON AnyValue
decoding with numeric-only counters. Run from observability/:
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
from claude_code_plugin.adapter import flatten_lines  # noqa: E402


class ClaudeCodeVocabBridgeTests(unittest.TestCase):
    def test_output_keys_subset_of_bridge_allowlist(self):
        # Test 4: every key the adapter may emit is accepted by the bridge.
        extra = set(OUTPUT_ATTRIBUTE_KEYS) - set(bridge.ATTRIBUTE_ALLOWLIST)
        self.assertEqual(
            extra, set(),
            f"adapter emits keys the bridge would refuse: {sorted(extra)}",
        )

    def test_output_names_subset_of_bridge_span_names(self):
        # Test 5: every fleet span name the adapter may emit is canonical.
        names = set(SPAN_TYPE_TO_V1.values())
        extra = names - set(bridge.SPAN_NAMES)
        self.assertEqual(
            extra, set(),
            f"adapter emits span names the bridge would refuse: {sorted(extra)}",
        )


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
        })
        self.assertEqual(len(spans), 1)

    def test_non_dict_resource_span_entries_counted(self):
        # Non-dict resourceSpans entries (including None inside a list) are counted.
        line = _line([1, "x", None])
        spans, counters = flatten_lines([line, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 1, "skipped_items": 3,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
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
        })
        self.assertEqual(len(spans), 2)

    def test_non_text_line_is_malformed(self):
        # None and int items count as read and malformed; later lines still flatten.
        spans, counters = flatten_lines([_good_line(), None, 123, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 4, "malformed_lines": 2, "spans": 2, "skipped_items": 0,
            "dropped_attributes": 0, "invalid_timestamps": 0, "spans_over_limit": 0,
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
        bridge._attribute_value("gen_ai.usage.input_tokens", 42, "span")

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

        # Flatten record fed straight into mapper.map_trace (same span dict
        # shape; no adaptation needed).
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
            })


if __name__ == "__main__":
    unittest.main()
