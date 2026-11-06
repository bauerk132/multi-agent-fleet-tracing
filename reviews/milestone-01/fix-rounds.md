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

---

# Milestone 1 — Fix Round 2 (Antigravity re-review of Fix Round 1, 2026-11-06)

**Review under fix:** Antigravity G1/G2 re-review of staging commit
`dad1d48` (HiveMind handoff `hof_01M6SFABQNEAVFCX3R`, update 2026-11-06
22:27 UTC; episode memory `mem_01M6SFJPE0GSWQXRK3`).
**Verdicts being fixed:** physical staging **PASSED**; security/privacy
controls **VERIFIED** (G2-P0-01, G2-P0-02, G2-P2-02 passed isolation
tests); offline test suite **17 passed / 10 failed on the Windows host** —
**FIX ROUND 2 REQUIRED** (Windows test harness compatibility).
**Fix-round commit (local git):** `1928d1e` — "Fix Round 2: Windows test
harness compatibility (stub extension, env allowlist keys, SIGTERM exit
code, SystemRoot)" (5 files, +261/−15).
**Branch:** `staging`. **Date:** 2026-11-06. **Builder:** Muse.

> **How to read this round:** all 10 failures are test-harness /
> platform-compatibility issues, not production-code defects. The
> production code is unchanged except one testability refactor
> (`_install_signal_handlers` now returns the handler it registered).
> The 10 failures reconstruct exactly as 6 M2 stub tests + 4 M1 tests:
> the M2 stub was invisible to Windows PATH lookup; the env-allowlist
> assertion was POSIX-strict; the SIGTERM subprocess test asserted a
> POSIX exit code; hand-built child envs omitted SystemRoot. No
> interface, dependency, or unrelated production config changes.

## Findings

### FR2-01 — M2 stub extension (6 tests): extensionless `claude` stub invisible on Windows

- **Reviewer repro:** on the Windows host, `test_claude_child` creates a
  stub file named `claude` with no extension; Windows `shutil.which()`
  (PATHEXT) cannot find it, so resolution fell back to the real
  unauthenticated VS Code `claude.exe` binary and the stub tests ran
  against it. Affected: `test_stub_claude_parented_end_to_end`,
  `test_no_secrets_in_claude_child_env`,
  `test_stub_claude_missing_traceparent_unparented`,
  `test_stub_claude_nonzero_exit`,
  `test_resolve_claude_path_prefers_path`,
  `test_launch_claude_uses_env_override_path`.
- **Root cause:** POSIX-only stub naming. Windows executes scripts via
  file extension; an extensionless file is not a runnable command.
- **Correction:** platform-aware stub in `test_claude_child.setUp` — on
  Windows the stub is `claude.cmd` (batch wrapper,
  `@python "%~dp0claude.py" %*`) plus `claude.py` (the stub code); on
  POSIX it remains the executable script `claude`. `self.stub_path`
  records the per-platform path; the resolve/override tests use it.
  No change to `claude_child.py` — `shutil.which` is already
  PATHEXT-aware and `subprocess` runs `.cmd` files via `cmd.exe`.
- **Regression test:** the 6 affected tests above (they now run the
  stub on both platforms; on this Linux host they pass unchanged).

### FR2-02 — env allowlist assertion POSIX-strict (1 test): Windows SYSTEMROOT / PROCESSOR_ARCHITECTURE

- **Reviewer repro:** `test_env_allowlist` failed on the Windows host —
  the child env contained `SYSTEMROOT` and `PROCESSOR_ARCHITECTURE`
  beyond the allowlist.
- **Root cause:** the assertion compared keys exactly and assumed a
  POSIX key set. Windows environment names are case-insensitive
  (`SystemRoot` vs `SYSTEMROOT`), and Windows surfaces
  `PROCESSOR_ARCHITECTURE` in child processes.
- **Correction:** the test now compares keys upper-cased (Windows env
  names are case-insensitive) and permits `PROCESSOR_ARCHITECTURE`
  explicitly on Windows only, documented as a system identifier, not a
  secret carrier. The security-critical assertions are unchanged:
  no `FAKE_API_KEY` / `MY_TOKEN` keys reach the child and no secret
  values appear in the serialized OTLP.
- **Regression test:** `test_env_allowlist`.

### FR2-03 — hand-built child envs omit SystemRoot (2 tests, +1 preventive)

- **Reviewer repro:** child spawners failed on the Windows host when the
  env lacked SystemRoot. Affected: `test_missing_invalid_traceparent`,
  `test_early_termination`.
- **Root cause:** the tests hand-built a minimal env (`PATH` + OTel /
  FLEET vars). The Python interpreter cannot start on Windows without
  SystemRoot (it locates system DLLs through it).
- **Correction:** new test helper `_child_env_base()` returns the minimal
  env and adds `SystemRoot` from the parent env on Windows; used by
  `test_missing_invalid_traceparent`, `test_early_termination`, and
  preventively by `test_traceparent_validation` (same hand-built pattern).
- **Regression test:** the three tests above.

### FR2-04 — SIGTERM subprocess test asserts POSIX exit code (1 test)

- **Reviewer repro:** `test_signal_handlers_flush_on_sigterm` asserts
  `rc == -15`. On Windows, `os.kill`/`send_signal` with SIGTERM ends
  the process via TerminateProcess, which bypasses Python signal
  handlers; the process exits 1, not -15.
- **Root cause:** the end-to-end delivery of a SIGTERM flush is
  POSIX-only. The handler logic itself is platform-independent.
- **Correction:** (a) the subprocess test is skipped on Windows
  (`skipIf os.name == "nt"`, with the reason recorded); (b) the
  handler logic is now directly testable — `_install_signal_handlers`
  returns the handler it registered — and a new test
  `test_signal_handler_flushes_directly` invokes it with `os.kill`
  patched, asserting `force_flush` + `shutdown` were called, the
  re-signal was issued with the original signum, and the default
  handler was restored first. The `fleet_trace.py` docstring records
  the Windows limitation (atexit + explicit shutdown remain the
  Windows guarantees).
- **Regression test:** `test_signal_handler_flushes_directly` (runs on
  every platform); `test_signal_handlers_flush_on_sigterm` (POSIX).

## Test results (re-run for this round, 2026-11-06)

Command: `python3 scripts/run_offline_tests.py` (from `observability/`,
venv Python 3.12, OTel 1.45.1)

**28 run, 27 passed, 1 skipped, 0 failed, 18.4s.**

New test in this round (1): `test_signal_handler_flushes_directly`.
The M1 suite grew 16 → 17 tests; the M2 suite stays at 11 tests (one
live test still skipped: no real `claude` binary on this machine).

**Windows verification caveat:** this VM is Linux, so the Windows-side
behavior is verified by reasoning + the reviewer's Windows run, not by
executing the suite on Windows here. The re-review on the laptop is the
Windows verification step for this round.

## Gate status after Fix Round 2

- **M1 acceptance:** NOT DECLARED. Fix Round 2 is a builder claim
  (Implemented + Tested), not acceptance.
- **Next required action:** Antigravity re-reviews this commit from a
  laptop clone and records a new verdict in `g2-independent-review.md`
  (and G1 verification in `g1-verification.md`, both gates being
  Antigravity's per Kyle's rule). Re-review must verify the exact
  commit, rerun `scripts/run_offline_tests.py` on Windows, and confirm
  the 10 failures from the previous round are gone.

---

# Milestone 2 — Fix Round 3 (Antigravity re-review of Fix Round 2, 2026-11-06)

**Review under fix:** Antigravity G1/G2 re-review of staging commit
`1d885e8a` (HiveMind handoff `hof_01M6SGG3E1DT6ZDTTV`, update 2026-11-06
23:52 UTC).
**Verdicts being fixed:** M1 **PASSED** (16 passed, 1 skipped, 0 failed);
M2 **FAIL** — 6 failures in `test_claude_child.py`.
**Fix-round commit (local git):** `885ae9f` — "Fix Round 3 (M2-only):
as_posix() for stub OUT_FILE path, normcase() for .CMD vs .cmd"
(3 files, +124/−7).
**Branch:** `staging`. **Date:** 2026-11-06. **Builder:** Muse.

> **How to read this round:** M1 is accepted by this review (both gates,
> Antigravity's, per Kyle's rule). This round is M2-only: two one-line
> test fixes, no production-code change at all. The 6 failures split
> 5 + 1 with two distinct root causes.

## Findings

### FR3-01 — stub template bakes Windows backslashes into generated Python (5 tests): SyntaxError: unicodeescape

- **Reviewer repro:** on the Windows host, the generated `claude.py`
  stub contained `OUT_FILE = "C:\Users\kbaue\..."`; `\U` is a unicode
  escape, so the stub failed to parse (`SyntaxError: unicodeescape`)
  and every test that runs the stub failed:
  `test_stub_claude_parented_end_to_end`,
  `test_no_secrets_in_claude_child_env`,
  `test_stub_claude_missing_traceparent_unparented`,
  `test_stub_claude_nonzero_exit`,
  `test_launch_claude_uses_env_override_path`.
- **Root cause:** `STUB_CLAUDE.replace("__OUT_FILE__", str(self.env_file))`
  baked a Windows path with backslashes into generated Python source
  as a string literal.
- **Correction:** use `self.env_file.as_posix()` — forward slashes are
  valid in a Python string literal on every platform and Windows
  accepts them in paths. One line in `test_claude_child.setUp`.
- **Regression test:** the 5 affected tests above.

### FR3-02 — `.CMD` vs `.cmd` case comparison (1 test)

- **Reviewer repro:** `test_resolve_claude_path_prefers_path` failed on
  the Windows host — `shutil.which()` returns the extension in PATHEXT
  case (`claude.CMD`) while the file on disk is `claude.cmd`.
- **Root cause:** exact string comparison of a Windows path, where
  case is not significant.
- **Correction:** compare with `os.path.normcase()` on both sides
  (no-op on POSIX, lower-cases + normalizes separators on Windows).
  One line in `test_claude_child.py`.
- **Regression test:** `test_resolve_claude_path_prefers_path`.

## Test results (re-run for this round, 2026-11-06)

Command: `python3 scripts/run_offline_tests.py` (from `observability/`,
venv Python 3.12, OTel 1.45.1)

**28 run, 27 passed, 1 skipped, 0 failed, 18.3s.**

No new tests in this round. The M1 suite stays at 17 tests; the M2
suite stays at 11 tests (one live test still skipped: no real `claude`
binary on this machine).

**Windows verification caveat:** this VM is Linux, so the Windows-side
behavior is verified by reasoning + the reviewer's Windows run, not by
executing the suite on Windows here. The focused re-test on the laptop
is the Windows verification step for this round.

## Gate status after Fix Round 3

- **M1 acceptance:** DECLARED — Antigravity G1+G2 passed M1 on staging
  `1d885e8a` (16 passed, 1 skipped, 0 failed). Milestone 1 is accepted.
- **M2 acceptance:** NOT DECLARED. Fix Round 3 is a builder claim
  (Implemented + Tested), not acceptance.
- **Next required action:** Antigravity runs one focused re-test of the
  M2 harness on the laptop (staging commit for this round) and hands
  back the outcome. Per Kyle (2026-11-06), after that focused test the
  work moves on to Milestone 3.
