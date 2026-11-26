"""M7-B step 1 tests (unittest): Claude Code adapter vocabulary.

Covers: vocabulary allowlist subsets (tests 4 and 5) and malformed-line
counting in the flatten step (test 14). Run from observability/:
    python -m unittest tests.test_claude_code_plugin -v
"""

import json
import sys
import unittest
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, sys_path)

from agentscope_importer import bridge  # noqa: E402
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
        })
        self.assertEqual(len(spans), 1)

    def test_non_dict_resource_span_entries_counted(self):
        # Non-dict resourceSpans entries (including None inside a list) are counted.
        line = _line([1, "x", None])
        spans, counters = flatten_lines([line, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 2, "malformed_lines": 0, "spans": 1, "skipped_items": 3,
        })
        self.assertEqual(len(spans), 1)

    def test_deeply_nested_line_is_malformed(self):
        # ~5000 levels of nesting raises RecursionError in json.loads: malformed.
        deep = "[" * 5000 + "]" * 5000
        spans, counters = flatten_lines([_good_line(), deep, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 3, "malformed_lines": 1, "spans": 2, "skipped_items": 0,
        })
        self.assertEqual(len(spans), 2)

    def test_non_text_line_is_malformed(self):
        # None and int items count as read and malformed; later lines still flatten.
        spans, counters = flatten_lines([_good_line(), None, 123, _good_line()])
        self.assertEqual(counters, {
            "lines_read": 4, "malformed_lines": 2, "spans": 2, "skipped_items": 0,
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
        })
        self.assertEqual(len(spans), 2)


if __name__ == "__main__":
    unittest.main()
