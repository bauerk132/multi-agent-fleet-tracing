# Copilot observer — opt-in configuration (Milestone 4)

**Status:** Implemented + Tested on synthetic payloads. **NOT verified
against an installed VS Code build** — the contract test (below) is
the verification. Do NOT present this as live until that passes.

## What this is

Copilot Chat in VS Code natively emits OpenTelemetry traces when the
owner opts in. There is no plugin to install into VS Code and no hook
to register: the observer (`copilot_plugin/`) is a receiver-side
adapter that consumes what Copilot exports and re-emits it as fleet
schema-v1 spans. Nothing in this repo enables Copilot telemetry —
the owner does that, in VS Code settings, on the dev machine.

Source (verified 2026-11-07):
https://code.visualstudio.com/docs/agents/guides/monitoring-agents

## Opt in (owner, on the dev machine)

VS Code settings (`settings.json`):

```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "file",
  "github.copilot.chat.otel.outfile": "C:\\Users\\kbaue\\copilot-otel.jsonl",
  "github.copilot.chat.otel.captureContent": false,
  "github.copilot.chat.otel.captureIdentity": false
}
```

Or the equivalent environment variables (env vars take precedence
over settings):

```
COPILOT_OTEL_ENABLED=true
COPILOT_OTEL_FILE_EXPORTER_PATH=C:\Users\kbaue\copilot-otel.jsonl
COPILOT_OTEL_CAPTURE_CONTENT=false
COPILOT_OTEL_CAPTURE_IDENTITY=false
```

Notes:

- `exporterType: "file"` is the verified local loop: the file
  exporter writes one JSON value per line. The default exporter is
  `otlp-http` (protobuf) to `http://localhost:4318`; the local
  receiver speaks OTLP/HTTP **JSON**, so pointing the default
  exporter at it would be rejected as a transport mismatch — use
  the file exporter for the local loop, or put a real OTLP collector
  (protobuf-capable) in front of the adapter's `handle_payload`.
- `captureContent` and `captureIdentity` MUST stay `false`. The
  adapter strips content/identity attributes even if they arrive,
  but the correct control is to leave them off upstream.
- Managed (enterprise) settings override user settings; on a managed
  machine the `telemetry` block in `managed-settings.json` wins.
- OTel is off by default; nothing is exported until the owner
  enables it. Restart / reload the window after changing settings
  if prompted.

## Run the adapter (owner, on the dev machine)

```python
from copilot_plugin import create_observer

observer = create_observer()  # fleet endpoint from
# OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, loopback default
summary = observer.ingest_jsonl(r"C:\Users\kbaue\copilot-otel.jsonl")
print(summary)  # lines / spans_enqueued / skipped / invalid / errors
observer.drain(timeout=10.0)
print(observer.diagnostics())
observer.shutdown(timeout=10.0)
```

Or wire an OTLP/HTTP JSON receiver in front of `handle_payload`
(see `observability/receiver.py` for the local receiver shape).

## Contract test (reviewer, on the dev machine)

1. Enable the file exporter as above (disposable VS Code profile
   if possible; never the owner's active profile with content
   capture on). Send one Copilot agent-mode prompt that uses at
   least one tool.
2. Confirm the JSONL file exists and record the exact line shape:
   is each line a full OTLP payload (`resourceSpans`) or a single
   span? Which span kinds appear (`invoke_agent`, `chat`,
   `execute_tool`, `execute_hook`)? Does the file contain any
   content-capture attributes (`gen_ai.input.messages`,
   `gen_ai.tool.call.arguments`, `...command`, `...file_path`) or
   identity attributes (`user.name`, `host.name`) with capture off?
3. Run `ingest_jsonl` on the file and confirm: the fleet receiver
   gets `agent.run` / `llm.call` / `tool.exec` spans (plus
   `hook.exec` if a hook ran), with correct parenting (tool spans
   under their turn root; a subagent `invoke_agent` under the
   `runSubagent` tool span), no content/identity attributes in the
   serialized OTLP, and diagnostics showing zero `invalid_spans`
   for the documented shapes.
4. Confirm the adapter never blocks Copilot: chat stays responsive
   while the adapter ingests; stopping the adapter changes nothing
   in VS Code.
5. Hand back M4 PASS / FAIL (or FIX ROUND REQUIRED) with evidence.
   Do not merge to main (staging-only until the end of the project).
