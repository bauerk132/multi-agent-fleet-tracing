"""Map one synthetic Copilot turn to the AgentScope feed (M4 entry check).

Mirrors the M3 entry check (map_hermes_sample_to_agentscope_feed.py):
this script drives the M4 Copilot observer with one synthetic turn
(an invoke_agent root, one chat span, one tool span), maps the
received OTel spans to the collector's Span objects, and runs the
collector's ACTUAL build_feed
(agent-trace-collector/agentscope/feed.py) — the mapping is verified
against the real importer code, not hand-rolled.

Field mapping (observer span -> collector Span):
  agent.name              -> agent ("copilot")
  gen_ai.request.model    -> model (llm.call spans only)
  gen_ai.tool.name        -> tool (tool.exec spans only)
  status.code 2           -> status "error", else "ok"/"unknown"
  traceId / spanId / parentSpanId / startTimeUnixNano / endTimeUnixNano
                          -> trace_id / span_id / parent_span_id /
                             start_ns / end_ns
  gen_ai.usage.* tokens   -> tokens_input / tokens_output on llm.call
                             spans (the feed prefers llm spans, so the
                             root's session totals are not double-counted)

Run from observability/:
    ../.venv/bin/python examples/map_copilot_sample_to_agentscope_feed.py
"""

import json
import os
import sys
from pathlib import Path

OBS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OBS))

# The AgentScope collector lives outside this repo. Its checkout path
# is configurable via AGENTSCOPE_SRC so this script also runs on
# Windows, where the Linux default below does not exist. If the
# collector is absent, fail with a clear message instead of a raw
# ImportError traceback. (FR7-06)
AGENTSCOPE_SRC = Path(
    os.environ.get("AGENTSCOPE_SRC", "/home/hatch/workspace/agent-trace-collector")
)
if AGENTSCOPE_SRC.is_dir():
    sys.path.insert(0, str(AGENTSCOPE_SRC))
else:
    print(
        "AgentScope collector not found at %s; set AGENTSCOPE_SRC to the "
        "agent-trace-collector checkout and retry." % AGENTSCOPE_SRC,
        file=sys.stderr,
    )
    raise SystemExit(2)

from agentscope.feed import build_feed
from collector.spans import Span
from copilot_plugin import create_observer
from receiver import LocalReceiver

TRACE_ID = "d" * 32


def _span(name, span_id, parent=None, attrs=None, status=1):
    return {
        "traceId": TRACE_ID,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": "1000000000000000000",
        "endTimeUnixNano": "1000000001000000000",
        "attributes": dict(attrs or {}),
        "events": [],
        "status": {"code": status, "message": None},
        "resource": {"service.name": "copilot-chat"},
        "scope": {"name": "copilot-chat", "version": "1.0.0"},
    }


def _payload(spans):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "copilot-chat"}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "copilot-chat", "version": "1.0.0"},
                        "spans": [
                            {
                                **s,
                                "attributes": [
                                    {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}}
                                    for k, v in s["attributes"].items()
                                ],
                            }
                            for s in spans
                        ],
                    }
                ],
            }
        ]
    }


def to_collector_span(flat):
    attrs = flat.get("attributes") or {}
    status_code = (flat.get("status") or {}).get("code", 0)
    return Span(
        trace_id=flat["traceId"],
        span_id=flat["spanId"],
        parent_span_id=flat.get("parentSpanId"),
        name=flat["name"],
        agent=attrs.get("agent.name", ""),
        model=attrs.get("gen_ai.request.model"),
        tool=attrs.get("gen_ai.tool.name"),
        start_ns=int(flat.get("startTimeUnixNano") or 0) or None,
        end_ns=int(flat.get("endTimeUnixNano") or 0) or None,
        status="error" if status_code == 2 else ("ok" if status_code == 1 else "unknown"),
        tokens_input=attrs.get("gen_ai.usage.input_tokens"),
        tokens_output=attrs.get("gen_ai.usage.output_tokens"),
        attributes=dict(attrs),
    )


def main():
    recv = LocalReceiver().start()
    observer = create_observer(endpoint=recv.endpoint)
    try:
        root = _span(
            "invoke_agent", "1111111111111111", None,
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": "conv-sample",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 1200,
                "gen_ai.usage.output_tokens": 300,
                "copilot_chat.turn_count": 1,
            },
        )
        chat = _span(
            "chat", "2222222222222222", "1111111111111111",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 400,
                "gen_ai.usage.output_tokens": 100,
            },
        )
        tool = _span(
            "execute_tool", "3333333333333333", "2222222222222222",
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "readFile",
                "gen_ai.tool.call.id": "call-sample-1",
            },
        )
        assert observer.handle_payload(_payload([root, chat, tool])) == 3
        assert observer.drain(timeout=5.0), "observer did not drain"
    finally:
        observer.shutdown(timeout=5.0)

    collector_spans = [to_collector_span(s) for s in recv.spans]
    records, manifest = build_feed(
        collector_spans, traces_path="synthetic-copilot-turn.jsonl"
    )
    recv.stop()
    print(json.dumps({"records": records, "manifest": manifest}, indent=2))
    return records


if __name__ == "__main__":
    main()
