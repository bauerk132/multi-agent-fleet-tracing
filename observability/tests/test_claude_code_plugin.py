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

        self.assertEqual(counters, {"lines_read": 5, "malformed_lines": 3, "spans": 2})
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["name"], "claude_code.interaction")
        self.assertEqual(spans[0]["traceId"], "a" * 32)
        self.assertEqual(spans[0]["attributes"]["span.type"], "claude_code.interaction")
        self.assertEqual(spans[0]["resource_attributes"]["service.name"], "claude-code")


if __name__ == "__main__":
    unittest.main()
