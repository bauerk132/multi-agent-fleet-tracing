# Milestone 4 — Build notes (Copilot observer, 2026-11-07)

**Builder:** Muse. **Branch:** `staging`. **Reviewer:** Antigravity
(handoff to follow). **Status:** Implemented + Tested; acceptance NOT
declared.

## B-01 — The Copilot surface is native OTel export, not a plugin

- **Evidence:** official VS Code docs
  (https://code.visualstudio.com/docs/agents/guides/monitoring-agents,
  crawled 2026-11-07). Copilot Chat "can export traces, metrics, and
  events via OpenTelemetry" when enabled
  (`github.copilot.chat.otel.enabled` / `COPILOT_OTEL_ENABLED` /
  `OTEL_EXPORTER_OTLP_ENDPOINT`). No hook/callback registration API
  is documented anywhere on the page — unlike Hermes's
  `register_hook`. The M3 plugin shape therefore does not transfer:
  there is nothing to `register()` into.
- **Decision:** the M4 observer is a receiver-side adapter
  (`copilot_plugin/`): it consumes Copilot's native OTLP spans
  (OTLP/HTTP JSON payloads via `handle_payload`, or the file
  exporter's JSONL via `ingest_jsonl`) and re-emits them as fleet
  schema-v1 spans. It never enables Copilot telemetry, never touches
  VS Code settings, never installs anything.
- **Test:** `test_package_loads_standalone_from_staged_copy`
  (subprocess: ONLY the `copilot_plugin/` directory on sys.path) and
  `test_config_doc_names_verified_settings` (config.md names the
  exact settings/env vars).

## B-02 — Span-kind mapping (documented attributes only)

- **Evidence:** the docs' attribute tables for `invoke_agent`
  (conversation.id, models, token totals, git repo/branch/commit,
  agent type, turn_count, error.type), `chat` (models, finish
  reasons, per-call tokens, time_to_first_token, server.address,
  error.type), `execute_tool` (tool name/type, call id, edit/skill/
  MCP params, error.type), `execute_hook` (decision, duration,
  tool_names, hook_type, hook_result_kind, error.type).
- **Decision:** `invoke_agent` → turn root `agent.run`;
  `chat` → `llm.call`; `execute_tool` → `tool.exec`;
  `execute_hook` → `hook.exec` (a documented v1 vocabulary
  extension — the M3 12-type event set is unchanged for Hermes).
  Native timestamps are preserved (fleet start/end = native
  start/end) so durations stay faithful.
- **Test:** `test_full_turn_end_to_end` (all four kinds + subagent
  nesting; timestamps asserted) and `test_error_taxonomy`.

## B-03 — Unsupported parenting stays external

- **Evidence:** the word "traceparent" does not appear anywhere on
  the official docs page. No inbound context injection into Copilot
  Chat is documented. Subagent propagation IS native (a subagent's
  `invoke_agent` appears as a child of the parent's `execute_tool`
  span) — within Copilot's own trace only.
- **Decision:** fleet trace IDs are SDK-generated; the native
  traceId is preserved as the turn correlation key
  (`turn.id` = `<traceId>:<rootSpanId>`, which also distinguishes a
  subagent invocation sharing its parent's traceId) and the native
  parentSpanId as the parent link. No remote span ID is fabricated.
- **Test:** `test_full_turn_end_to_end` (one fleet trace for the
  whole native trace; subagent root's parentSpanId == the
  runSubagent tool span's spanId).

## B-04 — Privacy: content, identity, and repo URL are dropped

- **Evidence:** docs state content capture is off by default
  ("Only metadata like model names, token counts, and durations are
  included") and identity capture is opt-in only, with explicit
  cautions for both. The docs' own security table: "No PII in
  default attributes".
- **Decision:** the vendored sanitizer allowlists only opaque IDs,
  bounded enums, and numeric metadata. Stripped + counted even when
  present: all content-capture attributes, identity attributes,
  repository URL + GitHub org, `server.address`. Kept: branch +
  commit SHA, MCP server-name hash, tool/skill/edit names, hook
  decisions. Claude agent spans are out of scope (dropped +
  counted).
- **Test:** `test_content_and_identity_stripped` (sentinel strings
  absent from serialized OTLP) and `test_claude_spans_out_of_scope`.

## B-05 — Ingestion: JSON payloads + file-exporter JSONL

- **Evidence:** exporter types are `otlp-http` (default, protobuf),
  `otlp-grpc`, `console`, `file`. The file exporter
  (`github.copilot.chat.otel.outfile` /
  `COPILOT_OTEL_FILE_EXPORTER_PATH`) writes "all signals" as JSON
  lines. The exact line shape is NOT documented. The dbSpanExporter
  path (SQLite + "Chat: Export Agent Traces DB") has an undocumented
  schema.
- **Decision:** `handle_payload` (OTLP/HTTP JSON) + `ingest_jsonl`
  (defensively accepts a full OTLP payload per line OR a single
  flattened span per line; metric/log lines skipped + counted).
  The SQLite path is NOT implemented. The default protobuf exporter
  is a known transport mismatch for the local JSON receiver —
  documented in `config.md`, not silently worked around.
- **Test:** `test_jsonl_ingestion` (both line shapes + metrics line
  + garbage + blank), `test_jsonl_missing_file`,
  `test_handle_payload_never_raises`.
- **Open verification (reviewer):** the real JSONL line shape on
  the installed build — see `config.md` contract test.

## B-06 — Lifecycle: lazy roots, fold-in, stale pruning

- **Evidence:** native spans arrive complete (start + end), but
  delivery order across payloads is not guaranteed: children may
  arrive before their `invoke_agent` root.
- **Decision:** the engine mirrors M3's shape (bounded queue 256,
  single worker, drain/shutdown, dedup by (traceId, spanId),
  stale TTL 300s, counters). A child arriving before its root
  creates a lazy root; the root folds the invoke_agent attributes
  in when it arrives (one `agent.run` per turn, never two). A late
  invoke_agent after its root already closed is dropped + counted.
  Late children parent to the ended root's context (still valid for
  parenting). Native parent links resolve through a bounded
  context map; unresolved parents fall back to the turn root and
  are counted, never invented.
- **Test:** `test_children_before_root_lazy_root_folds`,
  `test_stale_pruning`, `test_queue_overflow`,
  `test_duplicate_spans_dropped`, `test_session_fallback`.

## B-07 — Metrics and span events are counted, not emitted

- **Evidence:** the docs list GenAI metrics (operation duration,
  token usage), extension metrics (tool call counts/durations, edit
  acceptance/survival, user feedback), and span events
  (session.start, agent.turn, tool.call, edit feedback, survival,
  user feedback, cloud.session.invoke).
- **Decision:** out of scope for this round — translating metrics
  and user-behavior events would expand the fleet's purpose beyond
  agent execution flow. Events are counted by kind
  (`session_start_seen`, `agent_turn_seen`, `user_event_seen`, …);
  metrics stay at the native endpoint. Revisit in M5 if the
  importer needs them.
- **Test:** `test_span_events_counted_not_emitted`.

## B-08 — AgentScope feed mapping (entry check)

- **Evidence:** `observability/examples/map_copilot_sample_to_agentscope_feed.py`
  (2026-11-07 run) through the collector's ACTUAL `build_feed`:
  one record, agents ["copilot"], models ["gpt-4o"], span_count 3,
  top_tools readFile×1, error_count 0.
- **Note:** tokens report 0 — `build_feed` sums tokens only from
  spans named exactly `"llm"` (or `"session:*"` roots); ours are
  `llm.call` / `agent.run`. Token counts ARE present in the span
  attributes for a future importer update (M5). Nothing is
  double-counted; nothing is zero-claimed as unknown.
