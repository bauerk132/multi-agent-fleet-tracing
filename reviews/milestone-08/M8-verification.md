# M8 dashboard specification — verification note

**Artifact status:** The specification is a Phase 2 consolidation draft written under explicit spec-only authorization. M7-B remains pending. This note verifies document coverage and source traceability; it does not report product tests or G1/G2 review.

## Completed checks

- **10/10 screen references covered:** Runs dashboard, execution debugger, trace tree detail, Timeline, run provenance, Ask the Run, empty state, loading state, partial import, and validation failure. Each has an explicit treatment in §7.1 of the specification.
- **Manifest boundary preserved:** two creates (`FleetSourceBadge.tsx`, `RunsTable.tsx`), five modifies (`Runs.tsx`, `RunView.tsx`, `GraphLens.tsx`, `api/routes.ts`, `api/client.ts`), and existing `state/selection.ts` reuse. Table rendering is extracted from `Runs.tsx` to `RunsTable.tsx`; selection uses the actual `runId`, `nodeId`, and `evidenceRef` fields; span links use `evidenceRef`, not a presumed `spanId` slot. Interfaces, alternatives, and out-of-manifest dependency behavior are specified in §9 and §10. No implementation files were changed.
- **Baseline provenance:** no standalone Phase 1 specification was available in the supplied source set. The specification explicitly says it reconstructs that baseline from refined Phase 1 inputs; it does not claim a Phase 1 artifact or acceptance.
- **Evidence boundary:** fresh M7-A fixture evidence is labeled fixture-only. Fleet receipt (`imp_dcefa8ae9c61e336`, `example-trace-0001`, `complete`) remains distinct from AgentScope import (`imp_7538401e7c6fad46`, `run_c73771bb67feeeee`, `partial`). Matching three spans and 120/340 tokens does not establish M7-B or live Copilot provenance.
- **Route check:** `/api/v1` prefix retained for run, span, graph, import, findings, and Ask paths; no invented query parameters or response fields. Dashboard reads AgentScope API only.
- **Mock corrections:** demo roster/IDs, mock cryptographic/WAL/CRC labels, fake schema version, synthetic Timeline span, unsupported source-list claims, fabricated loading stages, and unconditional validation/quarantine claims are explicitly excluded. The bounded project observer roster's Hermes/Copilot evidence is distinguished from runtime producer verification and M7-B.
- **No tests were run.** No code, commit, merge, push, deployment, real export, or application settings were changed.

## Input inventory and SHA-256

Hashes are for the exact source files read for this draft. The ZIP hash was supplied and verified before extraction.

| Input | SHA-256 |
|---|---|
| `work/stitch-downloaded.zip` | `e778852b30829a7e4ea3dedf566f8df70303cb809104c8617f08ecf551e4218c` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_design_system.md` | `4674bc020058107f04f49fd65f9178ac3c80f3e3c1f476a56d50ffe14a4a1ab9` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_runs_dashboard/code.html` | `bd523655eb87b3d071566721c2571e6fb33a270d81a49f82f4879db332bbffef` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_execution_debugger/code.html` | `ddf8778add68677a2caec8566f1792909aa688ef0e76562243c3e6e526c56581` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_trace_tree_detail/code.html` | `2b165dca9b7240bf9a2b83c56eaeef093a537bb87081c8a8c4cb158bc395c75c` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_timeline_view/code.html` | `4ea9e487a46c90773d0bec9567f9af2bff2d806c828410ff656e782cda6d91d8` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_run_provenance/code.html` | `dcfda06ba5050fbf779edf3deb3fb8c0521abcbb43b9647ea623dc4cb53f83fe` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_ask_the_run/code.html` | `b42095e5bce5ed19f1bd9a7337f12dbda6e603b2c8fd4878711116bad84243dc` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_empty_state/code.html` | `aeacbc5169df049df8b3730fa9736a1a445af7470228ce663ef06639253f89fd` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_loading_state/code.html` | `d3326841a7218f2f23ce66326e4ba940749b524121ca172ff2d2a1d818f878a2` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_partial_import_state/code.html` | `c4f26867a53468c0eee8ea2f190c0956b77456a4dc42f9eefa52693285cfc89c` |
| `work/stitch/stitch_agentscope_local_agent_debugger/agentscope_validation_failure_state/code.html` | `e3dd853497dd8f78aeeb22d6d1b0eb37fd90833011a36be9bed3a072d039e193` |
| `C:\Users\kbaue\AppData\Local\hermes\cache\scratch\spec-docs\reviews\dashboard-plan-draft.md` | `39fe810c67468e80f008f3961577baf09b1dff55700f40806dc30482ba0ea115` |
| `C:\Users\kbaue\AppData\Local\hermes\cache\scratch\spec-docs\reviews\milestone-07\bridge-implementation-proposal.md` | `12dbc4a0e448ec56853080ae32f39098de481fb8e960c49667232f331e83e2d7` |
| `C:\Users\kbaue\AppData\Local\hermes\cache\scratch\spec-docs\reviews\milestone-06\m7-acceptance-contract.md` | `777733984ea5b612905307ba904c9c28f52a299addb45dda465e8cd6ae10ace3` |
| `C:\Users\kbaue\OneDrive\Documents\Desktop\Tracing\goldfish_review_report.md` | `9be95676381810f4049f1574327da9b1fb4321ea37443c19c943f9b563699928` |
| `C:\Users\kbaue\AppData\Local\hermes\cache\scratch\agentscope-live-proof\canonical.json` | `dd9a1b74ce7a9243d34be931a3fa8aa2810532fe352a9e348e4159664742c927` |
| `C:\Users\kbaue\AppData\Local\hermes\cache\scratch\agentscope-live-proof\m7a-fresh-201-evidence.json` | `74a0afc7f2dd587fa74857402587fc207ec2aa7d64b06255c776c404f25ae9f0` |

## Current evidence limitation

The inspected `m7a-fresh-201-evidence.json` records a successful fixture import and readback. It includes AgentScope `partial` quality and `run_c73771bb67feeeee`; the separate fleet receipt is `complete` and uses `example-trace-0001` plus a different fleet import ID. This proves neither an actual Copilot emission nor the dashboard's G1/G2 review. M7-B and downstream dashboard acceptance remain open.
