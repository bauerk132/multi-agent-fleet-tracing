# Milestones 1–2 — Safe local receiver + coordinator + 12-span fixture, and the Claude Code controlled child trace

Part of the **Multi-Agent Fleet Tracing** plan (Notion: "Multi-Agent Fleet
Tracing — OpenTelemetry Integration Plan", under Side Projects). Milestone
1 builds the foundation: a local OTLP receiver, the coordinator with the
plan's fixed propagation, an instrumented controlled child, and a
deterministic 12-span fixture — all passing locally. Milestone 2 adds
the harness that verifies a controlled `claude -p` session accepts
incoming TRACEPARENT (`claude_child.py`); the live run must happen on
the dev machine, where Claude Code is installed and signed in.

## What this milestone covers

- Local OTLP receiver (`receiver.py`, stdlib only) on localhost.
- Coordinator (`fleet_trace.py`) implementing the plan's initialization
  order, including the **S1 fixes** from the Trace Architect review:
  child env from an explicit allowlist (never `os.environ.copy()`),
  `propagate.inject()` while the handoff span is current, and child
  extraction with composite W3C propagators.
- Instrumented controlled child (`child.py`) that extracts context and
  reports whether it is genuinely parented.
- Deterministic 12-span fixture (`fixture.py`): one coordinator trace,
  two `agent.run` spans (coordinator + controlled child), two handoffs,
  one failed `tool.exec`, `state.write` refs, synthetic `approval.wait`.
- Tests (`tests/test_fleet_trace.py`, unittest) covering every acceptance
  criterion in the plan's test table plus the S1 fixes.

## What this milestone does NOT cover

- **Hermes** — `hermes_plugin/` is an UNVERIFIED stub only (Milestone 3).
  Do not install it. The plugin contract is unverified against any
  installed Hermes build.
- **Copilot / Antigravity** — `examples/` holds staged documentation
  examples only (Milestones 4–6). They stay external in this milestone.
- **ChatGPT desktop** — external only; no instrumentation hook exists.
- **AgentScope ingestion** — Milestone 5, after the importer schema is
  validated against current code.

## Quick start

```bash
cd multi-agent-fleet-tracing
python3 -m venv .venv
.venv/bin/pip install -r observability/requirements.txt

# 1. Start the local receiver (ephemeral port; it prints its URL)
.venv/bin/python observability/receiver.py --out observability/out/received.jsonl
# listening on http://127.0.0.1:<PORT>/v1/traces

# 2. In another shell, run the coordinator end to end (same endpoint):
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:<PORT>/v1/traces
.venv/bin/python observability/fleet_trace.py

# 3. Export the deterministic 12-span fixture:
.venv/bin/python observability/fixture.py --endpoint http://127.0.0.1:<PORT>/v1/traces

# 4. Run the tests:
cd observability && ../.venv/bin/python -m unittest discover -s tests -t . -v
```

## Transport note (read this)

The local receiver speaks **OTLP/HTTP JSON**. The pinned
`opentelemetry-exporter-otlp-proto-http` package serializes with protobuf
(`application/x-protobuf`), and there is no official OTLP JSON exporter
package on PyPI, so the local loop uses `otlp_json.OTLPJSONExporter` — a
small stdlib (urllib) exporter emitting spec-compliant OTLP JSON. The
pinned proto-http exporter is installed and can be used against a real
OTLP collector by pointing `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` at it;
the stdlib-only local receiver does not decode protobuf. That is a
documented limitation, not a silent substitution.

## Privacy

- Attributes pass through an explicit allowlist (`sanitize_attributes`
  in `fleet_trace.py`). Anything else is dropped; secret-looking values
  become `[REDACTED]`; `error.message` is restricted to generic messages.
- Never stored in spans: API keys, auth headers, cookies, prompt bodies,
  raw tool inputs/outputs, unredacted file content, PII.
- The child process receives ONLY allowlisted env vars plus explicit
  trace context. `FLEET_TASK_ID` / `FLEET_SESSION_ID` are opaque
  correlation IDs, not content.
- No guarantee for SIGKILL or sudden power loss: spans are exported on
  span end (child, SimpleSpanProcessor) or on `force_flush()` /
  `shutdown()` (coordinator, BatchSpanProcessor + atexit flush).

## Layout

```
observability/
  requirements.txt            pinned OTel deps (api/sdk/proto-http 1.45.1)
  fleet_trace.py              coordinator (init order, fixed propagation)
  receiver.py                 stdlib-only local OTLP/HTTP JSON receiver
  fixture.py                  deterministic 12-span fixture
  child.py                    instrumented controlled child
  claude_child.py             Milestone 2: controlled `claude -p` launcher + detection
  otlp_json.py                OTLP JSON encoder + stdlib exporter
  tests/test_fleet_trace.py   unittest suite, Milestone 1 (11 tests)
  tests/test_claude_child.py  unittest suite, Milestone 2 (7 tests)
  hermes_plugin/              UNVERIFIED stubs only — do not install
  examples/                   staged doc examples only — unverified
  out/                        receiver JSONL output (created on run)
```

## Milestone 2 — Claude Code controlled child trace

Goal: prove a controlled non-interactive `claude -p` session accepts
incoming TRACEPARENT and emits spans under the coordinator's trace with
the handoff span as parent. The harness (`claude_child.py`) launches the
child with the coordinator's fixed env allowlist plus the plan's
candidate Claude Code telemetry flags, then classifies the outcome from
what the local receiver actually received. It never invents telemetry
behavior: it detects and reports.

**Native OTel export alone does NOT prove inbound W3C support.** Only a
traceId + parentSpanId match proves the child accepted the injected
context. A child that exports spans under its own trace is EXTERNAL,
not parented, even if its telemetry is on.

### Prerequisites (dev machine)

- Claude Code CLI installed and signed in (`claude --version` works).
- This repo's venv with the pinned deps installed
  (`.venv/bin/pip install -r observability/requirements.txt`).
- Nothing else: the harness starts its own in-process local receiver.

### The exact command

```bash
cd multi-agent-fleet-tracing
.venv/bin/python observability/claude_child.py --prompt "Reply with exactly: OK"
```

It prints one JSON result. Exit codes: **0 = PARENTED** (Milestone 2
verified), **1 = not parented / error / unverified**, **2 = skipped**
(`claude` CLI not found — nothing was launched).

### What PASS looks like

```json
{
  "status": "parented",
  "claude_version": "1.0.x (Claude Code)",
  "child_parented": true,
  "detection": {
    "classification": "parented",
    "child_trace_ids": ["<same as trace_id>"],
    "child_parent_span_ids": ["<same as handoff_span_id>"]
  },
  "flush_ok": true
}
```

`child_trace_ids` must equal the coordinator `trace_id`, and
`child_parent_span_ids` must equal the `handoff_span_id`.

### What FAIL looks like

- `"status": "external"` — claude spans arrived under a different trace
  id. **Fallback: external handoff only.** Correlate by FLEET_* IDs;
  do not present the child as parented.
- `"status": "unverified"` — no claude spans arrived at the receiver.
  Check `rejected_payloads` in the result: if the child exported
  protobuf (`application/x-protobuf`), the stdlib receiver cannot decode
  it — the spans were attempted, not received. That is a transport
  mismatch, not proof of anything.
- `"status": "error"` — the child exited non-zero or timed out. The
  handoff span carries ERROR status with generic metadata only; the
  result records the returncode and a bounded stderr tail.
- `"classification": "parented_to_other"` — claude spans share the
  coordinator trace but are parented to a different span. Context
  propagated, wrong parent. Also NOT handoff-parented.

### Candidate flags (read this)

The harness sets `CLAUDE_CODE_ENABLE_TELEMETRY=1`,
`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`, `OTEL_TRACES_EXPORTER=otlp`,
and `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` (to steer the child toward
JSON the local receiver can read). These are the plan's **candidate**
flags — their spelling and values are not verified against any installed
Claude Code build. The result always reports the installed version and
the flags used, so a version difference is visible instead of silent.
If the installed build uses different flags, update
`CLAUDE_TELEMETRY_FLAGS` in `claude_child.py` and re-run; do not claim
the old flags worked.

The child's raw stdout/stderr are never placed on a span — only a
500-char tail goes in the result dict. Full model output is not
retained by the harness.

### Tests

`tests/test_claude_child.py` proves the harness with a stub `claude`
stand-in (posts one OTLP/HTTP JSON span, dumps its env): parented
end-to-end, no secrets in the child env, missing/invalid TRACEPARENT
goes external, non-zero exit is an error, the classifier's four
outcomes, and the receiver recording rejected payloads. The live test
against the real binary skips cleanly when no real `claude` is
installed; on the dev machine it runs one minimal `claude -p` call.
