# Milestone 3 entry — Sol §12/§13 findings mapped to the implementation

**Plan source:** Notion "🔭 Multi-Agent Fleet Tracing — OpenTelemetry
Integration Plan", §12 (GPT-6.1 Sol architecture/design review, Nov 6,
2026) and §13 (Sol 6.1 evidence-backed design review, Nov 6, 2026).
**Date:** 2026-11-06. **Builder:** Muse.

> The plan's M3 entry gate requires correcting the applicable P1s before
> building the Hermes observer, and mapping each new plan finding to
> actual code (fixed / already safe / not applicable). The Sol reviews
> are explicit that their probes reproduced defects in the plan's
> *example sketches* — not a claim that the shipped code contains them.
> This file is that mapping, verified against the code at local commit
> `485aec2` (Fix Round 2) plus the Fix Round 4 corrections below.

## Fixed in code (Fix Round 4)

### FR4-01 — claude_available omits SystemRoot (M2 close-out, Antigravity focused re-test)

- **Source:** Antigravity handoff `hof_01M6SMXWW30JC1MRDF` (Fix Round 3
  review): `test_stub_claude_parented_end_to_end` failed on Windows
  because `claude_available()` ran `[path, "--version"]` with only
  `PATH` in the env; the `.cmd` wrapper cannot start Python without
  SystemRoot.
- **Correction:** `claude_child.py` — `claude_available()` now includes
  `SystemRoot` from the parent env on Windows. One production-code
  change; no new test (the existing M2 stub tests cover it on Windows).
- **Status:** fixed; re-verify on the laptop (acceptance: 28 run, 27
  passed, 1 skipped, 0 failed on Windows).

### FR4-02 — False unparented fallback: context=None (Sol §12 P1, §13 P1)

- **Plan defect:** the child sketch passed `context=None` on the
  invalid-parent branch; `None` inherits the ambient current span
  instead of guaranteeing a new root. Fix: use `Context()`.
- **Applies to code:** YES. `child.py` had
  `context=ctx if parented else None` on the `agent.run` span.
  (In a fresh child process there is no ambient span, so the practical
  risk today was nil — but the branch was wrong by construction and
  would falsely parent if the child were ever embedded in a host
  process with a current span.)
- **Correction:** extraction factored into `resolve_parent_context()`
  (explicit `_EXTRACTOR` instance, no global textmap) returning an
  explicit empty `Context()` when unparented; `main()` uses it.
- **Regression test:** `test_unparented_context_ignores_ambient_span`
  — sets an ambient span in-process, resolves with no valid
  TRACEPARENT, asserts the returned context holds no valid span.

### FR4-03 — Automatic exception leakage (Sol §12 P1, §13 P1)

- **Plan defect:** `start_as_current_span()` defaults record exceptions
  and build a status description from exception text; a synthetic
  secret survived safe attributes and appeared in serialized OTLP.
  Fix: `record_exception=False` and `set_status_on_exception=False` on
  every owned span scope; classify errors with a safe enum and ERROR
  status without content.
- **Applies to code:** YES as hardening. All 9 owned scopes
  (`child.py` ×3, `fleet_trace.py` ×4, `claude_child.py` ×2) used the
  defaults. Nothing in the code relied on auto-exception status —
  both coordinators already set ERROR status manually with bounded
  messages ("Process failed; details withheld"), and `test_error_handling`
  sets status explicitly.
- **Correction:** both flags added to all 9 scopes plus the
  `_emit_span` test helper.
- **Regression tests:** `test_exception_privacy_no_leak` — a mocked
  `spawn_controlled_child` raising `RuntimeError("boom CANARY-EXC-999")`
  inside `run_intent`; asserts the coordinator spans still arrive AND
  the canary is absent from all serialized OTLP. Plus
  `test_all_owned_spans_disable_exception_recording` — a static guard
  asserting every `start_as_current_span(` in the three modules carries
  `record_exception=False`, so a future scope cannot silently
  reintroduce the leak.

## Already safe in code (no change)

### Extraction bug: lowercase carrier keys (Sol §12 P1, §13 P1)

- **Plan defect:** the child sketch passed uppercase TRACEPARENT keys
  in a plain dict to a case-sensitive getter; the valid parent became
  invalid. Fix: lowercase carrier keys.
- **Code:** `child.py` already builds
  `carrier = {key.lower(): os.environ[key] for key in (...)}` with a
  comment explaining the case-sensitivity. **Already safe.**

### Claude telemetry flags in the child env (Sol §12 P1, §13 P2)

- **Plan defect:** the old environment allowlist omitted all three
  documented Claude tracing-enable/exporter flags.
- **Code:** `claude_child.build_claude_child_env()` does
  `env.update(CLAUDE_TELEMETRY_FLAGS)` — `CLAUDE_CODE_ENABLE_TELEMETRY`,
  `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, `OTEL_TRACES_EXPORTER`, plus
  `OTEL_EXPORTER_OTLP_PROTOCOL`. **Already safe.** (Verified by
  `test_stub_claude_parented_end_to_end`, which asserts the flags in
  the stub's env.)

### Baggage allowlist (Sol §13 P1)

- **Plan defect:** default composite injection forwarded synthetic
  unapproved baggage. Fix: inject from an explicit context and omit
  baggage by default.
- **Code:** the coordinator's composite propagator includes the baggage
  propagator, but **no baggage is ever set anywhere in the code**
  (no `set_baggage` call exists) — the injected baggage is always
  empty. `build_child_env` only forwards `carrier["baggage"]` when
  present, which is never. **Already safe** (no baggage to forward);
  noted here so a future baggage use must go through an explicit
  allowlist.

## Not applicable to this codebase

### Link-timing rule correction (Sol §13 P2)

- The plan now permits late `AddLink` while a span is recording.
  The implementation only creates links at span creation
  (`fixture.export_external_agent_link`, FR1) and uses a bridge span /
  external ledger after a span ends — consistent with the corrected
  rule. No change needed.

### Per-adapter launch environment (Sol §13 P2)

- The Hermes adapter's own launch environment is M3 work, not M1/M2
  (the Hermes plugin spawns no child process — it observes in-process
  callbacks, so there is no child env to define). The Claude adapter's
  env is covered above.

## Test results for Fix Round 4 (2026-11-06)

Command: `python3 scripts/run_offline_tests.py` (from `observability/`,
venv Python 3.12, OTel 1.45.1)

**31 run, 30 passed, 1 skipped, 0 failed, 20.8s.**

New tests (3): `test_unparented_context_ignores_ambient_span`,
`test_exception_privacy_no_leak`,
`test_all_owned_spans_disable_exception_recording`. M1 suite 17 → 20
tests; M2 suite stays at 11 (one live test still skipped: no real
`claude` binary on this machine).

## M3 entry status

- M1: **ACCEPTED** (Antigravity G1+G2, staging `1d885e8a`).
- M2: verified except the FR4-01 SystemRoot line; acceptance pending a
  Windows re-run of the 28 tests (Antigravity).
- M3 entry P1s: **corrected** (FR4-02, FR4-03) or **verified already
  safe** (extraction keys, Claude flags, baggage).
- Next: build the Hermes observer (schema v1, plugin, isolated test
  harness) per plan §5 and §12/§13 M3 acceptance; the installed-build
  contract test runs on the laptop (disposable profile, never the
  active profile).
