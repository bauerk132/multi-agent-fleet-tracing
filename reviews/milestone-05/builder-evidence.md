# Milestone 5 — Builder Evidence (headless AgentScope/importer contract)

**Status: BUILT, awaiting Antigravity G1/G2.** M5 is NOT accepted —
only the reviewer's verdict can do that.

**Repo:** `bauerk132/multi-agent-fleet-tracing`, branch `staging`.
**Staging commit:** `02de365137dea73273304c3b313ee5cc342627aa`
(verified as the staging head on GitHub; parent `e63a436a`, the
first M5 snapshot).
**Local commits:** code `4866320`, docs `ae52f08`, double-check
fixes `1751a62`. The staging hash recorded here was committed
locally after the push (it rides on the next snapshot push, per
the FR8 pattern); the handoff to Antigravity names the same hash.
**Date:** 2026-11-07.

## What M5 proves

One metadata-only, versioned end-to-end import: fleet schema-v1 spans
are mapped to an AgentScope canonical `0.1` document, written to a
bounded local SQLite ledger, and acknowledged with a receiver receipt.
The same mapped document is the input the real AgentScope importer
accepts — proven by running it through the real `import_bytes`
(quality `complete`, idempotent).

## Contract (full definition)

`observability/agentscope_importer/CONTRACT.md` — read it before
reviewing. Summary of the guarantees:

- Native IDs preserved verbatim (traceId → `run.native_id`,
  spanId → `span.native_id`); stored record ids are namespaced
  hashes, never replacements.
- Dedup: finalized spans by `(trace_id, span_id)`, normalized events
  by `(source, producer, event_id)`; first write wins; a changed
  re-delivery is a visible `span_conflict` (the comparison covers
  name, kind, status, parent, times **and attributes** — an
  attribute-only change is a conflict, not a silent duplicate).
- Late/out-of-order: spans sequenced by start time, not arrival;
  late spans join their run additively.
- Unknown values carried as unknown, never invented; dangling
  parents nulled with a visible note; unknown span fields moved
  under `attributes`; unknown outcomes → `unknown`.
- Partial coverage: a run with no spans imports as `partial`.
- No raw-payload storage (proven with canary strings); no egress.
- Receipt on every step, including `unsupported`/`invalid`.
- Bounded: 10,000 spans / 16 MiB per import; ledger keeps at most
  100 imports / 30 days.
- Tokens carried on `span.llm` with `supports_token_usage=true`
  (the M3/M4 follow-up: the collector's `build_feed` only sums spans
  named exactly `"llm"`, so Copilot tokens reported 0 — that is a
  collector-side quirk, out of scope here; the import contract
  carries real token totals).

## Files (new, all in this repo)

| File | Role |
|---|---|
| `observability/agentscope_importer/CONTRACT.md` | The contract definition |
| `observability/agentscope_importer/contract.py` | Contract constants |
| `observability/agentscope_importer/mapper.py` | Fleet spans → canonical 0.1 document |
| `observability/agentscope_importer/ledger.py` | SQLite ledger (imports/spans/events/runs) |
| `observability/agentscope_importer/importer.py` | Import operation + receipt + quality ladder |
| `observability/agentscope_importer/verify.py` | CLI assertion scenario + real-import check |
| `observability/agentscope_importer/cli.py` | `import` / `verify` / `receipt` / `purge` / `stats` |
| `observability/agentscope_importer/__main__.py` | `python -m agentscope_importer` entry |
| `observability/agentscope_importer/__init__.py` | Public API |
| `observability/agentscope_importer/README.md` | Operator's view |
| `observability/tests/test_agentscope_importer.py` | 44 unit tests |
| `observability/examples/verify_agentscope_import.py` | Real-import evidence script |

## The actual AgentScope importer (grounding)

Repo `bauerk132/agentscope`, branch `ags/sprint1-staging`, commit
`95f8a68144f7b64d766578091d423c98af1e334c`. Importer:
`backend/src/agentscope/ingest/pipeline.py` — `import_bytes(conn,
data_dir, data, filename)` / `make_importer(data_dir)` (the API's
entry point); adapters: canonical (`schema_version "0.1"`) and
rag-agent (JSONL) only; no OTLP adapter (a `resourceSpans` document
returns `unsupported`). Quality ladder (first match wins):
`unsupported` → `invalid` (no run) → `partial` (a run dropped,
unreadable records, or any run partial) → `warnings` → `complete`.
Whole-upload idempotency by content sha256; refused imports
recorded so the import id resolves. Limits: 64 MiB upload,
2,000,000 values, 100,000 runs, 100,000 spans/record, JSON depth 64.

## Test results

Full offline suite (`observability/scripts/run_offline_tests.py`,
repo `.venv`, Python 3.12.3, OTel 1.45.1):

```
Ran 137 tests in 55.0s — OK (skipped=2)
```

- Baseline before M5: 93 run, 92 passed, 1 skipped, 0 failed.
- New: 44 M5 tests, all passing under the repo venv (1 skip: the
  real-import test, which needs `AGENTSCOPE_SRC` + pydantic).
  Two of the 44 are regression tests for the idempotency-key gap
  the double-check pass found and fixed (see build-notes.md).
- The 2 skips in the full run are the pre-existing live-Claude test
  and the real-import test. No regressions (M1–M4 suites unchanged).

CLI assertion (`python -m agentscope_importer verify`, fresh temp
ledger): **15/15 checks passed** — happy-path complete; duplicate
delivery; content stripping (warnings + canary absent); out-of-order
arrival; late span; missing parent; unknown values; partial coverage;
duplicate native id; parent cycle; restart recovery; native-ID
preservation; no raw-payload storage; OTLP content types (JSON
accepted, protobuf refused); receipt on every step.

Real-import check
(`AGENTSCOPE_SRC=~/workspace/agentscope-src`,
`~/workspace/agentscope-venv/bin/python` — pydantic 2.14.3, separate
venv outside the fleet repo so the snapshot push never sweeps it in):

```
PASS: real importer accepted the document (quality=complete,
run_ids=['run_629b9f07f1be861d'], idempotent)
```

The mapped document went through the real `make_importer` /
`import_bytes` (with `apply_migrations`, as the API does at startup)
into a temporary data dir. All 44 M5 tests also pass under that
interpreter (0 skipped).

## Staging push

- First snapshot: `e63a436a5ff893daff0b93cefc6b002227168f27`
  (parent `68b53aee`, the M4 FR8 head). Verified independently
  from GitHub: head listed, 7 key files read back and hashed,
  all matching the committed versions.
- Second snapshot (double-check fixes):
  `02de365137dea73273304c3b313ee5cc342627aa`. Verified the same
  way: head listed, all 7 changed files read back and hashed,
  all matching local commit `1751a62`.
- Sync record:
  `~/workspace/multi-agent-fleet-tracing.muse-github-snapshot-sync.json`
  (16 entries). No merge to main.

## Design decisions (see build-notes.md for the full list)

1. One upload = one canonical document per trace; the content sha
   covers all documents in the upload (sorted by trace id).
2. Graph transcribed from recorded parent links, labelled as such
   (`graph: transcribed-from-parentage` in run metadata) — a faithful
   transcription of observed structure, not inferred state.
3. The ledger stores mapped attributes (`attributes_json`) so the
   record is auditable and attribute changes are detectable as
   conflicts.
4. Sanitization is a correction: a turn that needed stripping imports
   as `warnings` (the real importer's ladder: anything dropped is a
   warning); a pre-sanitized turn imports as `complete`.
5. Identical content always returns the original receipt
   (`already_imported=true`) — where "identical" means the same
   mapped documents AND the same input accounting (unreadable
   items, dropped runs, mapping notes). The double-check found
   the first key covered only the documents, so a duplicate-content
   upload with a bad line returned the original receipt and hid the
   skipped line; fixed with `_outcome_sha` (see build-notes.md).
   A deterministic mapping makes the real importer's
   refused-upgrade path vacuous here, so it is not implemented
   (documented in the code).
6. OTLP protobuf is refused, never silently dropped; OTLP/HTTP JSON
   is accepted and flattened with events preserved.

## Open follow-ups (not blockers)

- The collector's `build_feed` only sums spans named exactly `"llm"`
  (collector-side quirk; token totals are carried correctly here).
- The AgentScope checkout at `~/workspace/agentscope-src/` was
  missing `backend/src/agentscope/store/migrations/0001_init.sql`
  when assembled; the file was fetched from GitHub at commit
  `95f8a68` (sha `e0d43049…`, 8,465 bytes) and the checkout is now
  complete for the real-import run. The checkout lives outside the
  fleet repo, so the snapshot manifest never sweeps it in.
- `pydantic` lives in a separate venv (`~/workspace/agentscope-venv/`,
  2.14.3) for the same reason; the fleet repo venv stays OTel-only.

## Review ask (Antigravity, G1 + G2)

Verify M5 against the contract gates in CONTRACT.md §10/§12:
the mapping, the ledger, the receipt, the CLI assertion, and the
real-import evidence — including an installed-build/real-import
check on the laptop where applicable (AgentScope checkout +
pydantic). Verdict applies only to the exact staging commit named
above.
