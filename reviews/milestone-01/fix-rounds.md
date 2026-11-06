# Milestone 1 — Fix Round 1 (Antigravity review, 2026-11-06)

**Review under fix:** Antigravity "Independent Review Report: OpenTelemetry Fleet
Tracing (Milestone 1) & Claude Code Integration Readiness (Milestone 2)",
2026-11-06 (HiveMind handoff `hof_01M6RNMAQA9H660H2D`).
**Verdicts being fixed:** M1 **FAIL** (blocked by missing repository artifacts);
M2 **NEEDS CHANGES**.
**Fix-round commit (local git):** `25643e8` — "Fix Round 1: address Antigravity
M1/M2 review findings" (8 files, +394/−15).
**Branch:** `main`. **Date:** 2026-11-06. **Builder:** Muse.

> **How to read this round:** every finding from the review is numbered below
> with its severity, a repro (or the verification that it was already handled
> in code), the smallest targeted correction, and the regression test that
> proves it. Findings the reviewer could not see — because the code was not
> on its host — are marked **already-handled** with the test that proves it;
> they got no code change. Nothing here changes an interface, a dependency,
> or unrelated production config.

## Findings

### FR1-01 — M1-P0-01 (P0, Availability): codebase missing on the review host

- **Reviewer repro:** `Test-Path "C:\Users\kbaue\workspace"` → `False`;
  drive-wide search for `*multi-agent-fleet-tracing*` → 0 matches;
  `C:\Users\kbaue\OneDrive\Documents\Desktop\Tracing` empty.
- **Root cause:** the code lives on the builder VM
  (`~/workspace/multi-agent-fleet-tracing/`) and on GitHub
  (`bauerk132/multi-agent-fleet-tracing`); it was never staged on the
  laptop. The reviewer's test claims are therefore uncorroborated *from its
  side* — not disproven.
- **Correction:** no code change. This commit is pushed to GitHub and the
  re-review handoff (HiveMind, to Antigravity) gives the exact clone path
  and commit (`25643e8`). The reviewer clones the repo onto the laptop
  before re-running the tests.

### FR1-02 — G2-P0-01 (P0, Security): child env sanitizer

- **Reviewer repro:** spec file `coordinator/spawner.py` does not exist in
  the implementation it could see.
- **Verification (already-handled):** `fleet_trace.build_child_env()` builds
  the child environment from `ALLOWED_CHILD_ENV` only — never
  `os.environ.copy()`. No `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD`
  or auth header can reach a child. Regression tests:
  `test_env_allowlist` (fake secret in the parent env never reaches the
  child) and `test_no_secrets_in_claude_child_env`.
- **Correction:** none.

### FR1-03 — G2-P0-02 (P0, Privacy): payload bounding

- **Reviewer repro:** spec file `schema/spans.py` does not exist in the
  implementation it could see.
- **Verification (already-handled):** `sanitize_attributes()` allowlists
  span keys; `llm.call` records only `gen_ai.usage.input_tokens` /
  `output_tokens` and model/provider names — no raw prompts or completions.
  `state.write` carries hash refs only. Regression test:
  `test_no_secrets_in_otlp` (poisoned attributes + fake secrets/PII never
  appear in the received serialized OTLP).
- **Correction:** none (see also FR1-14 for the new length guard).

### FR1-04 — M1-P1-01 (P1, Verification): no self-contained test runner

- **Reviewer repro:** `pytest` is not installed on the review host; the
  11/11 claim had no runnable artifact attached.
- **Correction:** `observability/scripts/run_offline_tests.py` — stdlib-only
  runner for the M1 + M2 unittest suites (uses the repo venv python when
  present, else `sys.executable`). No pytest, no network.
  `python scripts/run_offline_tests.py` → **27 run, 26 passed, 1 skipped**
  (live-Claude test, no binary on this machine), 18.0s.
- **Regression test:** the runner itself; its exit code is the verdict.

### FR1-05 — G1-P1-01 (P1, Architecture): graceful flush / shutdown

- **Reviewer repro:** spec file `coordinator/lifecycle.py` has no
  `force_flush()` / `shutdown()` lifecycle.
- **Verification (partially handled):** the code already called
  `provider.force_flush(timeout_millis=5000)` + `provider.shutdown()` at
  the end of `run_intent`, in `export_fixture`, in `child.py`, and via
  `atexit`. What was missing: `atexit` does not run on SIGTERM, so a
  SIGTERM-killed coordinator would drop in-flight batch spans.
- **Correction:** `fleet_trace._install_signal_handlers()` registers
  SIGINT/SIGTERM handlers that flush, shut down, restore the default
  handler, and re-raise the signal (original termination semantics kept).
  Wired into `init_tracing()`; no-ops outside the main thread.
- **Regression tests:** `test_signal_handlers_flush_on_sigterm`
  (subprocess: span ended, SIGTERM sent, process exits `-15`, span
  arrived at the receiver) and `test_signal_handlers_registered`.

### FR1-06 — G1-P1-02 (P1, Architecture): span links across external boundaries

- **Reviewer repro:** spec file `coordinator/boundary.py` risks fabricating
  fake parent-child spans across external agent boundaries.
- **Verification:** the implementation never fabricated one — the fixture's
  external handoff is a lone `agent.handoff` span with ledger IDs only
  (`test_external_handoff`). What was missing: a demonstrated span-link
  case, and the receiver dropped `links` when flattening payloads.
- **Correction:** `receiver.flatten_payload()` now carries `links`
  (`traceId`, `spanId`, `attributes`); the OTLP JSON exporter already
  serialized them. New `fixture.export_external_agent_link()` simulates
  the external agent's side: its own root trace, linked back to the
  coordinator's handoff span with `link.type = "external_boundary"`.
- **Regression test:** `test_external_agent_span_link` (external trace id
  differs, no `parentSpanId`, exactly one link with the coordinator trace
  id, the handoff span id, and `link.type = "external_boundary"`).

### FR1-07 — G2-P1-01 (P1, Security): error sanitizer

- **Reviewer repro:** spec file `telemetry/errors.py` serializes unsanitized
  paths/tokens via `record_exception()`.
- **Verification (already-handled):** the implementation never calls
  `record_exception()` and never emits tracebacks. Errors are bounded
  enums (`SAFE_ERROR_TYPES` / `SAFE_ERROR_MESSAGES`); rejected receiver
  payloads are recorded as metadata only (content-type + length, never raw
  bytes). Regression test: `test_error_handling`.
- **Correction:** none.

### FR1-08 — G2-P1-02 (P1, Security): resource detectors

- **Reviewer repro:** spec file `telemetry/provider.py` uses default OTel
  resource detectors (host/OS/process metadata).
- **Verification (already-handled):** every provider is built with an
  explicit `Resource({"service.name": ..., "agent.name": ...})`; no default
  detectors are invoked. Covered by the received-payload assertions in
  `test_no_secrets_in_otlp` (no host/user/path data in the serialized OTLP).
- **Correction:** none.

### FR1-09 — M2-P1-01 (P1, Integration): `claude` not on PATH

- **Reviewer repro:** `Get-Command claude` fails on the laptop; the binary
  lives at
  `C:\Users\kbaue\.vscode\extensions\anthropic.claude-code-2.1.318-win32-arm64\resources\native-binary\claude.exe`.
- **Correction:** `claude_child.resolve_claude_path()` — resolution order
  `FLEET_CLAUDE_PATH` env override → `claude` on PATH → newest VS Code
  extension install under `~/.vscode/extensions`. `launch_claude()` uses
  the resolved path; `run_claude_intent()` passes it through.
- **Regression tests:** `test_resolve_claude_path_env_override`,
  `test_resolve_claude_path_prefers_path`,
  `test_resolve_claude_path_missing`,
  `test_launch_claude_uses_env_override_path` (stub found via override
  while absent from PATH).

### FR1-10 — M2-P1-02 (P1, Integration): enhanced telemetry beta flag

- **Reviewer repro:** tracing requires `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`
  in addition to `CLAUDE_CODE_ENABLE_TELEMETRY=1`.
- **Verification (already-handled):** both flags are in
  `claude_child.CLAUDE_TELEMETRY_FLAGS` and asserted present in the child
  env by `test_stub_claude_parented_end_to_end`.
- **Correction:** none.

### FR1-11 — M2-P1-03 (P1, Integration): OTLP exporter, not console

- **Reviewer repro:** the Claude Code runtime scrubs `"console"` from
  `OTEL_TRACES_EXPORTER` in formatted modes.
- **Verification (already-handled):** `CLAUDE_TELEMETRY_FLAGS` sets
  `OTEL_TRACES_EXPORTER=otlp` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`,
  asserted in the child env by `test_stub_claude_parented_end_to_end`.
- **Correction:** none.

### FR1-12 — G1-P2-01 (P2, Architecture): approval.wait vendor attributes

- **Reviewer repro:** spec fixture decorates synthetic `approval.wait` spans
  with vendor AI attributes.
- **Verification (already-handled):** the fixture's `approval.wait` carries
  only `approval.kind = "synthetic"` and `synthetic = "true"` (plus ledger
  IDs). Asserted in `test_fixture_12_spans`.
- **Correction:** none.

### FR1-13 — G2-P2-01 (P2, Security): loopback-only exporter

- **Reviewer repro:** spec exporter permits non-loopback streaming.
- **Verification (already-handled):** `LocalReceiver` binds `127.0.0.1`
  by default; all endpoints in code and tests are loopback.
- **Correction:** none.

### FR1-14 — G2-P2-02 (P2, Quality): unbounded dynamic string attributes

- **Reviewer repro:** spec `schema/attributes.py` has no length bound on
  `handoff.reason`.
- **Correction:** `MAX_ATTRIBUTE_CHARS = 256` in `fleet_trace.py`;
  `sanitize_attributes()` truncates any string value longer than 256
  characters (before the email/token redaction check).
- **Regression test:** `test_attribute_length_guard` (300-char value →
  256 chars; short values and numbers unchanged).

### FR1-15 — G2-P3-01 (P3, Robustness): traceparent regex

- **Reviewer repro:** spec `propagation/w3c.py` uses a fragile regex for
  inbound `TRACEPARENT`.
- **Correction:** `child.py` now validates `TRACEPARENT` against the strict
  W3C Level 2 regex (`^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`) before
  extraction; a malformed header is dropped and logged to stderr, and the
  child runs unparented. (The SDK propagator also validates, but the
  explicit check makes the rejection deterministic and visible.)
- **Regression test:** `test_traceparent_validation` (well-formed header →
  parented with matching ids; uppercase hex → rejected, unparented).

## Notes

- **Fixture topology vs the reviewer's §3.3 DAG:** the reviewer's 12-span
  topology is illustrative; the builder's deterministic fixture is a
  different but valid 12-span DAG (child `agent.run` under the handoff
  span for the *controlled* child, which is a spawned subprocess — direct
  parenting is correct there). The external-boundary rule is now
  demonstrated separately via `export_external_agent_link` (FR1-06) rather
  than by changing the 12-span fixture.
- **M2 live run:** still pending on the dev machine (Claude Code installed
  and signed in). This round fixes the harness prerequisites (FR1-09/10/11);
  it does not claim the live `claude -p` run. The reviewer's §4.4
  controlled-child spec (mock Anthropic server) is the laptop-side next
  step after re-review.
- **Reviewer rule (Kyle, 2026-11-06):** the reviewer just needs to be
  different from the builder, so Antigravity covering both G1 and G2 is
  acceptable. No separate G1 reviewer is required.

## Test results (re-run for this round, 2026-11-06)

Command: `python3 scripts/run_offline_tests.py` (from `observability/`,
venv Python 3.12, OTel 1.45.1)

**27 run, 26 passed, 1 skipped, 0 failed, 18.0s.**

New tests in this round (9): `test_attribute_length_guard`,
`test_signal_handlers_flush_on_sigterm`, `test_signal_handlers_registered`,
`test_traceparent_validation`, `test_external_agent_span_link`,
`test_resolve_claude_path_env_override`,
`test_resolve_claude_path_prefers_path`,
`test_resolve_claude_path_missing`,
`test_launch_claude_uses_env_override_path`. The M1 suite grew 11 → 16
tests and the M2 suite 7 → 11 tests (one live test still skipped: no real
`claude` binary on this machine).

## Gate status after Fix Round 1

- **M1 acceptance:** NOT DECLARED. Fix Round 1 is a builder claim
  (Implemented + Tested), not acceptance.
- **Next required action:** Antigravity re-reviews this commit
  (`25643e8`, pushed to GitHub) from a laptop clone and records a new
  verdict in `g2-independent-review.md` (and G1 verification in
  `g1-verification.md`, both gates being Antigravity's per Kyle's rule).
  Re-review must verify the exact commit, rerun
  `scripts/run_offline_tests.py`, and check FR1-01 by cloning the repo.
