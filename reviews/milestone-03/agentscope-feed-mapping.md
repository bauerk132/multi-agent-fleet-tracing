# Milestone 3 entry — synthetic Hermes sample mapped to the AgentScope importer

**Date:** 2026-11-06. **Builder:** Muse. **Source:** plan §13 M3 entry
gate 2 ("map a synthetic sample to AgentScope's current importer now").

## Method

`observability/examples/map_hermes_sample_to_agentscope_feed.py` drives
the M3 observer with one synthetic turn (pre_api_request with model
`hermes-test-model`, pre/post_tool_call with tool `Bash`,
on_session_end), maps the received OTel spans to the collector's `Span`
objects, and runs the collector's ACTUAL `build_feed`
(`agent-trace-collector/agentscope/feed.py`) — the mapping is verified
against the real importer code, not hand-rolled.

## Field mapping (observer span -> collector Span)

| Observer span field | Collector Span field | Notes |
|---|---|---|
| `agent.name` = "hermes" | `agent` | feed `agents`: ["hermes"] |
| `gen_ai.request.model` | `model` | feed `models`: ["hermes-test-model"] |
| `gen_ai.tool.name` | `tool` | feed `top_tools`: [{"tool": "Bash", "calls": 1}] |
| `status.code` 2 / 1 / 0 | `status` "error" / "ok" / "unknown" | `error_count`: 0, `error_spans`: [] |
| `traceId` / `spanId` / `parentSpanId` | `trace_id` / `span_id` / `parent_span_id` | native IDs preserved |
| `startTimeUnixNano` / `endTimeUnixNano` | `start_ns` / `end_ns` | `started_at` / `ended_at` derived |
| tokens | `tokens_input` / `tokens_output` = None | **Unknown, not zero-claimed.** The observer does not collect token counts; `build_feed` sums to 0 and does not double-count (its `llm`/`session:` branches do not match our span names, so no fallback total is invented). |

## Result (2026-11-06 run)

One synthetic turn produced exactly one feed record: `trace_id`
`ba6aaa70de4e05902cdac20228bb3ae9`, `span_count` 3 (agent.run root +
llm.call + tool.exec), `agents` ["hermes"], `models`
["hermes-test-model"], `top_tools` [{"tool": "Bash", "calls": 1}],
`tokens_in` 0, `tokens_out` 0, `error_count` 0, `projects` [].
Manifest: `schema_version` 1, `trace_count` 1, `span_count` 3.

## Limits

- This is a synthetic sample through the collector's feed builder, not
  a live AgentScope import (the AgentScope repo is not on this machine;
  full ingestion is M5). It proves the schema-v1 spans carry everything
  the current importer needs and nothing it would misread.
- `turn.id` / `request.id` / `observation.*` attributes ride along in
  the Span's `attributes` dict (preserved, not consumed by the feed).
