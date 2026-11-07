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
- `cli.py` — `import` / `verify` / `receipt` / `purge` / `stats`.

## CLI

Run from the `observability/` directory:

```
python -m agentscope_importer verify
python -m agentscope_importer import <file> [--producer NAME]
python -m agentscope_importer receipt <import_id>
python -m agentscope_importer purge
python -m agentscope_importer stats
```

`verify` runs the full contract assertion (14 checks) against a fresh
temporary ledger and exits 0 only if every check passes. It never
touches the default ledger (`~/.local/share/fleet-tracing/`,
or `$FLEET_LEDGER_DIR`).

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
