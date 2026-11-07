# Milestone 4 — Builder evidence (Copilot observer)

**Milestone:** 4 — Copilot observer, separately opt-in (plan §5, §10).
**Scope this round:** COPILOT ONLY. Antigravity comes second,
separately. **Date:** 2026-11-07. **Builder:** Muse.
**Status:** Implemented + Tested.
**Acceptance:** NOT DECLARED — requires the independent review step
(Antigravity) plus the installed-build contract test on the dev machine.

## What was built

- `observability/copilot_plugin/observer.py` — the Copilot observer
  core: native-span → schema-v1 translation, bounded queue + single
  worker thread, explicit (never ambient) span parenting, lazy turn
  roots with fold-in, dedup / stale TTL / dropped counters, bounded
  shutdown, OTLP/HTTP JSON payload + JSONL file ingestion. Full
  design in the module docstring.
- `observability/copilot_plugin/__init__.py` — thin adapter:
  `create_observer(endpoint)` builds the observer; importing the
  package registers nothing and starts nothing (there is no host
  plugin to install — see below). OTel guard: a clear RuntimeError
  if OpenTelemetry is absent.
- `observability/copilot_plugin/_sanitize.py` — vendored sanitize
  logic (M3 set + Copilot-safe keys: `copilot.*`,
  `gen_ai.response.model`, extra `gen_ai.usage.*` token counters).
- `observability/copilot_plugin/_otlp_json.py` — verbatim copy of
  `observability/otlp_json.py` (docstring note only).
- `observability/copilot_plugin/config.md` — the opt-in
  configuration: exact VS Code settings + env vars, the file-exporter
  local loop, what must stay off (content/identity capture), and the
  contract-test checklist for the reviewer.
- `observability/tests/test_copilot_plugin.py` — 18 tests (see below).
- `observability/examples/map_copilot_sample_to_agentscope_feed.py`
  — one synthetic Copilot turn mapped through the collector's ACTUAL
  `build_feed` (see "AgentScope feed mapping").
- `observability/examples/copilot-settings.json` — updated from the
  unverified M1 placeholder to the verified setting names
  (`github.copilot.chat.otel.*`); still marked not-verified against
  the installed build.

## The verified Copilot surface (official docs)

Source: https://code.visualstudio.com/docs/agents/guides/monitoring-agents
(crawled 2026-11-07). Copilot Chat natively emits OTel traces, metrics,
and events when the owner opts in. There is NO host hook/callback
plugin API for Copilot — the observer is a receiver-side adapter.

| Native span (`gen_ai.operation.name`) | Fleet span | Notes |
|---|---|---|
| `invoke_agent` | `agent.run` (turn root) | `gen_ai.conversation.id` → session; turn = `<traceId>:<rootSpanId>`; models, token totals, agent type, turn_count, git branch/commit |
| `chat` | `llm.call` | per LLM call; models, per-call tokens, time-to-first-token; outcome from status/error.type |
| `execute_tool` | `tool.exec` | tool name/type, call id → request.id; MCP/skill/edit params (hashes/names only) |
| `execute_hook` | `hook.exec` | **v1 vocabulary extension** (new span name); decision, duration, hook type, result kind |

- Subagent nesting is native: a subagent's `invoke_agent` appears as
  a child of the parent's `execute_tool` (runSubagent) span. The
  adapter preserves that nesting (subagent root parents to the tool
  span, same fleet trace).
- Metrics (token/duration counters, edit acceptance, user feedback)
  and span events (session.start, agent.turn, tool.call, edit
  feedback, survival) are NOT translated into fleet spans this round:
  events are counted, never emitted; metrics stay at the native
  endpoint.
- Services: `copilot-chat` (extension) and `github-copilot`
  (runtime + CLI terminal sessions). CLI terminal sessions are
  independent root traces, NOT linked to extension traces.
- Content capture is OFF by default; identity capture is opt-in
  only. The adapter strips both kinds of attributes even if they
  arrive (misconfigured upstream).

## Parenting: unsupported stays external

No inbound TRACEPARENT injection into Copilot Chat is documented
anywhere on the official page (the word "traceparent" does not
appear). Copilot emits its own trace IDs; there is no supported way
to parent a Copilot trace under a fleet trace. Per plan §1 the
adapter correlates, never re-parents: fleet trace IDs are
SDK-generated; the native traceId is preserved as the turn
correlation key (`turn.id`) and the native parentSpanId as the
parent link. No remote span ID is ever fabricated.

## Privacy

Allowlist before emission (same rule as M3). Stripped + counted even
when present: content-capture attributes (`gen_ai.input.messages`,
`gen_ai.output.messages`, `gen_ai.tool.definitions`,
`gen_ai.tool.call.arguments/result`,
`github.copilot.tool.parameters.command/file_path/mcp_server_name`,
`copilot_chat.hook_input/output`), identity attributes (`user.name`,
`process.user.name`, `host.name`), identifying repo attributes
(repository URL, GitHub org), `server.address`. Kept: branch +
commit SHA (opaque, short), MCP server-name hash (opaque SHA-256),
tool/skill/edit names, hook decisions. Claude agent spans
(`gen_ai.agent.name` == "claude", service `claude-code`) are out of
scope — dropped and counted, never emitted as Copilot spans.

## Test results (2026-11-07)

Command: `python3 observability/scripts/run_offline_tests.py`
(from the repo root, venv Python 3.12, OTel 1.45.1)

**77 run, 76 passed, 1 skipped, 0 failed, ~46s.**

M4 suite: 18 tests, all passing — full turn end-to-end (root + 2
chat + 2 tool + 1 hook + subagent nesting), content/identity/repo
stripping (sentinel strings absent from serialized OTLP), error
taxonomy, dedup, Claude out-of-scope, unknown operation, lazy-root
fold, parallel tools distinct, session fallback, span-event
counting, JSONL ingestion (both line shapes + junk lines + missing
file), ingest-never-raises, queue overflow, stale pruning, config-doc
parity, staged standalone load (subprocess), missing-OTel guard
(subprocess). M1 (20), M2 (11), M3 (28) suites unchanged and passing.
The 1 skip is the pre-existing live-Claude test (no real `claude`
binary on this machine).
`python3 observability/scripts/run_offline_tests.py` → PASS.

## AgentScope feed mapping

`observability/examples/map_copilot_sample_to_agentscope_feed.py`
drives the observer with one synthetic turn and runs the collector's
ACTUAL `build_feed` (2026-11-07 run): exactly one feed record,
`agents` ["copilot"], `models` ["gpt-4o"], `span_count` 3
(agent.run + llm.call + tool.exec), `top_tools` [{"tool":
"readFile", "calls": 1}], `error_count` 0. Tokens report 0 — same
as M3: `build_feed` sums tokens only from spans named exactly
`"llm"` (or `"session:*"` roots), and our spans are named
`llm.call` / `agent.run`. The token counts ARE present in the span
attributes (`gen_ai.usage.*`) for a future importer update (M5);
the current feed does not sum them. Nothing is double-counted and
nothing is zero-claimed as unknown — the attributes carry the data.

## What is NOT claimed

- **Installed VS Code contract: UNVERIFIED.** No VS Code build is
  installed on this VM. The exact JSONL line shape of the file
  exporter (full OTLP payload per line vs single span per line) is
  NOT documented on the official page — the adapter defensively
  accepts both, and the reviewer must confirm the real shape on the
  installed build (contract test in `config.md`). The default
  `otlp-http` exporter speaks protobuf, which the local JSON receiver
  rejects — the verified local loop is the file exporter, not the
  default endpoint.
- **No live Copilot run.** All M4 evidence is synthetic OTLP
  payloads through the adapter. No real Copilot session was observed.
- **Windows behavior is verified by reasoning + the reviewer's run,
  not by executing on Windows on this VM.**
- M4 acceptance is NOT DECLARED — this is a builder claim
  (Implemented + Tested), not acceptance. Nothing merges to main
  (staging-only until the end of the project, per Kyle 2026-11-07).

## Next required action

Antigravity (independent reviewer) runs on the laptop:
1. Enable the file exporter per `copilot_plugin/config.md`
   (disposable VS Code profile if possible; content/identity capture
   OFF) and send one agent-mode prompt using at least one tool.
2. Record the exact JSONL line shape and whether the four span
   kinds appear; confirm no content/identity attributes in the file.
3. Run `ingest_jsonl` on the file and confirm the fleet receiver
   gets `agent.run` / `llm.call` / `tool.exec` spans (plus
   `hook.exec` if a hook ran) with correct parenting, no
   content/identity attributes in the serialized OTLP, and zero
   `invalid_spans` for the documented shapes.
4. Confirm the adapter never blocks Copilot; stopping it changes
   nothing in VS Code.
5. Hand back M4 PASS / FAIL (or FIX ROUND REQUIRED) with evidence.
   Do not merge to main.
