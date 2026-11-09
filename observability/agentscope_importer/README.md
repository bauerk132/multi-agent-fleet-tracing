# AgentScope importer (M5)

One metadata-only, versioned end-to-end import: fleet schema-v1 spans
are mapped to AgentScope canonical `0.1` documents, written to a
bounded local SQLite ledger, and acknowledged with a receiver receipt.

The full contract is in `CONTRACT.md` (same directory). This README is
the operator's view.

## What it accepts

| Input | How |
|---|---|
| Flattened fleet spans (receiver shape) | `import_spans` / JSON array file / JSONL file |
| OTLP/HTTP JSON payload (`resourceSpans`) | `import_otlp_payload` / JSON file |
| AgentScope canonical `0.1` document | `import_canonical_document` / JSON file |

OTLP protobuf (`application/x-protobuf`) is **refused** with a clear
error — JSON only. The same mapped document is the input the real
AgentScope importer accepts; see "Real-import check" below.

## Guarantees (the contract)

- **Native IDs preserved verbatim**: traceId → `run.native_id`,
  spanId → `span.native_id`; stored record ids are namespaced hashes,
  never replacements.
- **Deduplication**: finalized spans by `(trace_id, span_id)`,
  normalized events by `(source, producer, event_id)`. First write
  wins; a changed re-delivery is a visible `span_conflict`, never a
  silent overwrite. Identical content returns the original receipt
  with `already_imported=true`.
- **Late / out-of-order**: spans are sequenced by start time, not
  arrival order; a late span joins its run additively.
- **Unknown values**: carried as unknown, never invented. Dangling
  parents are nulled with a visible note; unknown span fields move
  under `attributes`; unknown outcomes become `unknown`.
- **Partial coverage**: a run with no spans imports as `partial`.
- **No raw payloads**: only mapped metadata, content hashes, and
  receipts are stored. Content/identity attributes the observers
  strip are dropped again by the mapper and counted.
- **Receipt on every step**: every import — including `unsupported`
  and `invalid` — returns a receipt with `import_id`, `quality`,
  `warnings`, the 11 stats keys, `run_ids`, token totals, and
  provenance.
- **Bounded**: at most 10,000 spans / 16 MiB per import; the ledger
  keeps at most 100 imports / 30 days (purge on import and via
  `purge`). No egress.

## Layout

- `contract.py` — the contract constants (schema version, source
  block, kind map, bounds, retention).
- `mapper.py` — fleet spans → canonical document (per trace).
- `ledger.py` — SQLite ledger (imports / spans / events / runs).
- `importer.py` — the import operation + receipt + quality ladder.
- `verify.py` — the CLI assertion scenario + the real-import check.
- `bridge.py` — frozen-snapshot validation, privacy allowlist, loopback transport, and semantic readback.
- `cli.py` — `import` / `deliver` / `verify` / `receipt` / `purge` / `stats`.

## CLI

Run from the `observability/` directory:

```
python -m agentscope_importer verify
python -m agentscope_importer import <file> [--producer NAME]
python -m agentscope_importer deliver <file> [--data-dir DIR] [--producer NAME]
    [--server http://127.0.0.1:8765] [--synthetic-only]
python -m agentscope_importer receipt <import_id>
python -m agentscope_importer purge
python -m agentscope_importer stats
```

`verify` runs the full contract assertion (15 checks) against a fresh
temporary ledger and exits 0 only if every check passes. It never
touches the default ledger (`~/.local/share/fleet-tracing/`,
or `$FLEET_LEDGER_DIR`).

`deliver` accepts a canonical `0.1` document, a JSON array/JSONL of flattened
Fleet spans, or OTLP/HTTP JSON. It reads the source once into a bounded
16 MiB byte snapshot, validates the source and mapped canonical document
against the M4 metadata allowlist and a restricted safe-value format
(rejecting prose, credential-like values, and absolute paths), imports that
exact snapshot into the local Fleet ledger, and POSTs the deterministic
`canonical_bytes(document)` bytes. Validation happens before either local
persistence or upload.
The result keeps the raw snapshot SHA-256 separate from each canonical-upload
SHA-256 and records a per-trace crosswalk between Fleet run IDs and locally
predicted AgentScope import/run IDs. Warning counts preserve duplicate warning
occurrences. Token totals are `null` when no complete total is known; an
explicit reported zero remains `0`.

The endpoint must be plain HTTP on an explicit literal loopback IP
(`127.0.0.0/8` or `::1`). DNS names, environment proxies, and redirects are
refused. Supply the one-launch token through `AGENTSCOPE_TOKEN` or the hidden
interactive prompt; there is no token flag. Delivery requires the explicit
`--synthetic-only` flag acknowledging the snapshot is synthetic test data;
without it the command refuses before prompting for a token, and the bridge
itself refuses any delivery without explicit acknowledgment. Use a dedicated
`--data-dir` for isolated synthetic runs.

The bridge checks the POST and persisted ImportReports, RunDetail status,
quality, tree mode and capabilities, RunSummary timestamps, and every returned
SpanView's IDs, parentage, timestamps, duration, status, attributes,
LLM/tool/error metadata, and absence of payload/cost fields. It accepts only
the exact safe AgentScope source reference `{"pointer":"/spans/<index>"}`
for each span. Span readback uses explicit `limit=200` and increasing offsets
(the endpoint's plain-list contract; its maximum is 200), and fails on repeated
or non-progressing pages. Authentication failure stops remaining run uploads;
those runs are reported as unattempted. ImportReport `partial`
quality remains partial; the bridge does not upgrade the AgentScope quality
to the Fleet receipt's separate quality ladder. AgentScope exposes no event
readback endpoint, so the bridge refuses documents with non-empty events
before local persistence or transmission. `verified` means the exposed API
projection matched these checks; it is not direct database inspection, and
RunDetail itself does not expose the native trace ID or import ownership.
Those are linked through the persisted import's run IDs and each span's
native ID/`fleet.trace` metadata. No live integration proof is claimed until
an operator runs the command against the pinned isolated server.

Run the synthetic bridge tests and importer contract check from `observability/`:

```
python -m unittest tests.test_agentscope_bridge -v
python -m agentscope_importer verify
```

## Real-import check

`observability/examples/verify_agentscope_import.py` maps a synthetic
Copilot turn through the mapper and runs the resulting document
through the **real** AgentScope `import_bytes` (via
`make_importer`), asserting acceptance + idempotency. It needs:

- an AgentScope checkout at `$AGENTSCOPE_SRC`
  (`backend/src/agentscope/ingest/pipeline.py`), and
- pydantic in the running interpreter.

The fleet test venv has no pydantic by design; run the example with
the dedicated venv:

```
AGENTSCOPE_SRC=~/workspace/agentscope-src \
  ~/workspace/agentscope-venv/bin/python \
  observability/examples/verify_agentscope_import.py
```

When either is missing the script prints SKIP with the reason and
exits 0 — the check is evidence, not a gate the suite depends on.

## What M5 does NOT do

No dashboard or UI, no remote backend, no changes to the AgentScope
repo or the trace collector, no live vendor runs, no merge to main.
The collector's `build_feed` only sums spans named exactly `"llm"`
(a collector-side quirk) is a recorded follow-up, not part of this
contract: token totals are carried on `span.llm` here.
