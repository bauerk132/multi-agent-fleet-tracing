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

---

## Fix Round 5 (2026-11-07) — Antigravity: M1/M2 ACCEPTED, M3 FAILS installed-build contract test

Full per-finding record: `reviews/milestone-03/fix-rounds.md` (FR5-01…
FR5-05, all P1 / blocking acceptance). Summary of what changed:

- **FR5-01 — self-contained plugin.** The observer implementation
  moved into `observability/hermes_plugin/observer.py` with relative
  imports only; its dependencies are vendored inside the package
  (`hermes_plugin/_otlp_json.py`, verbatim copy of
  `observability/otlp_json.py`; `hermes_plugin/_sanitize.py`, verbatim
  copy of the sanitize logic from `observability/fleet_trace.py`).
  `hermes plugins doctor` can now load the plugin from a staged copy
  of ONLY the `hermes_plugin` directory. Top-level
  `observability/hermes_observer.py` is a thin re-export shim, so the
  existing M3 tests and the AgentScope example work unchanged.
- **FR5-02 — declared deps + OTel guard.** `plugin.yaml` declares
  `python_dependencies` (OTel 1.45.1 pins, same as
  `observability/requirements.txt`). Importing `hermes_plugin` touches
  only the stdlib; `register()` raises a clear `RuntimeError` naming
  the missing dependency if OpenTelemetry is absent, instead of
  leaking an `ImportError` traceback into the host.
- **FR5-03 — `**kwargs` callbacks.** Hook callbacks are literally
  `def _callback(**kwargs)`, satisfying the host's
  `_accepts_var_kwargs` check.
- **FR5-04 — kwargs → schema-v1 mapping.** New `_event_from_kwargs`
  (status → outcome, error dict → bounded `error_type`, subagent
  coordinates → session/turn/request/parent-request ids, `payload`
  kwarg as fallback; dict/object payload path kept). The observer
  relaxes `turn_id` for human-input events only (stable default
  `"human-input"`; `request_id` falls back to the `event_id`);
  validation is unchanged for all other events. `_close_span` now
  records `error.type` on failure closes, so the mapped error type
  is observable.
- **FR5-05 — manifest + tests.** `plugin.yaml` declares
  `provides_hooks` (all 12 hook names). New suite
  `observability/tests/test_hermes_plugin.py` (8 tests) covers the
  plugin adapter, manifest parity, and both subprocess isolation
  checks (staged copy, missing OTel).

### Test results (2026-11-07)

Command: `../.venv/bin/python -m unittest discover -s tests -t . -v`
(from `observability/`, venv Python 3.12, OTel 1.45.1)

**53 run, 52 passed, 1 skipped, 0 failed, ~33s.**

M3 suite grew 14 → 22 tests (8 new plugin tests, all passing; the 14
pre-existing M3 tests pass unchanged through the re-export shim). M1
(20) and M2 (11) suites unchanged and passing. The 1 skip is the
pre-existing live-Claude test (no real `claude` binary on this
machine). `python3 scripts/run_offline_tests.py` → PASS (same result).

### What is still NOT claimed (FR5)

- **Installed Hermes contract: still UNVERIFIED on this VM.** No
  Hermes build is installed here. Three details could not be resolved
  from the code or the findings as handed over (see fix-rounds.md):
  the exact Hermes runtime kwarg names / status vocabulary, the exact
  manifest-schema shape for `python_dependencies` / `provides_hooks`,
  and whether the host delivers flat kwargs, a `payload` kwarg, or
  something else. The laptop-side contract test (`hermes plugins
  doctor` on the staged `hermes_plugin/` directory + live hook
  delivery in a disposable profile) is the verification for all three.
- **Windows behavior is verified by reasoning + Antigravity's Windows
  run, not by executing on Windows on this VM.**
- M3 acceptance is NOT DECLARED — Fix Round 5 is a builder claim
  (Implemented + Tested), not acceptance. The plugin must NOT be
  enabled in the active Hermes profile until the contract test passes.
