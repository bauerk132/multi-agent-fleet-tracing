"""Map one synthetic Hermes turn to the AgentScope feed (M3 entry check).

Plan §13 M3 entry gate: "map a synthetic sample to AgentScope's current
importer now, before accumulating adapters". This script drives the M3
observer with one synthetic turn, maps the received OTel spans to the
collector's Span objects, and runs the collector's ACTUAL build_feed
(agent-trace-collector/agentscope/feed.py) — the mapping is verified
against the real importer code, not hand-rolled.

Field mapping (observer span -> collector Span):
  agent.name              -> agent ("hermes")
  gen_ai.request.model    -> model (llm.call spans only)
  gen_ai.tool.name        -> tool (tool.exec spans only)
  status.code 2           -> status "error", else "ok"/"unknown"
  traceId / spanId / parentSpanId / startTimeUnixNano / endTimeUnixNano
                          -> trace_id / span_id / parent_span_id /
                             start_ns / end_ns
  tokens                  -> None (the observer does not collect token
                             counts; missing stays unknown, never
                             double-counted or zero-claimed)

Run from observability/:
    ../.venv/bin/python examples/map_hermes_sample_to_agentscope_feed.py
"""

import json
import sys
import uuid
from pathlib import Path

OBS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OBS))
sys.path.insert(0, "/home/hatch/workspace/agent-trace-collector")

from agentscope.feed import build_feed
from collector.spans import Span
from hermes_observer import HermesObserver
from receiver import LocalReceiver


def _event(event_type, session="s1", turn="t1", request=None, **extra):
    evt = {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "session_id": session,
        "turn_id": turn,
        "request_id": request or uuid.uuid4().hex[:16],
    }
    evt.update(extra)
    return evt


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
        tokens_input=None,  # observer does not collect tokens: unknown
        tokens_output=None,
        attributes=dict(attrs),
    )


def main():
    recv = LocalReceiver().start()
    observer = HermesObserver(endpoint=recv.endpoint)
    try:
        api = _event("pre_api_request", model="hermes-test-model")
        tool_pre = _event("pre_tool_call", tool_name="Bash")
        observer.handle_event(api)
        observer.handle_event(tool_pre)
        observer.handle_event(
            _event("post_tool_call", request=tool_pre["request_id"], outcome="observed_success")
        )
        observer.handle_event(
            _event("post_api_request", request=api["request_id"], outcome="observed_success")
        )
        observer.handle_event(_event("on_session_end", request=None))
        assert observer.drain(timeout=5.0), "observer did not drain"
    finally:
        observer.shutdown(timeout=5.0)

    collector_spans = [to_collector_span(s) for s in recv.spans]
    records, manifest = build_feed(
        collector_spans, traces_path="synthetic-hermes-turn.jsonl"
    )
    recv.stop()
    print(json.dumps({"records": records, "manifest": manifest}, indent=2))
    return records


if __name__ == "__main__":
    main()
