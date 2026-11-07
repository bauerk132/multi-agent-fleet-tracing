# Milestone 4 — Fix Round 8 (FR8, 2026-11-07)

**Builder:** Muse. **Branch:** `staging` (staging-only until the end;
no merge to main). **Reviewer:** Antigravity (final verification to
follow).
**Status:** Implemented + Tested; acceptance NOT declared.
**Trigger:** Antigravity's M4 re-review after FR7 (candidate commit
`93638f52`) — 90/90 offline tests pass, 5 of 6 FR7 items pass; a
surgical Fix Round 8 is required (handoff `hof_01M6VYKXNHFD5NXE28`,
claimed by `claude-muse`, status working). The two items below are
Antigravity's `next_step`, implemented as specified.

## What changed

### FR8-01 (P1 blocker) — log spanContext poisoning
Root cause: in copilot-chat, FileLogExporter emits log records
(`copilot_chat.session.start`, `copilot_chat.agent.turn`, GenAI
inference, `copilot_chat.tool.call`) WHILE `invoke_agent`, `chat`,
and `execute_tool` spans are active on the OTel context. Those log
lines carry `hrTime`, `hrTimeObserved`, `body`, AND `spanContext`
with the active span's ids, and are written to the JSONL file BEFORE
the active span ends. In `ingest_jsonl`, the `spanContext` branch
was evaluated before the non-trace skip branch, so every such log
record was collected as a span; then in `_index_and_enqueue`,
`self._seen[(trace_id, span_id)]` was inserted BEFORE
`_operation_of(span)`, so the log failed the operation check AFTER
poisoning `_seen` — and 100% of the real spans arriving later were
dropped as duplicates.

- (a) `ingest_jsonl`: the non-trace metric/log skip branch now runs
  BEFORE the single-span branches (`"traceId" in obj and "spanId"
  in obj` / `spanContext` dict). The skip condition is broadened to
  the specified set: `resourceMetrics`, `resourceLogs`,
  `scopeMetrics`, `scopeLogs`, `type in ("metric", "log")`,
  `dataPoints`, bare `hrTime`, `hrTimeObserved`, `severityNumber`,
  `severityText`. (A real ReadableSpan line never carries a
  top-level `hrTime` — its timestamps are `startTime`/`endTime`
  HrTime arrays — so the broadened check cannot swallow a span.)
- (b) `_index_and_enqueue`: the `_seen` lookup/insertion (with
  `MAX_SEEN` eviction) moved AFTER the out-of-scope
  (`service.name` / `gen_ai.agent.name`) and `op =
  self._operation_of(span)` checks, so rejected or
  unknown-operation objects never poison `_seen` for valid spans.
- Tests: `test_fr8_01_log_records_with_span_context_do_not_poison_seen`
  (two log records with active spanContexts — one `severityText`,
  one `severityNumber` — written before the real root/chat span
  lines; `skipped_non_trace == 2`, `spans_enqueued == 2`,
  `unknown_operation == 0`, `duplicates == 0`, `agent.run == 1`,
  `llm.call == 1` with the root's turn id),
  `test_fr8_01_unknown_operation_does_not_poison_seen` (an
  unknown-operation object sharing ids with a later real
  `invoke_agent`; the real span survives, `duplicates == 0`).

### FR8-02 (P2 cleanup) — keyed lazy root registers its native context
- `_turn_root`: when creating a keyed lazy root (`root_key`
  non-empty), registers `self._native_contexts[(trace_id,
  root_key)] = ctx` with the FIFO `MAX_NATIVE_INDEX` bound, so
  direct child spans arriving in later streaming batches resolve
  `self._native_contexts.get((trace_id, parent_id))` instead of
  counting `parent_unresolved`. The real `invoke_agent` overwrites
  the entry with its own context when it folds the lazy root (that
  write already existed at the fold site).
- Test: `test_fr8_02_keyed_lazy_root_registers_native_context`
  (two children in separate streaming batches before the root;
  `parent_unresolved == 0`, `lazy_roots_folded == 1`, both children
  parented to `agent.run`).

Regression proof: all 3 new tests FAIL against the pre-fix
`observer.py` (verified by stashing only `observer.py` and
re-running them — 3 failures, including `parent_unresolved == 2`
on the FR8-02 test — then restoring the fix).

## Test results

- Full suite (from `observability/`):
  `../.venv/bin/python -m unittest discover -s tests -t .`
  — **Ran 93 tests, OK (skipped=1)**. FR7 baseline was 90 (89
  passed, 1 skipped); FR8 adds 3 tests, all passing, no
  regressions. Count note: Antigravity reported "90/90 offline
  tests pass" for the FR7 tree — the 1 skipped test is a
  pre-existing skip (unittest counts it as skipped, not failed);
  the named offline runner reports PASS.
- Named offline runner (`observability/scripts/run_offline_tests.py`):
  Ran 93 tests, OK (skipped=1); **offline tests: PASS**.
- Example feed mapping still runs:
  `../.venv/bin/python examples/map_copilot_sample_to_agentscope_feed.py`
  — 1 trace, 3 spans, feed manifest OK.

## Commits

- Local commit: `8cb7737` ("Fix Round 8 (M4): log spanContext
  poisoning, keyed lazy-root native context").
- Staging push (snapshot-push):
  `68b53aee5bbfd6dde71acaf9477cb023f2f1d78f` ("Fix Round 8 (M4):
  ..."), verified as the staging head via `list_commits`; parent
  `93638f52` (FR7). Sync record updated (14 entries). This SHA was
  recorded in a local commit after the push; it rides along on the
  next snapshot push (recording it cannot be part of the pushed
  tree itself).
- Previous staging head: `93638f52` (FR7).

## Acceptance

NOT declared. Awaits Antigravity's final verification of M4 against
FR8-01 and FR8-02 (handoff to follow). Do NOT merge to main.
