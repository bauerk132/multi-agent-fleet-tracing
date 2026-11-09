# M8 AgentScope Fleet Investigation Dashboard Specification

**Status:** Phase 2 consolidated specification, ready for review and approval. Authored early under the user's explicit spec-only authorization; M7-B is still pending, so this document does not claim the normal M8 entry gate has passed. No product implementation is authorized by this document.

**Baseline note:** No standalone Phase 1 specification was found. This document reconstructs the Phase 1 baseline from the refined plan's §7 manifest, the bridge proposal, the M7 acceptance contract, the ten supplied screen references, and the current evidence. It does not claim a missing Phase 1 draft was written, reviewed, or accepted. This is a Phase 2 consolidation, not an assertion of Phase 1 acceptance.

## 1. Goal and boundaries

Extend the existing AgentScope React application with a focused view for investigating runs imported from the fleet tracing path. The UI reads AgentScope's HTTP API only. It reuses AgentScope's run storage, import flow, Graph Lens, Timeline, and Ask the Run behavior; it does not create a second observability product or second data store.

The dashboard helps an operator:

1. Find and select an imported run, with producer and provenance context.
2. Inspect its recorded span parentage as a trace tree when no framework topology was supplied.
3. Move between tree, timeline, span details, provenance, and evidence-linked Ask results while preserving one selected run/span.
4. Understand what was observed, correlated, missing, partial, unsupported, or invalid without inventing data.
5. Move from a run to workflow grouping or handoff references when the API exposes those links, with explicit behavior for unresolved and external references.

The dashboard must not read fleet collector files, the fleet ledger, or raw producer session files. It must not claim that a source is verified merely because a producer label exists. Source confidence, fleet receipt quality, AgentScope import quality, and verification status are separate concepts.

## 2. Status and evidence model

M7-B is the dashboard-unblocking operational proof and remains pending. Existing M7-A evidence demonstrates a bounded fixture import and readback only; it does not demonstrate a live producer-to-investigation path. This spec is useful for review now, but M9 implementation remains gated on M7 acceptance and separate approval of this spec.

Use the following status vocabulary in product copy and review records:

| Status | Meaning | UI treatment |
|---|---|---|
| Implemented | Present in an exact code revision | Do not show as a run badge unless API evidence supports it |
| Tested | A test was actually executed | Keep as review/evidence metadata, not source confidence |
| Independently verified | A reviewer reproduced evidence for the exact candidate | Show only when an explicit API-backed verification record exists |
| Accepted | Required review gates and owner release are complete | Never infer from import success |

Do not use a single green “Verified” label for producer, transport, readback, and code acceptance. Each is an independent assertion and needs its own evidence. The current bounded project observer roster has evidence for Hermes and Copilot; this roster evidence does not prove which producer generated a particular persisted run and does not establish M7-B. Other producers remain unverified/unsupported/unobserved only when the project roster source says so. If the runtime API does not expose producer verification or these roster states, render “Source verification unavailable” and identify that API dependency; do not derive it from an import status, fixture, mock, or producer string.

## 3. Product flow and shared selection

### 3.1 Runs to investigation

1. The app shell retains the existing 200px left navigation rail with Runs, Graph Lens, Timeline, Ask the Run, and Imports destinations. The selected run and active investigation pane are visible in a compact header. A connection indicator reflects the latest actual AgentScope API request state; never hardcode the mock's “connected” or “verified” state. Reuse an existing health/status mechanism if one exists; otherwise report connection state from actual API request success/failure without adding a health route.
2. The Runs view loads the supported run list from AgentScope.
3. Search and producer filters apply only within the fetched dataset or using query parameters confirmed by the live API contract. Do not invent pagination or filter parameters.
4. Selecting a row navigates to the persisted AgentScope target run ID, not a fleet receipt run ID.
5. The run investigation view keeps selection using the existing fields in `web/src/state/selection.ts`: `runId`, `nodeId`, and `evidenceRef`. A span selection is represented by its evidence reference (for example, `ev:span:spn_<id>`) and mapped to a graph node only when the API provides that mapping; do not add a presumed `spanId` selection slot.
6. Graph Lens, Timeline, span details, Provenance, and Ask the Run share that selection. A span selected in one view is highlighted in the others when the relevant API data is available.
7. Show workflow grouping and handoff/external references only if AgentScope returns them. Selecting a same-run reference selects its evidence span; a target-run reference navigates only when the target ID resolves through the API, preserving a return path. Unresolved, external, or correlated references remain explicitly labeled with available evidence; do not guess targets or draw causal edges. If these fields are absent, show “Workflow/handoff details unavailable” and record an API dependency rather than inventing workflow metadata or a new store.

Guard asynchronous responses by the selected run ID. If the operator switches runs while requests are in flight, stale responses must not overwrite the newly selected run.

### 3.2 Parentage and graph semantics

The M7 tree derivation represents recorded parent-span relationships. It is a trace tree, not a native LangGraph/framework topology. Show a “Tree” mode badge and an explanatory sentence such as “Tree from recorded parent links; no framework topology was supplied.” Do not render native topology edges, label span parentage as causality, or silently promote the tree to a native graph.

Fleet runs under the accepted tree correction are expected to have no source graph topology and may carry AgentScope quality `partial` under AgentScope's quality ladder. This can coexist with fleet receipt quality `complete`; the two quality labels describe different systems and must be displayed separately.

The tree view supports expand/collapse, selection, search within loaded spans, and a plain hierarchy-list fallback if the visual layout cannot be rendered. Preserve native span IDs, recorded parent links, root spans, and any missing-parent indication. A missing parent is an explicit unknown/boundary; do not fabricate a root-to-child edge. Tree aggregation must distinguish a span's own token usage from descendant totals to avoid double counting. For the corrected fleet derivation, show the exact honesty line: **“No native graph — shown as a tree. Partial is honest, not broken.”**

### 3.3 Timeline and span detail

Timeline shows only spans returned by the API, using real timestamps/durations and their actual units. Missing start/end/duration values remain “Unknown”; they do not receive inferred timestamps or invented ordering beyond the API's sequence. Show the real time ruler only for spans with valid timing. Unknown-duration spans use a hatched, explicitly “Not to scale” marker/lane with no implied duration; unknown timestamps remain in an “Unknown time” lane. Show errors with a red diamond plus text/icon semantics, not color alone. Do not turn an unknown duration into a zero-width bar.

Selecting a timeline item selects the same span in the hierarchy and detail panel. Errors and durations are shown where present. Correlation IDs or external references remain correlated/external; do not label them causal handoffs unless the persisted record supports that claim.

## 4. Data and confidence semantics

### 4.1 Independent fields

The UI keeps these fields distinct:

- **Producer:** source name returned by the API, e.g. `copilot`. A name is not verification.
- **Source confidence:** verified, unverified, unsupported, unobserved, or unavailable only when backed by an explicit roster/status contract. Do not infer roster status from the run itself. If no such endpoint/field exists, show “Source verification unavailable.”
- **Fleet receipt:** fleet import ID, receipt quality, warnings, span/run counts, token totals, and graph derivation only when exposed through an AgentScope API contract or an explicit safe import record. The browser must never read the fleet ledger to obtain it.
- **AgentScope import:** AgentScope `import_id`, HTTP result, quality, warnings, and reported statistics from its API.
- **AgentScope target run:** AgentScope run ID returned or predicted for the exact frozen upload bytes, then confirmed through semantic readback. Do not require this ID to equal the fleet receipt's run ID.
- **Graph derivation:** e.g. `tree-from-recorded-parentage`, as returned by the persisted run/provenance contract.
- **Evidence level:** a producer execution, delivery/readback, independent review, or acceptance assertion only when the corresponding explicit evidence exists.

### 4.2 Receipt trap and current fixture evidence

The supplied fresh fixture record contains two different import identities. AgentScope's M7-A fixture import is `imp_7538401e7c6fad46` → target run `run_c73771bb67feeeee`, with AgentScope quality `partial`, three persisted spans, 120 input tokens, and 340 output tokens. The fleet receipt for the same sample has fleet import ID `imp_dcefa8ae9c61e336`, fleet run ID `example-trace-0001`, fleet receipt quality `complete`, three spans, and matching token totals. These IDs and qualities must not be conflated.

This is fixture evidence, not proof that a live Copilot execution produced this persisted run. It does not establish M7-B acceptance, G1/G2 acceptance of the dashboard, or production readiness. The UI must use actual API-returned IDs for persisted records and must not copy example IDs from the screen designs into application data.

### 4.3 Unknown is not zero

An absent numeric value is `Unknown` (or an em dash plus accessible text). Do not turn missing token counts, duration, costs, findings, state snapshots, or source metadata into zero. Show explicit zero only when the API returned a measured zero. State snapshots are not supported by the fleet source; display the exact message **“No state snapshot available for this producer”**, not an empty fabricated state panel.

## 5. API contract and loading behavior

The UI uses the existing AgentScope API, under the `/api/v1` prefix. The bridge proposal's corrected routes take precedence over stale unprefixed route examples in earlier planning notes.

| Purpose | Method and route |
|---|---|
| List runs | `GET /api/v1/runs` |
| Read run details | `GET /api/v1/runs/{run_id}` |
| Read run spans | `GET /api/v1/runs/{run_id}/spans` |
| Read graph/tree projection | `GET /api/v1/runs/{run_id}/graph` |
| Read findings | `GET /api/v1/runs/{run_id}/findings` |
| Read import record | `GET /api/v1/imports/{import_id}` |
| Import a file through existing flow | `POST /api/v1/imports` as multipart `file` |
| Ask about a run | `POST /api/v1/runs/{run_id}/ask` |

Do not add guessed query parameters, response fields, pagination, or endpoints. Before implementation, map these paths to the exact existing typed response models in `web/src/api/routes.ts` and the server contract at the selected AgentScope revision. If a route/field is absent, create a named dependency or propose a manifest extension for review; do not silently add backend scope.

The dashboard only renders server/API data. It does not open local paths, read server-side files from a path typed into the UI, parse arbitrary trace formats, or implement a new importer. The existing multipart upload remains the import entry point.

### 5.1 Request states

- **Loading:** skeletons or an inline loading label, `aria-busy="true"` on the loading region, and stable layout. Do not display fabricated byte counts, progress percentages, checksum stages, or validation phases unless the API emits them.
- **Empty collection:** API returned a successful empty run list. Offer the existing import action and concise instructions.
- **No filter matches:** data exists but the active filter/search yields no rows. Offer clear-filter/search controls; do not label it “No runs yet.”
- **Run unavailable:** a `GET /api/v1/runs/{run_id}` detail request returns 404 or an explicit unavailable response. Absence from a list alone is insufficient because the list may be filtered or incomplete. Explain that the persisted run could not be loaded and provide a safe return path.
- **API/auth/network failure:** show the exact safe failure category and retry action; never imply the import failed if only a later read failed. Do not expose bearer tokens or sensitive response bodies.
- **Partial:** state which source/API record is partial and list only returned warnings/findings. “Partial” can mean AgentScope's tree-mode quality or actual data loss; explain which meaning applies. Actual dropped/unreadable counts must come from a receipt/report.
- **Invalid/unsupported:** show the real ImportReport status, warnings, and a “View report” action when available. Do not claim quarantine, atomic no-write behavior, checksum validation, root-span validation, or zero persisted records unless the API response explicitly proves it.
- **Retry:** retry the failed API operation only; do not imply that a repeated import is safe unless the server's idempotency result confirms it.

## 6. Ask the Run evidence presentation

Use the existing Ask the Run capability and endpoint. Submit the operator's exact question; do not imply zero-hallucination guarantees or local-only/zero-cloud execution unless verified in the current product contract.

Each returned claim retains its server-provided classification: **Observed**, **Supported inference**, **Hypothesis**, or **Unknown**. Citations reference persisted run/span evidence and navigate to the cited span in the shared selection model. If no citation is returned, do not create one. If the answer says an agent's internal state is unknown because snapshots are unsupported, present that unknown plainly.

Do not convert event order into causality. Distinguish recorded parentage, observed sequence, correlation, supported inference, and hypothesis in copy and visual connectors.

## 7. Visual and interaction reference

Use the supplied AgentScope design system as visual guidance: desktop workbench, 200px navigation rail, dense master/detail layout, ink-dark canvas `#0E1116`, base surface `#161B22`, raised surface `#21262D`, 1px dividers, 6px default radii, Inter UI type, IBM Plex Mono for IDs/metrics, blue `#388BFD` focus/selection, verified green `#3FB950`, warning amber `#D29922`, and error red `#F85149`. Preserve clear keyboard focus and semantic labels; color alone cannot carry state. Keep transitions brief and respect reduced-motion preferences.

The ZIP's design-system references to `@components/*.html` point to a `components/` sticker folder that is absent from the supplied archive. Those files are not available implementation assets. Treat the ten full-screen HTML files as visual references, not reusable production components, not API evidence, and not React code to execute.

### 7.1 Ten screen references: required layout, controls, states, and reuse

| Reference and layout | Controls and actions | States and data rules | Existing app mapping |
|---|---|---|---|
| **`agentscope_runs_dashboard`** — existing app shell with 200px left rail; topbar with actual connection state and run count only when returned; main Runs table; compact filters above it. Columns: target run ID, producer, source confidence (if API-backed), receipt/import provenance, mode/quality, spans, tokens, duration, import time. | Search and producer dropdown; clear filters; keyboard-selectable row and Open action. Workflow grouping/handoff navigation appears only when API-backed references exist. | Separate successful empty list from no filter matches and API error. Counts and columns are returned data only. Source roster labels distinguish the project's Hermes/Copilot observer roster from runtime producer verification. No demo rows or fictional IDs. | `RunsTable.tsx`, `FleetSourceBadge.tsx`, `api/routes.ts`, existing imports and selection hooks. |
| **`agentscope_execution_debugger`** — investigation shell with left run context, center Graph Lens/Timeline tabs, right span inspector, compact mode/quality strip. | Switch Tree/Timeline; select a run or evidence item; open provenance/Ask; follow only resolvable workflow/handoff links; preserve a return path. | Loading/failure applies independently per pane; stale responses cannot replace the current run. Omit replay/step/next, PID, memory, raw state, raw console, IPC/pipe, and controls without accepted APIs. | Reuse existing Graph Lens, Timeline, Ask, inspector, and `state/selection.ts` (`runId`, `nodeId`, `evidenceRef`). |
| **`agentscope_trace_tree_detail`** — navigable parent-link tree with hierarchy labels, ID, kind/status, own-token values; right inspector shows selected span and evidence. Include plain hierarchy-list fallback. | Expand/collapse all and per node; search loaded spans; select by keyboard/pointer; copy full native ID. | Preserve recorded roots/parents and expose missing-parent boundaries; no fabricated edges. Avoid double counting by showing own usage separately from descendant totals. For corrected fleet derivation show **“No native graph — shown as a tree. Partial is honest, not broken.”** | `GraphLens.tsx` with `isTreeDerivation`; existing graph data and inspector; selection mapped via `evidenceRef` and `nodeId` only when API mapping exists. |
| **`agentscope_timeline_view`** — time ruler for timed spans, aligned rows, span labels, status/error markers, and selected-span detail. | Fit/zoom only if current Timeline supports it; select a row to synchronize the tree and inspector; focus selected evidence. | Real times and spans only. Errors use red diamond plus text. Unknown duration uses a hatched “Not to scale” marker; unknown timestamps use an Unknown time lane. No invented metric span, duration, overlap, or aggregate latency. | Reuse existing Timeline and shared evidence selection. Richer trace detail is a Sprint 2(c) dependency. |
| **`agentscope_run_provenance`** — provenance summary; separate AgentScope ImportReport and fleet receipt sections; rows for source/producer, import IDs, target run ID, schema versions, derivation, quality, warnings, and receipt/readback status. | Open import report; copy IDs; follow readback to persisted run only where exact target ID resolves. | Render only fields actually supplied by API. Keep fleet `complete` and AgentScope `partial` distinct. Never show mock WAL/CRC/cryptographic proof, “receipt matched,” fake schema versions, or readback pass absent independent evidence. | `FleetSourceBadge.tsx`, existing import/run detail views, typed routes. Missing fleet receipt/readback exposure is an API dependency. |
| **`agentscope_ask_the_run`** — existing query panel tied to selected persisted run; answer area separates claims and citations. | Submit exact question; use suggestions only if supported by existing UI; click citation to select/navigate to the evidence span. | Preserve Observed / Supported inference / Hypothesis / Unknown from API. Do not claim zero hallucinations, verify chronology as causality, or invent a citation. Missing state answers use the exact no-snapshot message. | Existing Ask the Run and `/api/v1/runs/{id}/ask`; shared `evidenceRef` selection. |
| **`agentscope_empty_state`** — app shell and Runs landing panel with concise explanation and existing Import Run action. | Open existing import flow; retry list load; clear filters when empty due to filtering. | Successful zero persisted runs says “No runs yet”; filtered zero says “No runs match these filters.” API error has its own state. Do not claim OTel dump, LangGraph checkpoint SQLite, live pipe, or other format support based on mock text. | Existing Runs route/importer; no new import workflow. |
| **`agentscope_loading_state`** — stable shell with skeleton rows/cards in the pane being fetched; current run identity remains visible if known. | No fake progress actions; allow existing cancel/retry only if the API/UI already supports it. | `aria-busy` on pending region; no invented buffer size, byte progress, checksum/integrity stages, or elapsed phase indicator. Other panes remain usable where safe. | Existing API request/query loading state; `RunsTable.tsx` and current detail views. |
| **`agentscope_partial_import_state`** — run summary with separate quality chips; report warning block; persisted readable spans remain navigable. | Open report, select readable span, return to runs; expose retry only for a retryable operation. | Explain whether Partial is AgentScope's tree-derived quality or actual recorded data loss. Show exact returned warning/drop/unreadable counts. Do not fabricate corrupted chunks, source drops, token totals, or absent warnings. | `FleetSourceBadge.tsx`, Runs table, provenance panel, Graph Lens. Any unavailable fleet receipt is a dependency. |
| **`agentscope_validation_failure_state`** — failure summary using actual report status and safe reason, with no pretend run row. | “View report” when an import record exists; “Retry” only when safe/available; “Back to Runs” always. | Distinguish invalid from unsupported and request/auth/network failures. Report persisted state only as the API describes it; no unconditional quarantine, zero-write, checksum failure, root-field validation, or no-record claim. | Existing import flow and typed ImportReport; routes in `api/routes.ts`. |

The design mockups are illustrative. Fixture IDs such as `imp_4011`, invented producers, run records, statuses, and counters are placeholders and must not ship as seeded evidence.

## 8. Accessibility and operational details

- Runs table uses semantic table headers, accessible row selection, and keyboard navigation. Filter labels are associated with controls; selected producer and clear-filter actions are announced.
- Tree uses an accessible tree pattern with expand/collapse state, level/parent context, selected node state, and keyboard navigation. The plain list fallback remains fully operable.
- Tabs and panels expose selected/controlled relationships and preserve focus when switching views. Escape closes transient panels only when consistent with the existing app pattern.
- Loading and update messages use restrained live regions; errors are announced without dumping raw server payloads. Badges include text labels as well as color.
- Provide a visible focus indicator, sufficient contrast, reduced-motion support, and non-color distinctions for warning/error/quality.
- Keep exact IDs available to copy and screen readers; truncation is visual only.

## 9. File manifest and change boundary

Only these frontend files are in the current M8/M9 target manifest. Existing files and exports must be inspected at the approved AgentScope revision before implementation. This list does not authorize implementation.

### Create `web/src/features/runs/FleetSourceBadge.tsx`

```ts
export interface FleetSourceBadgeProps {
  producer: string;
  importId?: string;
  quality?: 'complete' | 'partial' | 'warnings' | 'invalid' | 'unsupported';
  className?: string;
}
```

Render producer and available AgentScope import metadata as compact text. `quality` represents only the quality system explicitly named by the caller; callers must label fleet receipt quality separately from AgentScope import quality. Do not infer source verification from quality. Missing values are omitted or shown as unavailable, never populated from a mock.

### Modify `web/src/features/runs/RunsTable.tsx`

Add a source/producer filter and provenance badge column. Props:

```ts
activeProducerFilter?: string;
onProducerFilterChange?: (producer: string | null) => void;
```

Preserve existing props and table behavior. The filter options come from the loaded dataset unless a verified API contract supplies options. Include an “All producers” option that calls `onProducerFilterChange(null)`. Do not add new search/filter query parameters without contract confirmation.

### Modify `web/src/features/graph/GraphLens.tsx`

Add `isTreeDerivation?: boolean` to select parent-link tree rendering when the API graph mode/capabilities indicate it. Include a truthful Partial/tree explanation for the corrected fleet derivation. Preserve existing native Graph Lens behavior for genuine framework topology. Tree mode must not label parentage as a framework graph or causality. Use an existing layout fallback or plain hierarchy list when the layout cannot be built; do not add a new graph engine.

### Modify `web/src/api/routes.ts`

Keep typed mappings for `/api/v1/runs`, `/api/v1/runs/{id}/graph`, and `/api/v1/runs/{id}/spans`, and add/update only mappings needed for the existing import detail, findings, and Ask endpoints after verifying exact types from the selected API revision. Preserve `/api/v1` prefix. Do not invent response fields, request shapes, or unsupported endpoints.

### State

Use existing hook patterns in `web/src/state/selection.ts` for selected run/span and cross-view synchronization. Do not introduce Redux, Zustand, or another global state library. If the existing hooks cannot express a required state, record a manifest extension for approval rather than silently adding files or infrastructure.

### Out-of-manifest dependencies

Any requirement needing backend changes, new endpoints/response fields, roster verification, fleet receipt exposure, delivery/readback attestation, an import redesign, a new state library, or other files is a named dependency/proposed extension requiring explicit review. The UI cannot independently verify producer origin, transport receipt, cryptographic integrity, or source confidence from a producer string.

## 10. Sprint 2 dependencies and non-goals

Record the following dependencies explicitly when they affect an acceptance claim:

- **Sprint 2(a), missing-state fixture/test:** needed to validate honest missing-state UX through the product's test fixture. Until delivered, this spec defines the message contract, but fixture coverage remains pending.
- **Sprint 2(c), AgentScope roadmap M4 Timeline and trace details:** needed for richer Timeline/trace navigation. This is AgentScope's roadmap M4 and is unrelated to fleet M4 (Copilot observer).
- **Sprint 2(d), AgentScope roadmap M3 Graph Lens depth, filters, and layout polish:** reuse when delivered; do not build a duplicate Graph Lens or silently depend on undelivered controls.
- **API provenance/readback exposure:** if the API does not expose fleet receipt, source verification, or independent readback status, show unavailable and file a named dependency; never query the fleet ledger from the browser.
- **Sprint 2(b) security closeout:** remains a separate prerequisite/review item. Do not claim its auth/loopback/security work is complete based on this spec.

Non-goals: generic observability platform; standalone dashboard; second run store/importer/query layer; collector, broker, background worker, or fleet-ledger reads; raw prompts/tool outputs; secret scanning service; agent replay/step controls; invented state snapshots; source roster expansion without evidence; implementation of Sprint 2; deployment, merge, configuration changes, telemetry, monitoring, or real export.

## 11. Review and acceptance checklist for this specification

Reviewers should confirm:

1. This is labeled a Phase 2 consolidation draft reconstructed from refined Phase 1 inputs, not a recovered or accepted Phase 1 artifact.
2. M7-B is clearly pending; no fixture result is represented as live-producer evidence.
3. All ten screen references have a bounded, evidence-aligned treatment (Section 7.1).
4. File manifest and signatures match the refined plan and remain the boundary (Section 9).
5. `/api/v1` routes are used and no unsupported route, response field, filter parameter, or import method was invented.
6. Fleet receipt IDs/quality are not conflated with AgentScope import IDs/quality or target run IDs.
7. Unknown is never rendered as zero; tree parentage is never called native topology or causality.
8. The visual design system is treated as guidance; missing sticker-sheet assets and mock-only claims are not treated as production contracts.
9. Sprint 2 dependencies and any API exposure needs are named without silently expanding scope.
10. G1/G2 and Kyle's spec approval occur before M9 implementation. M9 approval, if granted later, does not authorize merge or deployment.

**No tests were run for this document.** It is a product specification draft for review. Implementation, test results, independent verification, and acceptance remain separate future statuses.

## 12. Source and traceability inventory

See [M8 verification note](M8-verification.md) for source paths, hashes, the fixture evidence distinction, and the completed 10/10 coverage and manifest-boundary checks.
