# Milestone 1 — Builder Evidence (G1 packet)

**Milestone:** 1 — Local tracing foundation (safe local receiver + coordinator + deterministic 12-span fixture)
**Candidate commit (local git):** `9a2a97a` — "Milestone 1: local receiver + coordinator + 12-span fixture (11/11 tests)"
**Branch:** `main`
**Working tree at packet time:** clean (`git status --short` empty)
**Date:** 2026-11-06
**Builder:** Muse (implementation agent)

> **Scope note for reviewers:** the GitHub snapshot commit `cf888dc9` (pushed 2026-11-06) contains Milestone 1 **and** Milestone 2 files together in one commit, because the connector snapshot push uploads the whole tree. Milestone 1-only review must use local commit `9a2a97a`. Milestone 2's harness (`observability/claude_child.py`, `observability/tests/test_claude_child.py`, plus small `receiver.py`/`README.md` changes) lives in the later local commit `d98289f` and is **not** part of this Milestone 1 candidate. The Milestone 2 harness was built before the G1/G2 protocol arrived, on Kyle's direct instruction to move to Milestone 2; no further Milestone 2 changes are being made while Milestone 1's gates are outstanding. **Exception:** Fix Round 1 (commit `25643e8`) did change the M2 harness (`claude_child.py` path resolution) because the Antigravity review's M2 findings required it — see `fix-rounds.md`.

## 1. What was built (commit 9a2a97a, 15 files, +1948 lines)

| File | Role |
|---|---|
| `observability/fleet_trace.py` | Coordinator: env-only config, composite W3C propagators, task/session IDs once per intent, fixed child env allowlist, external-handoff correlation, attribute sanitizer, force_flush(5000) + shutdown + atexit flush |
| `observability/receiver.py` | Stdlib-only localhost OTLP/HTTP JSON receiver, `/v1/traces`, in-memory + JSONL output |
| `observability/fixture.py` | Deterministic 12-span fixture (trace `a1b2c3d4e5f60718293a4b5c6d7e8f90`) |
| `observability/child.py` | Instrumented child: extract + validate parenting, JSON report on stdout |
| `observability/otlp_json.py` | OTLP JSON encoder + stdlib urllib exporter |
| `observability/requirements.txt` | `opentelemetry-api==1.45.1`, `opentelemetry-sdk==1.45.1`, `opentelemetry-exporter-otlp-proto-http==1.45.1` |
| `observability/tests/test_fleet_trace.py` | 11 tests |
| `observability/hermes_plugin/` | UNVERIFIED stubs, marked do-not-install (`__init__.py`, `plugin.yaml`) |
| `observability/examples/` | Unverified doc examples (`copilot-settings.json`, `antigravity-hooks.json`) |
| `observability/README.md` | Setup, quick start, transport note, limitations |
| `publish.py` | Repo publish helper (git + `gh`) |
| `.gitignore` | `.venv/`, `__pycache__/`, `observability/out/` |

No production configuration, CI, credentials, or unrelated files were modified. No secrets are in the tree (see §5).

## 2. How to reproduce

```bash
cd multi-agent-fleet-tracing
python3 -m venv .venv
.venv/bin/pip install -r observability/requirements.txt

# 1. Start the local receiver
.venv/bin/python observability/receiver.py --out observability/out/received.jsonl
# listening on http://127.0.0.1:<PORT>/v1/traces

# 2. Run the coordinator end to end (same endpoint)
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:<PORT>/v1/traces
.venv/bin/python observability/fleet_trace.py

# 3. Export the deterministic 12-span fixture
.venv/bin/python observability/fixture.py --endpoint http://127.0.0.1:<PORT>/v1/traces

# 4. Run the tests
cd observability && ../.venv/bin/python -m unittest tests.test_fleet_trace -v
```

## 3. Test results (re-run for this packet, 2026-11-06)

Command: `../.venv/bin/python -m unittest tests.test_fleet_trace -v` (from `observability/`, venv Python 3.12, OTel 1.45.1)

**11 run, 11 passed, 0 failed, 7.45s.**

| Test | Result |
|---|---|
| test_one_intent_multiple_handoffs | ok |
| test_controlled_child_propagates | ok |
| test_external_handoff | ok |
| test_handoff_fields_nonempty | ok |
| test_error_handling | ok |
| test_no_secrets_in_otlp | ok |
| test_env_allowlist | ok |
| test_missing_invalid_traceparent | ok |
| test_graceful_flush | ok |
| test_early_termination | ok |
| test_fixture_12_spans | ok |

## 4. Live end-to-end evidence (re-run for this packet, 2026-11-06)

Receiver on `http://127.0.0.1:4318/v1/traces`; coordinator run, then fixture export.

- **Coordinator run:** `child_parented: true`, `flush_ok: true`, `ok: true`. Child env keys observed: `FLEET_HANDOFF_REF`, `FLEET_SESSION_ID`, `FLEET_TASK_ID`, `LC_CTYPE`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `PATH`, `TRACEPARENT` (see §5 for the `LC_CTYPE` note).
- **Receiver got 19 spans total: 12 fixture + 7 coordinator.**
  - Fixture trace `a1b2c3d4e5f60718293a4b5c6d7e8f90` (12 spans): `agent.run` ×2, `agent.handoff` ×2, `subagent.spawn` ×1, `llm.call` ×1, `tool.exec` ×2, `retrieval.search` ×1, `state.write` ×2, `approval.wait` ×1. Child `agent.run` parent = `3333333333333333` (the fixture handoff span). ✓
  - Coordinator trace `e5dafedb87cd62614537927766a29d7d` (7 spans): child `agent.run` parent = the live handoff span id. ✓
- **Fixture span inventory (deterministic):** root `agent.run 1111…` → `state.write 2222…`, `agent.handoff 3333…` (→ child) → child `agent.run 4444…` → `llm.call 5555…` → `tool.exec 6666…` (ok), `tool.exec 7777…` (FAILED, ToolError), `subagent.spawn 8888…`, `retrieval.search 9999…`, `state.write aaaa…`; `agent.handoff bbbb…` (→ external) and `approval.wait cccc…` (labeled synthetic) under root.

## 5. Security evidence

- **Env allowlist (fixed per S1):** child processes receive only `PATH`, `SystemRoot`, `TEMP`, `TMP`, `PYTHONUTF8`, `PYTHONIOENCODING`, `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, plus explicit `TRACEPARENT`/`TRACESTATE`/`BAGGAGE` and `FLEET_*` IDs. No `*_KEY`/`*_TOKEN`/`*_SECRET`/`*_PASSWORD`/auth headers. Covered by `test_env_allowlist` (injects a fake secret into the parent env and asserts it is absent from the child).
- **No secrets in telemetry:** `test_no_secrets_in_otlp` injects synthetic secrets/PII and asserts none appear in the received serialized OTLP. Passed.
- **Sanitizer:** attribute sanitizer drops non-allowlisted keys; a bug caught by tests (substring check dropped legitimate `state.key`) was fixed by relying on the explicit key allowlist.
- **`LC_CTYPE` note:** `LC_CTYPE` appears in the child env but is runtime-added by CPython (PEP 538 locale coercion inside the child, even with `env -i`), not a leak. The allowlist is unchanged; the test excludes it with a comment.
- **Error handling:** failed `tool.exec` gets `error.type=ToolError`, generic redacted message, ERROR status (`test_error_handling`). Non-zero child exit / timeout → ERROR on the handoff span with safe generic metadata only.

## 6. Known limitations and uncertainties (for G1/G2)

1. **No real agent logs exist on this machine** (`~/.claude`, `~/.codex`, `~/.gemini` absent). Parsers and propagation are proven on fixtures and the instrumented Python child only — not against real Claude Code/Codex/Antigravity output.
2. **No official OTLP JSON exporter on PyPI** (`opentelemetry-exporter-otlp-proto-json` does not exist). The local loop uses a small stdlib OTLP-JSON exporter (`otlp_json.py`); the pinned proto-http (protobuf) exporter is installed but has **not** been exercised against a real collector.
3. **Fixture parents need the SAMPLED flag** — the SDK's default ParentBased sampler drops children of an unsampled parent. Documented in the README; a real deployment must set sampling explicitly.
4. **SIGKILL / power loss:** no flush guarantee. `test_early_termination` covers the `sys.exit` path only.
5. **Hermes / Copilot / Antigravity / ChatGPT desktop:** unverified (Milestones 3–4); only stubs/doc examples exist. The plan page's child-extraction sketch originally had wrong import paths (`opentelemetry.propagate.composite`, `TraceContextPropagator` do not exist in 1.45.1); the working imports are `opentelemetry.propagators.composite.CompositePropagator` and `opentelemetry.trace.propagation.tracecontext.TraceContextTextMapPropagator` — the plan page was corrected.
6. **AgentScope importer schema:** not validated (repo not located). `state.write` is coordinator-local evidence only until then.
7. **GitHub snapshot caveat:** the pushed commit `cf888dc9` bundles M1+M2 files; reviewers must use local commit `9a2a97a` for a Milestone 1-only review (see scope note above).

## 7. Gate status

- **Antigravity review RECEIVED 2026-11-06** (HiveMind handoff
  `hof_01M6RNMAQA9H660H2D`): M1 **FAIL** (blocked by missing repository
  artifacts — the code was never staged on the laptop, so test claims were
  uncorroborated from its side); M2 **NEEDS CHANGES** (Claude Code 2.1.318
  natively supports OTel and accepts TRACEPARENT in `claude -p`/SDK, with
  5 prerequisites). Full report pasted to the builder by Kyle.
- **Reviewer rule (Kyle, 2026-11-06):** the reviewer just needs to be
  different from the builder, so Antigravity covering both G1 and G2 is
  acceptable. No separate G1 reviewer is required.
- **Fix Round 1 DONE** — see `fix-rounds.md` (commit `25643e8`, 9 new
  tests, 27 run / 26 passed / 1 skipped). Builder claims Implemented +
  Tested only.
- **Milestone 1 acceptance:** NOT DECLARED.
- **Next required action:** Antigravity re-reviews commit `25643e8` from a
  laptop clone (see the re-review handoff in HiveMind) and records new
  verdicts in `g1-verification.md` / `g2-independent-review.md`.

## 8. Status vocabulary (Engineering Quality & Security Standards §2)

- **Implemented** — code exists. **Tested** — the test was actually executed. **Verified** — someone independently reproduced the evidence. **Accepted** — the required review gates passed.
- This packet claims **Implemented** and **Tested** only (11/11, re-run by the builder for this packet). **Verified** is G1's job; **Accepted** requires both gates. The builder does not review its own work and awards no G1 or G2 approval.

## 9. Fix Round 1 (2026-11-06, commit `25643e8`)

Antigravity's review (M1 FAIL / M2 NEEDS CHANGES) was answered in
`fix-rounds.md`: 15 findings, 6 fixed in code (signal handlers, 256-char
guard, traceparent regex, claude.exe resolution, span-link case, offline
test runner), 9 verified already-handled in code with existing regression
tests. Suite grew to **27 run, 26 passed, 1 skipped** (live-Claude test),
re-run via `python3 scripts/run_offline_tests.py`. The fix-round commit is
pushed to GitHub; Antigravity re-reviews it from a laptop clone.
