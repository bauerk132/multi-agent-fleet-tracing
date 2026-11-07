# Milestone 5 — Fleet → AgentScope import contract (v1)

**Status:** Implemented + Tested. Acceptance NOT declared — requires
independent review (Antigravity) plus the real-import check on the
reviewer's machine.

**Sources (read, not guessed):**
- Plan §10 ledger + §12 gate 4 (Notion: "Multi-Agent Fleet Tracing —
  OpenTelemetry Integration Plan"): prove one metadata-only, versioned
  end-to-end import with native IDs preserved, safe state references,
  deduplication, late/out-of-order events, unknown values, partial
  coverage and receiver receipt. CLI assertion first; dashboard is NOT
  the entry gate.
- The actual AgentScope importer: repo `bauerk132/agentscope`,
  branch `ags/sprint1-staging`, commit
  `95f8a68144f7b64d766578091d423c98af1e334c`,
  `backend/src/agentscope/ingest/` — `pipeline.py` (`import_bytes`,
  `make_importer`), `detect.py` (adapters: `CanonicalAdapter`,
  `RagAgentAdapter`), `adapters/canonical.py` (schema `"0.1"`),
  `adapters/rag_agent.py`, `validate.py`, `normalize.py`,
  `contracts/trace.py`, `contracts/api.py`, `store/repos/spans.py`,
  `store/repos/imports.py`, `security/limits.py`.
- Fleet observers M1/M3/M4 (schema v1 spans) and the collector's
  `agentscope/feed.py` (`build_feed`).

## 1. The one-sentence contract

A fleet import is **one versioned canonical-JSON document
(schema `"0.1"`) built from fleet schema-v1 spans, accepted by a
bounded local intake ledger that dedupes by native identity, keeps
provenance and unknown/incomplete outcomes visible, stores no raw
payload, performs no egress, and returns a receiver receipt for
every import.** The same document is the input the real AgentScope
importer accepts; M5 verification runs it through the real
`import_bytes` wherever an AgentScope checkout and pydantic are
available.

## 2. What the real importer accepts (grounded facts)

- **Two adapters only.** `CanonicalAdapter` (one JSON document,
  `schema_version` `"0.1"`, requires `source`, `run`, `spans`) and
  `RagAgentAdapter` (JSONL, one run per line). Detection is
  first-match-wins; no match → quality `unsupported`.
- **OTLP/HTTP JSON is NOT accepted.** There is no OTLP adapter; a
  `resourceSpans` document matches neither detector and returns
  `unsupported`. The fleet transport (OTLP/HTTP JSON) therefore
  **must be translated** to canonical JSON before import. This
  module is that translation, and it says so explicitly instead of
  pretending OTLP is importable.
- **Native IDs are namespaced, not replaced.** The importer stores
  record ids as `prefix + sha256(upload_sha | kind |
  runs/<ordinal>/<native_id>)`, and stores the original `native_id`
  verbatim in the row's `native_id` column. The same native id in
  two uploads yields different stored ids; the same bytes
  re-imported yield the same stored ids (idempotency).
- **Whole-upload idempotency.** A second import of identical bytes
  returns the existing report immediately (`already_imported=True`),
  with no re-parse and no re-write.
- **Per-trace dedup is fatal, not silent.** A duplicate `native_id`
  among spans (or node executions, events, checkpoints) **within one
  trace** is fatal (`validate.duplicate_native_id`): the whole run
  is dropped. A parent cycle is likewise fatal. There is no
  cross-run or cross-import span dedup in the real importer.
- **Dangling references are kept, not dropped.** A
  `parent_native_id` naming a non-existent span is set to null with
  a warning; the record is kept.
- **Out-of-order spans are accepted.** Storage order is
  parent-first; `seq` values are kept when unique and in int64
  range, otherwise the collection is renumbered with a warning.
- **Unknown values are never invented.** Unknown top-level keys are
  ignored with a warning; unknown span fields are moved under
  `attributes`; unknown record keys go to run `metadata["extra"]`;
  unknown outcomes become `unknown` with a warning. Absent metrics
  stay `None`.
- **Missing spans import as `partial`.** A trace with `spans: []`
  validates and imports, but is `partial`. Run times missing are
  derived from span times with a warning.
- **Quality, first match wins:** `unsupported` (no adapter) →
  `invalid` (no run imported) → `partial` (a run dropped, unreadable
  records, or any run partial — no native graph or no spans) →
  `warnings` (imported, not partial, warnings non-empty) →
  `complete`.
- **Limits (provisional in the importer):** 64 MiB upload, 2,000,000
  values, 100,000 runs, 100,000 spans per record, JSON depth 64.
  Breach raises out of `import_bytes` (no report).
- **ImportReport fields:** `import_id`, `quality`, `warnings`
  (`{code, message, source_ref}`), `stats` (exactly 11 keys: runs,
  spans, node_executions, events, checkpoints, payloads, findings,
  warnings, dropped, unmapped, detection_skipped_lines), `run_ids`,
  `already_imported`.
- **Dependencies of the real importer:** pydantic only (plus
  stdlib). No other third-party package.

## 3. The fleet mapping (fleet schema v1 → canonical 0.1)

Input: flattened fleet spans (the receiver's `flatten_payload`
output — `agent.run`, `llm.call`, `tool.exec`, `hook.exec`, …) or a
raw OTLP/HTTP JSON payload (flattened the same way, span events
preserved). One upload = one trace = one run.

| Fleet | Canonical 0.1 | Notes |
|---|---|---|
| traceId (32-hex) | `run.native_id` | Verbatim. The run is identified by the native trace id. |
| spanId (16-hex) | `span.native_id` | Verbatim. |
| parentSpanId | `span.parent_native_id` | Verbatim; dangling → null + `dangling_parent` note (record kept). |
| span name (`agent.run`, …) | `span.name` + `attributes["fleet.span"]` | Name verbatim; kind mapped (below). |
| `agent.run` | kind `run` | Turn root. |
| `llm.call` | kind `llm` | `llm: {model, input_tokens, output_tokens}` from `gen_ai.usage.*`. |
| `tool.exec` | kind `tool` | `tool: {name, call_id}`. Arguments/result NEVER carried. |
| `hook.exec`, `agent.handoff`, `state.write`, anything else | kind `other` | Name preserved in `attributes["fleet.span"]`; no content carried. |
| status code 2 / 1 / 0 | `error` / `ok` / `unknown` | `error: {message}` only when the fleet message exists (already generic — observers sanitize). |
| start/end ns | `started_at` / `ended_at` (aware UTC ISO) | Missing → `None`. End before start → duration unknown + note (mirrors the importer). |
| `duration_ms` | computed `(end-start)/1e6` | None when times are missing or inverted. |
| span `seq` | assigned by start-time order `(start_ns, span_id)` | Deterministic; fleet spans carry no seq. |
| `gen_ai.request.model` + usage | `span.llm` | **Token fix (M3/M4 follow-up):** tokens are carried on `span.llm` with `supports_token_usage=true`, so the import receipt reports real token totals. The collector's `build_feed` only sums spans named exactly `"llm"` and therefore reports 0 for fleet spans — that is a collector-side quirk, NOT the import contract: the import carries the tokens, and the receipt proves it. Fixing `build_feed` itself is out of scope here (different repo); it is recorded as a follow-up. |
| state content | NEVER carried | State spans map to kind `other` with an opaque reference only (`attributes["fleet.state_ref"]` when a ref exists). "Safe state references" means references, never content. |
| span events (OTLP payload input only) | canonical `events` | `native_id = "<trace>:<span>:ev/<index>"`, `type` = event name, `data` = event attributes (or `{}`). The receiver's flattened form drops events, so event mapping only applies to raw-payload input. |
| graph | `{nodes, edges, entry}` | **Transcribed from recorded parent links, and labelled as such.** Nodes: one per span, `name` = span `native_id` (unique), `kind` = mapped kind, `attributes.fleet.span` = original name. Edges: `{"from": parent, "to": child, "kind": "static"}` for every recorded parent link. `entry` = root span `native_id`. This is a faithful transcription of observed structure, not inferred state: every edge corresponds to a recorded `parentSpanId`. The capability flag `supports_graph_topology=true` asserts the source can supply topology — it can, because parentage is recorded on every fleet span. The derivation is documented here, in the import document's run metadata, and in the evidence, so a reviewer can challenge it. No graph is supplied when there are no spans (the run is then `partial`, as in the real importer). |
| run status | `error` if any span errors, else `ok` if all ok, else `unknown` | |
| run times | min span start / max span end | Always derived from span times (fleet spans carry no run-level times); the derivation is a defined mapping rule, recorded in run metadata. |
| run metadata | `{"fleet_schema_version": "1", "producer": <agent>, "trace_id": <traceId>, "graph": "transcribed-from-parentage"}` | Provenance, visible in the document. |

Source block (every document):
```json
{"adapter": "canonical", "framework": "fleet-tracing",
 "framework_version": "1",
 "capabilities": {
   "supports_spans": true, "supports_graph_topology": true,
   "supports_node_execution_mapping": false,
   "supports_state_snapshots": false, "supports_checkpoints": false,
   "supports_interrupts": false, "supports_token_usage": true,
   "supports_cost": false}}
```

## 4. The ledger (bounded local intake)

SQLite (stdlib only), one file per data directory
(`ledger.sqlite3`). Tables:

- `imports` — one row per accepted upload: `import_id` (PK,
  `imp_` + first 16 hex of sha256 of the canonical document),
  `content_sha256` (UNIQUE — the idempotency key),
  `schema_version`, `quality`, `stats_json`, `warnings_json`,
  `tokens_in`, `tokens_out`, `producer`, `received_at`.
- `spans` — `(trace_id, span_id)` PK: `name`, `kind`, `status`,
  `parent_span_id`, `start_ns`, `end_ns`, `attributes_json`
  (the mapped, sanitized attributes), `import_id`
  (first import that stored it). **First finalized write wins:**
  a re-delivery of an identical span is a duplicate (counted,
  not stored twice); a re-delivery with conflicting content keeps
  the first version and records a `span_conflict` warning on the
  new import's receipt. No silent overwrite. The comparison is
  complete — name, kind, status, parent, times **and attributes**
  must all match for a duplicate; an attribute-only change (e.g.
  a different model name) is a conflict. `attributes_json` is
  stored so the record is auditable and conflicts are detectable.
- `events` — `(source, producer, event_id)` PK: `trace_id`,
  `span_id`, `name`, `at_ns`, `import_id`. Event identity is
  explicit: source `"fleet-tracing"`, producer = the span's agent,
  event_id = `"<trace>:<span>:ev/<index>"`.
- `runs` — `trace_id` PK: `status`, `started_at`, `ended_at`,
  `span_count`, `partial`, `import_id`. Updated additively by late
  spans (span count grows, end extends); never duplicated.
- `span_imports` / `event_imports` — join rows, so retention
  purges only rows no live import references.

**Retention / deletion policy (explicit, configurable via
`ImportSettings`):** keep at most `max_imports` (default 100)
imports and at most `max_age_days` (default 30) of history. Every
successful import purges oldest-first past either bound, and a
`purge` CLI command does the same on demand. Purged rows are
deleted, not archived.

**Bounds (fleet side, inside the real importer's limits):**
`max_spans_per_import` 10,000, `max_bytes` 16 MiB. Breach →
the import is refused with a clear error (no partial write).

**No raw-payload storage.** The ledger stores the canonical
document's sha256, metadata, normalized spans/events and the
receipt — never the uploaded bytes or the canonical document.
(Verified by test: a sentinel in an ignored part of the input
appears nowhere in the database file.)

**No egress.** The module performs no network I/O (stdlib
`sqlite3`/`json`/`pathlib` only). The real-import check script is
the only network-adjacent step, and it only reads local files.

## 5. The receipt (receiver receipt)

Every import returns an `ImportReceipt` (dict + stored row):

```json
{"import_id": "imp_<16 hex>",
 "quality": "complete|partial|warnings|invalid|unsupported",
 "warnings": [{"code": "...", "message": "...", "source_ref": "trace:<id>"}],
 "stats": {"runs": 1, "spans": N, "node_executions": 0,
           "events": N, "checkpoints": 0, "payloads": 0,
           "findings": 0, "warnings": N, "dropped": N,
           "unmapped": 0, "detection_skipped_lines": 0},
 "run_ids": ["run_<16 hex>"], "already_imported": false,
 "tokens_in": N, "tokens_out": N,
 "provenance": {"producer": "copilot", "feed": "fleet-tracing",
                "schema_version": "0.1",
                "content_sha256": "<64 hex>", "received_at": "<ISO>"}}
```

Quality mirrors the real importer (§2), first match wins. A
duplicate-content import returns the **original** receipt with
`already_imported=True` and no new rows. `run_ids` carries the
ledger run id (`run_` + first 16 hex of
`sha256(content_sha | "run" | trace_id)` — deterministic per
content, distinct per upload, mirroring the real importer's
namespacing).

## 6. Transport / content type

- Accepted: OTLP/HTTP JSON (`application/json`) — the fleet
  observers' output — and fleet span JSONL (one flattened span
  dict per line) for the CLI file path.
- Refused with a clear error (never silently dropped):
  `application/x-protobuf` (OTLP protobuf) — "OTLP protobuf is
  not accepted; use OTLP/HTTP JSON". This validates the supported
  protocol/content type explicitly: JSON in, protobuf out.
- A canonical JSON document (`schema_version` + `run` + `spans`)
  may also be imported directly (it is validated against §3 and
  stored the same way); a document with an unsupported
  `schema_version` is `unsupported`.

## 7. CLI assertion

`python -m agentscope_importer verify` runs the full contract
check end-to-end on a synthetic fleet turn through a fresh
temporary ledger and exits 0 only if every check passes (15
checks): happy-path `complete` import; duplicate delivery;
content stripping (a turn carrying stripped content imports as
`warnings`, the note is visible, the canary values never reach
the ledger); out-of-order arrival; late span; missing parent;
unknown values; partial coverage; duplicate native id (run
dropped); parent cycle (run dropped); restart recovery;
native-ID preservation; token totals real; receipt on every step;
OTLP content types (JSON accepted, protobuf refused); no
raw-payload storage. The real-import check
(`examples/verify_agentscope_import.py`) additionally runs
the mapped document through the REAL `import_bytes` when an
AgentScope checkout (`AGENTSCOPE_SRC`) and pydantic are
available, and skips cleanly otherwise with the reason printed.

## 8. What M5 does NOT do

- No dashboard or UI. No remote backend. No changes to the
  AgentScope repo or the collector. No live vendor runs. No
  merge to main (staging-only, per Kyle 2026-11-07).
- The ledger is the fleet-side proof of the contract; it is not
  AgentScope's store. The real-import check is the bridge that
  proves the fleet document is accepted by the real importer.
- `build_feed`'s `"llm"`-name-only token summing is a
  collector-side follow-up, documented in §3, not fixed here.
