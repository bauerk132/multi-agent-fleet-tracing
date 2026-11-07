# Milestone 4 — Fix Round 7 (FR7, 2026-11-07)

**Builder:** Muse. **Branch:** `staging` (staging-only until the end;
no merge to main). **Reviewer:** Antigravity (re-review to follow).
**Status:** Implemented + Tested; acceptance NOT declared.
**Trigger:** Antigravity's M4 review — M3 ACCEPTED, M4 FAILS the
installed VS Code Copilot OTel contract (handoff
`hof_01M6VMGCRZB6RKY8MH`, claimed by `claude-muse`, status working).
The six items below are Antigravity's `next_step`, implemented as
specified.

## What changed

### FR7-01 (P1) — Real FileSpanExporter / FileMetricExporter / FileLogExporter JSONL format
- `observer.py`: new `_normalize_span` (with `_hrtime_to_ns`,
  `_span_time_ns`, `_resource_attrs`, `_normalize_event`,
  `_normalize_link`) converts one raw span dict into the internal
  flattened shape. Applied in `_index_and_enqueue`, so every entry
  point (`handle_payload`, `handle_spans`, `ingest_jsonl`) accepts
  the real shape.
- Timestamps: `startTime` / `endTime` HrTime `[seconds, nanoseconds]`
  arrays convert to Unix nanoseconds (`sec * 1_000_000_000 + nsec`);
  `startTimeUnixNano` / `endTimeUnixNano` still take precedence.
  `flatten_payload` uses the same conversion.
- Parent link: `parentSpanId` falls back to
  `parentSpanContext.spanId` when the top-level key is absent/empty.
- Resource attributes: `service.name` (and all resource attrs) are
  read from nested `resource.attributes` — flat dict, OTLP key/value
  list, or top-level flat form — so out-of-scope filtering works on
  JSONL lines. `_attrs_to_dict` now passes flat dicts through.
- `ingest_jsonl`: `scopeMetrics`, `type == "metric"`, `dataPoints`,
  `hrTime` + `body` (OTel JS log record), and `scopeLogs` lines are
  `skipped_non_trace`, never `invalid_lines`. ReadableSpan lines
  (ids only in `spanContext`) are collected as spans.
- Tests: `test_fr7_01_readable_span_jsonl` (HrTime conversion
  asserted on the emitted spans, parentSpanContext turn linkage),
  `test_fr7_01_nested_resource_attributes` (list-form
  `claude-code` resource is out of scope; flat-dict `copilot-chat`
  is in scope), `test_fr7_01_non_trace_jsonl_lines_skipped` (5
  non-trace line shapes, all skipped, 0 invalid).

### FR7-02 (P1) — Multi-batch / JSONL child-before-root ordering & keyed/subagent lazy-root folding
- `ingest_jsonl` collects span lines across the whole file and calls
  `handle_spans` once, so `_topological_order` orders parents before
  children across the file (previously one batch per line).
- `_root_span_id_for`: when the walk hits a parent not yet in
  `_native_index` (streaming export — the parent `invoke_agent`
  hasn't finished), it returns that parent span id (the highest
  known ancestor) instead of `""`, so the child gets
  `turn.id = <traceId>:<rootSpanId>` and keys its lazy root under
  `(trace_id, root_span_id)`.
- `_process_invoke_agent`: the unkeyed lazy root `(trace_id, "")` is
  only popped when the arriving `invoke_agent` has no
  `parent_span_id`, so a subagent `invoke_agent` never hijacks the
  parent turn's lazy root. When folding/closing a keyed lazy root
  (`_turn_roots.get((trace_id, span_id))`), `_root_attrs(...)` is
  applied onto the existing span and
  `_native_contexts[(trace_id, span_id)]` is recorded before ending
  it, so `agent.run` retains `gen_ai.request.model`,
  `gen_ai.usage.*`, `copilot.agent.type`, `copilot.git.*`,
  `copilot.turn_count`.
- Tests: `test_fr7_02_child_before_root_jsonl_orders_parents_first`
  (child line first in file; `lazy_roots_folded == 0` proves the
  ordering happened at ingest; child `turn.id` is the root's),
  `test_fr7_02_streaming_child_keys_lazy_root_by_root_span_id`
  (child in its own batch before the root; keyed fold retains all
  root attributes), `test_fr7_02_subagent_root_does_not_hijack_unkeyed_lazy_root`
  (subagent root nests under its tool span; parent root folds the
  unkeyed lazy root with its own attrs).

### FR7-03 (P2) — Session ID resolution
- New `_resolve_session_id` checks `gen_ai.conversation.id`, then
  `copilot_chat.chat_session_id`, then `copilot_chat.session_id`
  (first non-empty wins). `_session_id_for` and the native index now
  store the resolved id, so the aliases also resolve up the parent
  chain.
- Test: `test_fr7_03_session_id_aliases` (both aliases resolve;
  `gen_ai.conversation.id` still wins).

### FR7-04 (P2) — Real span name suffixes & SDK attribute aliases
- `_operation_of`: when `gen_ai.operation.name` is absent, the
  operation is the first token of the span name
  (`name.split()[0]` — `"chat github.copilot"` → `chat`,
  `"hook PreToolUse"` → `hook`); `"hook"` maps to `"execute_hook"`.
- New `_attr_first` helper; aliases supported:
  `github.copilot.turn_count` (for `copilot_chat.turn_count`),
  `github.copilot.hook.type` (for `copilot_chat.hook_type`),
  `github.copilot.mcp.server.name_hash` (for
  `github.copilot.tool.parameters.mcp_server_name_hash`).
- Tests: `test_fr7_04_span_name_suffixes`,
  `test_fr7_04_sdk_attribute_aliases`.

### FR7-05 (P2) — Stripped-field accounting & case-insensitive out-of-scope check
- `CONTENT_ATTRIBUTES` gains the real `copilot-chat` v0.73.0 / SDK
  content keys (`copilot_chat.user_request`,
  `copilot_chat.hook_command`, `copilot_chat.file.relative_path`,
  `gen_ai.system_instructions`, `gen_ai.tool.description`,
  `gen_ai.agent.description`, `copilot_chat.reasoning_content`,
  `copilot_chat.prompt_context`, `copilot_chat.prompt_instructions`,
  `copilot_chat.markdown_content`, `copilot_chat.event_details`,
  `github.copilot.skill.content`,
  `github.copilot.elicitation.message`,
  `github.copilot.hook.error_message`).
- `REPO_ATTRIBUTES` gains `github.copilot.cwd`,
  `github.copilot.git_root`, `github.copilot.repository`,
  `github.copilot.skill.path`, `github.copilot.mcp.server.name`.
- `IDENTITY_ATTRIBUTES` gains `enduser.pseudo.id`.
- Out-of-scope check is case-insensitive: `service.name` is
  lowercased before the `COPILOT_SERVICES` test; `gen_ai.agent.name`
  is lowercased and matches `"claude"` or `"claude-code"`.
- Tests: `test_fr7_05_new_stripped_keys_counted` (21 stripped
  fields — 20 new keys plus the `server.address` the chat helper
  always carries; values absent from the serialized output),
  `test_fr7_05_out_of_scope_case_insensitive` (`"Claude"`,
  `"CLAUDE"`, `"claude-code"` agent names and `"Claude-Code"`,
  `"CLAUDE-CODE"` services dropped; `"Copilot-Chat"` accepted).

### FR7-06 (P2/P3) — Docs & example portability
- `config.md`: notes that `copilot-chat` v0.73.0 defaults the
  `otlp-http` protocol to `http/json` (earlier versions defaulted
  to protobuf, which the JSON receiver rejects); new "File exporter
  line shape (ReadableSpan JSONL)" section documents the exact line
  shape, HrTime conversion, `parentSpanContext`, nested resource
  attributes, name suffixes, non-trace lines, and child-before-root
  ordering.
- `examples/map_copilot_sample_to_agentscope_feed.py`: reads the
  collector checkout from `AGENTSCOPE_SRC` (default unchanged) and
  exits with a clear message (exit 2, no traceback) when the
  directory is absent, so it runs on Windows.
- `observer.py` module docstring updated to the verified shape
  (was: "exact JSONL line shape is NOT documented").
- Tests: `test_fr7_06_config_doc_notes_json_default_and_readable_span`,
  `test_fr7_06_example_falls_back_without_collector`.

## Test results

- Full suite (from `observability/`):
  `../.venv/bin/python -m unittest discover -s tests -t .`
  — **Ran 90 tests, OK (skipped=1)**. M4 baseline was 77 (76
  passed, 1 skipped); FR7 adds 13 tests, all passing, no
  regressions.
- Example feed mapping still runs:
  `../.venv/bin/python examples/map_copilot_sample_to_agentscope_feed.py`
  — 1 trace, 3 spans, feed manifest OK.

## Commits

- Local commit: `a66f687` ("Fix Round 7 (M4): real FileSpanExporter
  JSONL shape, child-before-root ordering, keyed lazy roots").
- Staging push (snapshot-push): `93638f52a181ef83d7d5a339ef5a49274c2070a3`
  ("Fix Round 7 (M4): ..."), verified as the staging head via
  `list_commits`; parent `865410be` (M4). Sync record updated (13
  entries). This SHA was recorded in a local commit after the push;
  it rides along on the next snapshot push (recording it cannot be
  part of the pushed tree itself).
- Previous staging head: `865410be` (M4).

## Acceptance

NOT declared. Awaits Antigravity's re-review of M4 against the FR7
items (handoff to follow). Do NOT merge to main.
