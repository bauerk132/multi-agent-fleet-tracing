# Milestone 5 — Build Notes

**Date:** 2026-11-07. **Builder coordinator:** Muse (claude-muse).
**Scope:** §10 ledger + §12 gate 4 — one metadata-only, versioned
end-to-end import with native IDs preserved, safe state references,
deduplication, late/out-of-order events, unknown values, partial
coverage, and receiver receipt. CLI assertion first; no dashboard,
no UI, no remote backend, no changes to the AgentScope repo or the
collector, no live vendor runs, no merge to main.

## Grounding (read before designing)

- Notion plan page "Multi-Agent Fleet Tracing — OpenTelemetry
  Integration Plan" (`4102b8e7-2a08-8171-89a2-dcdc80db4f7b`), §10/§12.
- `reviews/ENGINEERING_STANDARDS.md`.
- Investigator report on the actual AgentScope importer
  (bauerk132/agentscope @ `ags/sprint1-staging` `95f8a68`):
  only two adapters (canonical `0.1`, rag-agent JSONL); no OTLP
  adapter; native IDs namespaced, not replaced; whole-upload
  idempotency; per-trace duplicate native id and parent cycle are
  fatal; dangling parents kept as null + warning; out-of-order
  accepted; unknown values never invented; missing spans →
  `partial`; quality ladder `unsupported` → `invalid` → `partial`
  → `warnings` → `complete`; limits 64 MiB / 100k runs / 100k
  spans per record; `ImportReport` fields (`import_id`, `quality`,
  `warnings`, 11-key `stats`, `run_ids`, `already_imported`).
- The fleet repo's `examples/map_copilot_sample_to_agentscope_feed.py`
  (1 trace, 3 spans) and the M3/M4 observers' sanitizers.

## Design decisions (each reasoned through)

1. **One upload = one canonical document per trace.** The real
   importer is multi-run; a fleet batch may carry several traces.
   Grouping by trace keeps the mapping 1:1 with the real importer's
   run model and makes per-trace fatal failures (duplicate native
   id, parent cycle) drop only that run, not the whole upload.
2. **Content sha covers all documents in the upload** (sorted by
   trace id, deterministic JSON). The idempotency key is the whole
   upload, matching the real importer's whole-upload idempotency.
3. **Graph transcribed from recorded parent links.** Nodes named by
   span `native_id` (unique), edges `{"from": parent, "to": child,
   "kind": "static"}`, `entry` = root. Labelled
   `transcribed-from-parentage` in run metadata, CONTRACT.md, and
   the evidence, so a reviewer can challenge it. Every edge is a
   recorded `parentSpanId`; nothing is inferred. No graph when no
   spans (run then `partial`, as in the real importer).
4. **Ledger stores mapped attributes** (`attributes_json`). The
   first design stored only name/kind/status/parent/times, which
   made attribute-only changes invisible to conflict detection —
   caught by a failing test (a changed model name was reported as a
   duplicate, not a conflict). Storing the sanitized attributes
   makes the record auditable and the comparison complete.
5. **Sanitization is a correction, not a silent default.** The
   real importer's ladder says `warnings` when "anything was
   dropped, guessed or corrected" (verified in pipeline.py
   `_quality`). A turn that needed stripping therefore imports as
   `warnings`; a pre-sanitized turn (the normal observer output)
   imports as `complete`. The dropped values never reach the ledger.
6. **ISO↔ns conversion uses integer arithmetic.** The first
   implementation used `datetime.timestamp()` (float), which loses
   nanosecond precision; combined with a dict-shape bug in the
   duplicate comparison (the fresh row carried `trace_id`/`span_id`
   keys the stored record lacks), every re-delivery was reported
   as a conflict instead of a duplicate. Both were caught by
   failing tests and fixed (integer delta arithmetic; compare only
   the stored fields).
7. **OTLP string times coerced.** OTLP/HTTP JSON encodes
   `startTimeUnixNano` as a string; the mapper's first `_is_int`
   check silently dropped those times (seq fell back to 0,
   `started_at` None). Caught while writing the OTLP test path;
   fixed with `_to_ns` (int or digit string).
8. **Identical content always returns the original receipt.**
   The real importer lets a later good import upgrade a refused
   one; for the fleet ledger the outcome is deterministic per
   bytes, so an upgrade can never change the outcome for identical
   bytes — the path is deliberately not implemented (documented in
   `importer.py`).
9. **Direct-canonical documents are normalized, not trusted.**
   Unknown top-level keys are warned and ignored; unknown span
   fields move under `attributes`; unknown run fields move to
   `metadata["extra"` — mirroring the real importer's normalize
   rules. The document is deep-copied first so the caller's dict
   is never mutated (caught a dict-mutation-during-iteration bug
   in the first draft of the unknown-run-field handling).
10. **The real-import check uses the API's entry point.**
    `make_importer(data_dir)` + `apply_migrations` (as
    `api/app.py` does at startup) — not `import_bytes` directly,
    which needs a connected, migrated DB. The checkout assembly
    had missed `store/migrations/0001_init.sql`; it was fetched
    from GitHub at `95f8a68` (8,465 bytes, sha `e0d43049…`) and the
    checkout is now complete (the only non-.py runtime resource).
11. **pydantic lives in a separate venv** (`~/workspace/agentscope-venv/`,
    2.14.3) outside the fleet repo, so the snapshot push never
    sweeps it in and the fleet repo venv stays OTel-only. The
    AgentScope backend has no requirements file to pin against;
    latest pydantic v2 was installed.
12. **Retention purges are explicit and total.** `max_imports`
    (100) / `max_age_days` (30), oldest-first, on every import and
    via `purge`. Partial runs (no spans) are purged when their
    import goes; non-partial runs go when their last span goes.

## Test-failure log (what the first run caught)

The first M5 test run failed 5 of 42. Each failure was a real
defect or a real design question, fixed deliberately (not by
weakening the test):

- Happy-path quality `warnings` instead of `complete` — the
  synthetic turn carried canary content attributes, which the
  mapper correctly strips and counts. Resolution: the design
  decision in (5) above — pre-sanitized turn for the happy path,
  a dedicated `content_stripped` check for the canary path.
- `duplicate_span` never reported — dict-shape bug in the
  comparison (6). Fixed.
- `span_conflict` missed on an attribute-only change — the
  ledger didn't store attributes (4). Fixed by storing
  `attributes_json`.
- CLI `python -m agentscope_importer` failed — no `__main__.py`.
  Added.
- Real-import check failed `StoreError: no such table: imports`
  — `make_importer` does not migrate; the API does it at startup
  (10), and the checkout lacked the migration file. Both fixed;
  the check now PASSES with quality `complete`.

## Double-check pass (Kyle's direction)

See "Double-check" in the final report: the build was re-verified
from scratch after completion — contract re-read against §10/§12,
every edge case re-verified, the full suite re-run fresh, and the
staging push re-verified independently from GitHub (head listed,
pushed files read back). Findings (if any) are recorded there.
