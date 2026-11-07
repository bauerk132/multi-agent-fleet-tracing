# Milestone 3 — Builder evidence (Hermes observer)

**Milestone:** 3 — Validate the Hermes observer (plan §5, §10, §12, §13).
**Date:** 2026-11-06. **Builder:** Muse. **Status:** Implemented + Tested.
**Acceptance:** NOT DECLARED — requires the independent review step
(Antigravity) plus the installed-build contract test on the dev machine.

## What was built

- `observability/hermes_observer.py` — the Hermes-independent observer
  core: schema v1 event validation, bounded queue + single worker thread,
  explicit (never ambient) span parenting, turn roots, approval pairing,
  dedup / stale TTL / dropped counters, turn/session lifecycle, bounded
  shutdown. Full design in the module docstring.
- `observability/hermes_plugin/__init__.py` — thin adapter: `register(ctx)`
  wires the 12 documented hook names to the observer. Importing it
  registers nothing. `observability/hermes_plugin/plugin.yaml` — manifest
  updated (M3 state, isolated-profile validation pending).
- `observability/tests/test_hermes_observer.py` — 14 isolated tests with
  a FakeHermesHost harness (synthetic hook delivery + fake agent proving
  non-interference). No Hermes installation needed here.
- `observability/fleet_trace.py` — `SAFE_ATTRIBUTE_KEYS` extended with
  the M3 schema-v1 keys (`turn.id`, `request.id`, `observation.source`,
  `observation.completeness`, `hermes.attempt`); `SAFE_ERROR_TYPES`
  extended with `Cancelled`, `Unknown` (bounded enums).
- `reviews/milestone-03/m3-entry-p1-mapping.md` — Sol §12/§13 findings
  mapped to code (fixed / already safe / not applicable).

## Schema v1 (event)

Fields: `event_id`, `event_type` (12 hook types), `session_id`,
`turn_id`, `request_id`, `parent_request_id` (optional, explicit
enclosing request), `attempt`, `outcome` (bounded enum), `error_type`
(bounded enum), `model`, `tool_name`. Denied (stripped + counted):
`prompt`, `command`, `answer`, `content`, `arguments`, `result`,
`text`, `input`, `output`. IDs are validated (`[A-Za-z0-9._-]{1,128}`).

Hook → span mapping: pre/post_api_request → `llm.call`;
pre/post_tool_call → `tool.exec`; subagent_start/stop →
`subagent.spawn`; on_human_input_request/resolved → `approval.wait`
(kind=approval, paired by request_id); on_session_end → turn
reconciliation + bounded flush (never shutdown); on_session_finalize /
on_session_reset → session cleanup. Each turn gets a lazy `agent.run`
root; operation spans parent to it or to the named enclosing request.

## Test results (2026-11-06)

Command: `python3 scripts/run_offline_tests.py` (from `observability/`,
venv Python 3.12, OTel 1.45.1)

**45 run, 44 passed, 1 skipped, 0 failed, 29.1s.**

M3 suite: 14 tests, all passing — pairing, concurrency (no
cross-parenting), duplicates, retry-after-close, missing-post →
incomplete, late completion dropped, approval pairing, two consecutive
turns export, callback-never-raises + agent unchanged, queue overflow,
receiver outage (loss visible, shutdown bounded), stale pruning,
subagent explicit parenting. Privacy: denied content fields stripped;
sentinels (prompt/answer/secret) absent from all serialized OTLP.

M1 suite: 20 tests; M2 suite: 11 tests (one live test still skipped:
no real `claude` binary on this machine).

## What is NOT claimed

- **Installed Hermes contract: UNVERIFIED.** No Hermes build is
  installed on this VM. The plugin's `register(ctx)` signature, hook
  names, payload shapes, manifest syntax, and live hook delivery are
  validated only by the installed-build contract test on the dev
  machine (disposable profile, never the active profile). The plugin
  must NOT be enabled in the active Hermes profile until that passes.
- **No live Hermes run.** All M3 evidence is synthetic hook delivery
  through the FakeHermesHost harness.
- **AgentScope importer mapping: DONE.** One synthetic Hermes turn was
  mapped through the collector's actual `build_feed` (see
  `agentscope-feed-mapping.md` and
  `observability/examples/map_hermes_sample_to_agentscope_feed.py`):
  agents ["hermes"], models ["hermes-test-model"], top_tools Bash×1,
  span_count 3, tokens unknown (0, not double-counted). Full ingestion
  is M5; this proves schema v1 carries everything the current importer
  needs.

## Next required action

Antigravity (independent reviewer) runs on the laptop, in a disposable
Hermes profile:
1. `hermes --version`, `hermes plugins list`,
   `hermes plugins doctor .\observability\hermes_plugin --ci` — record
   the exact installed version and whether the manifest + register(ctx)
   load.
2. Drive the documented hooks (or a synthetic host) and confirm the
   event/schema/ID contract matches schema v1; report any payload or
   signature mismatch.
3. Confirm the plugin does not modify tool args / conversation text and
   does not block on export in the disposable profile.
4. Hand back M3 PASS / FAIL (or FIX ROUND REQUIRED) with evidence.
   Do not enable in the active profile and do not merge to main.
