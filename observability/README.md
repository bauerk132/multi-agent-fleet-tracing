# Milestone 1 — Safe local receiver + coordinator + 12-span fixture

Part of the **Multi-Agent Fleet Tracing** plan (Notion: "Multi-Agent Fleet
Tracing — OpenTelemetry Integration Plan", under Side Projects). This
milestone builds the foundation only: a local OTLP receiver, the
coordinator with the plan's fixed propagation, an instrumented controlled
child, and a deterministic 12-span fixture — all passing locally.

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
  otlp_json.py                OTLP JSON encoder + stdlib exporter
  tests/test_fleet_trace.py   unittest suite (11 tests)
  hermes_plugin/              UNVERIFIED stubs only — do not install
  examples/                   staged doc examples only — unverified
  out/                        receiver JSONL output (created on run)
```
