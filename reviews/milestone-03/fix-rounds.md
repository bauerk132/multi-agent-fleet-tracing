# Milestone 3 — Fix Round 5 (Antigravity review of M3, 2026-11-07)

**Review under fix:** Antigravity overnight review of Milestone 3
(HiveMind handoff; the full g1/g2 review files live on Kyle's laptop
only and are NOT in this repo — this round works from the 5 findings
as handed to the builder).
**Verdicts being fixed:** M1/M2 **ACCEPTED**; M3 **FAILS** the
installed-build contract test (`hermes plugins doctor` failed:
"No module named 'hermes_observer'"; runtime OTel missing; hook
signature/kwargs mismatches).
**Branch:** `main` (HEAD `5a5c8fe`). **Date:** 2026-11-07. **Builder:** Muse.
**Fix-round commit (local git):** `10a982f` — "Fix Round 5 (M3): self-contained hermes_plugin, **kwargs callbacks, kwargs mapping" (9 files, +1847/−520).

> **How to read this round:** every finding is numbered below with its
> severity (P1 / blocking acceptance, per Antigravity), the reviewer
> repro, root cause, the smallest targeted correction, and the regression
> test that proves it. Nothing here changes an interface, a dependency
> version, or unrelated production config. The OTel pins are unchanged
> (1.45.1, same as `observability/requirements.txt`); they are now
> declared in the plugin manifest as well.

## Findings

### FR5-01 — (P1, blocking acceptance): observer not loadable from a staged plugin copy

- **Reviewer repro:** `hermes plugins doctor` on the staged plugin
  failed with "No module named 'hermes_observer'".
- **Root cause:** `hermes_plugin/__init__.py` did
  `from hermes_observer import HermesObserver` (top-level import), and
  `hermes_observer.py` itself did top-level sibling imports
  (`from fleet_trace import ...`, `from otlp_json import ...`). When
  Hermes stages ONLY the `hermes_plugin` directory, the parent
  `observability/` package is not on `sys.path`, so none of those
  imports resolve.
- **Correction:** the plugin is now self-contained. The observer
  implementation moved into `hermes_plugin/observer.py` with relative
  imports only; its two sibling dependencies are vendored inside the
  package — `hermes_plugin/_otlp_json.py` (verbatim copy of
  `observability/otlp_json.py`, docstring note only) and
  `hermes_plugin/_sanitize.py` (verbatim copy of the sanitize logic
  from `observability/fleet_trace.py`: `SAFE_ATTRIBUTE_KEYS`,
  `SAFE_ERROR_TYPES`, `SAFE_ERROR_MESSAGES`, `MAX_ATTRIBUTE_CHARS`,
  `sanitize_attributes` — the coordinator half of `fleet_trace.py`
  is deliberately NOT vendored). The top-level
  `observability/hermes_observer.py` is now a thin re-export shim
  (`from hermes_plugin.observer import HermesObserver, ...`) so the
  existing M3 test suite and
  `observability/examples/map_hermes_sample_to_agentscope_feed.py`
  keep working unchanged. If the vendored sources change, copy them
  here again — never import across the package boundary.
- **Regression test:** `test_plugin_loads_standalone_from_staged_copy`
  (subprocess: copies ONLY `hermes_plugin/` to a temp dir, asserts no
  `sys.path` entry mentions `observability`, imports the plugin,
  registers 12 hooks, drives kwargs-only callbacks, exports a
  `tool.exec` span to the test receiver).

### FR5-02 — (P1, blocking acceptance): OTel dependency undeclared; missing OTel crashes the host

- **Reviewer repro:** runtime OTel missing in the staged environment;
  the plugin had no declared dependency and the failure surfaced as
  an `ImportError` traceback leaking into the host.
- **Root cause:** `plugin.yaml` declared only name/version/description
  (no `python_dependencies`), and `register()` imported the observer
  unguarded, so a missing `opentelemetry` package raised straight
  through `register()` into the host.
- **Correction:** `plugin.yaml` now declares `python_dependencies`
  with the same pins as `observability/requirements.txt`
  (`opentelemetry-api==1.45.1`, `opentelemetry-sdk==1.45.1`,
  `opentelemetry-exporter-otlp-proto-http==1.45.1`). Importing
  `hermes_plugin` itself now touches only the stdlib (`os`, `uuid`),
  so it can never crash the host. `register()` catches `ImportError`
  from the observer import: if the missing module is `opentelemetry`
  (checked via the exception's `name` / message), it raises a clear
  `RuntimeError` naming the missing dependency and the exact pins to
  install (`from None`, so no traceback chain leaks); any other
  import failure is re-raised unchanged so a real bug is not masked
  as a missing dependency.
- **Regression test:** `test_missing_otel_guard` (subprocess with a
  `MetaPathFinder` that blocks every `opentelemetry` import: plugin
  import succeeds, `register()` raises `RuntimeError` naming
  OpenTelemetry and the `opentelemetry-api==1.45.1` pin, no
  "Traceback" in output) and
  `test_plugin_yaml_declares_dependencies_and_hooks` (manifest pins
  asserted by a minimal section parser — no PyYAML dependency added).

### FR5-03 — (P1, blocking acceptance): hook callbacks use the wrong signature

- **Reviewer repro:** the host's `_accepts_var_kwargs` check rejects
  the callbacks; real Hermes runtime kwargs are never received.
- **Root cause:** callbacks were `def _callback(payload=None)` — a
  positional-payload signature. The host requires `**kwargs`.
- **Correction:** callbacks are now literally
  `def _callback(**kwargs)` in `register()` (`hermes_plugin/__init__.py`).
  No positional payload is accepted anywhere on the hook path.
- **Regression test:** `test_callback_signature_accepts_var_kwargs`
  (asserts every registered callback's signature is exactly
  `['kwargs']` with `VAR_KEYWORD` kind, then calls one with keyword
  arguments only).

### FR5-04 — (P1, blocking acceptance): Hermes kwargs not mapped to schema-v1 events

- **Reviewer repro:** hook deliveries carry Hermes runtime kwargs
  (status, error dict, subagent coordinates, human-input events
  without a turn) that the adapter never translated, so events were
  rejected or misclassified in the observer.
- **Root cause:** the adapter only adapted a positional payload dict
  (`_event_from_payload`); there was no kwargs path, no status/error
  mapping, no subagent-coordinate mapping, and `_validate` rejected
  any event with an empty `turn_id`, including human-input events
  that legitimately arrive outside a turn.
- **Correction:** new primary path `_event_from_kwargs(event_type,
  kwargs)` in `hermes_plugin/__init__.py`:
  - `status` -> `outcome` via `_map_status_to_outcome` (success /
    succeeded / ok / completed -> `observed_success`; error / failed
    -> `observed_failure`; cancelled/canceled -> `cancelled`;
    timeout/timed_out -> `timed_out`; incomplete -> `incomplete`;
    anything else -> `unknown`; a bare `outcome` kwarg already in
    the bounded enum passes through; a `success` bool maps to
    `observed_success` / `observed_failure`).
  - `error` dict -> `error_type` via `_map_error_type` (reads
    `type`/`kind`/`name`/`code`; also accepts an exception instance
    or a plain string; Timeout/Cancelled/ToolError/LLMError by
    substring, else `Unknown` — raw error text is never emitted).
  - Subagent coordinates: `subagent_session_id` / `session` ->
    `session_id`; `subagent_turn_id` / `turn` -> `turn_id`;
    `subagent_id` -> `request_id`; `parent_tool_call_id` /
    `parent_id` -> `parent_request_id`. `model_name` -> `model`;
    `tool` / `tool_call_name` -> `tool_name`.
  - `event_id` defaults to a fresh uuid4 hex; `request_id` falls
    back to the `event_id` so open/close pairing still has a key.
  - A `payload` kwarg (dict/object) is still accepted as a fallback
    source for fields the kwargs do not carry (kwargs win);
    `_event_from_payload` is unchanged and still works.
  - Observer side (`hermes_plugin/observer.py`, `_validate`):
    human-input events (`on_human_input_request` /
    `on_human_input_resolved`) no longer require a `turn_id` — a
    missing/invalid one gets the stable default `"human-input"`, and
    a missing `request_id` falls back to the `event_id`. Validation
    is unchanged for every other event type (`session_id` still
    required everywhere; `turn_id` still required elsewhere).
  - Also fixed while here: `_close_span` now records `error.type`
    on the closed span when a failure outcome carries a mapped
    `error_type` (previously the mapped value was validated and then
    silently dropped for `post_*` closes — only `api_request_error`
    set the attribute — so the error mapping had no observable
    effect for tool failures).
- **Regression tests:** `test_kwargs_status_and_error_mapping`
  (mapping tables + end-to-end: failed tool call with
  `error={"type": "TimeoutError"}` exports a `tool.exec` span with
  status ERROR and `error.type == "Timeout"`; successful api call
  exports an UNSET-status `llm.call` span),
  `test_kwargs_subagent_coordinates` (subagent kwargs parent the
  `subagent.spawn` span to the enclosing `tool.exec` span),
  `test_kwargs_human_input_without_turn_id` (human-input request
  with no `turn_id` is accepted; `approval.wait` span carries
  `turn.id == "human-input"`),
  `test_payload_dict_and_object_still_work` (dict/object payload
  path and `payload`-kwarg merge unchanged).

### FR5-05 — (P1, blocking acceptance): plugin.yaml lacks provides_hooks; no tests for the plugin itself

- **Reviewer repro:** the manifest did not declare the hooks the
  plugin provides, and there was no test coverage for
  `hermes_plugin` itself (only for the Hermes-independent observer).
- **Root cause:** `plugin.yaml` listed no `provides_hooks`, and the
  test suite had no `test_hermes_plugin.py`.
- **Correction:** `plugin.yaml` now declares `provides_hooks` with
  all 12 hook names (kept in sync with `_HOOK_NAMES` in
  `__init__.py`), and the new suite
  `observability/tests/test_hermes_plugin.py` (8 tests) covers the
  plugin adapter, manifest, and both subprocess isolation checks.
- **Regression test:** `test_plugin_yaml_declares_dependencies_and_hooks`
  (manifest `provides_hooks` equals `_HOOK_NAMES`, and the hooks
  `register()` actually wires equal the manifest list) plus the full
  new suite above.

## Test results (re-run for this round, 2026-11-07)

Command: `../.venv/bin/python -m unittest discover -s tests -t . -v`
(from `observability/`, venv Python 3.12, OTel 1.45.1)

**53 run, 52 passed, 1 skipped, 0 failed, ~33s.**

New tests in this round (8, all in `tests/test_hermes_plugin.py`):
`test_callback_signature_accepts_var_kwargs`,
`test_kwargs_status_and_error_mapping`,
`test_kwargs_subagent_coordinates`,
`test_kwargs_human_input_without_turn_id`,
`test_payload_dict_and_object_still_work`,
`test_plugin_yaml_declares_dependencies_and_hooks`,
`test_plugin_loads_standalone_from_staged_copy`,
`test_missing_otel_guard`. The M3 suite grew 14 → 22 tests; M1 (20)
and M2 (11) suites are unchanged and all passing, including the
pre-existing M3 tests, which now exercise the observer through the
top-level re-export shim (no test changes were needed there). The 1
skip is the pre-existing live-Claude test (no real `claude` binary on
this machine).

Also verified: `python3 scripts/run_offline_tests.py` → PASS (same
53/1-skipped result); vendored `_otlp_json.py` differs from
`observability/otlp_json.py` only in the docstring note, and the
vendored `sanitize_attributes` + constants are identical to
`fleet_trace.py` (diff-checked).

**Windows verification caveat:** this VM is Linux, so the Windows-side
behavior is verified by reasoning + Antigravity's Windows run, not by
executing the suite on Windows here. The re-review on the laptop is
the Windows verification step for this round.

## Details NOT resolvable from the code (for Antigravity)

These were not in the review findings as handed to the builder, and
the full g1/g2 review files are not in this repo. They need the
laptop-side contract test, not a guess here:

1. **Exact Hermes runtime kwarg names / status vocabulary.** FR5-04
   was implemented against plausible/documented alias names
   (`session_id`/`session`/`subagent_session_id`,
   `subagent_id`/`parent_tool_call_id`, `status` values such as
   `success`/`failed`/`cancelled`/`timed_out`). If the installed
   Hermes build passes differently-named kwargs or different status
   strings, the mapping table in `hermes_plugin/__init__.py`
   (`_STATUS_TO_OUTCOME`, `_map_error_type`, `_event_from_kwargs`)
   needs the real names — the live hook-delivery step of the
   contract test is the place to confirm them.
2. **Manifest schema shape.** `python_dependencies` (list of pinned
   specifiers) and `provides_hooks` (list of hook names) are written
   in the natural YAML form for those keys. If Hermes expects a
   different shape (e.g. a mapping, or a different key name),
   `hermes plugins doctor` on the laptop is the verification — the
   builder cannot confirm the schema from this VM.
3. **Payload vs flat kwargs.** The adapter handles both (flat kwargs
   primary, `payload` kwarg as fallback). If the host delivers
   neither — e.g. a single positional payload object — the
   `**kwargs` callback would receive nothing and the event would be
   rejected as invalid (counted, never raised); the contract test
   should confirm the actual delivery shape.

## Gate status after Fix Round 5

- **M3 acceptance:** NOT DECLARED. Fix Round 5 is a builder claim
  (Implemented + Tested), not acceptance.
- **Next required action:** Antigravity re-runs the installed-build
  contract test on the laptop from a clone (disposable Hermes
  profile, never the active profile): `hermes plugins doctor` on the
  staged `hermes_plugin/` directory, hook delivery with real runtime
  kwargs, and confirmation of the three open details above. On a
  PASS it records the M3 verdict; on a FAIL it hands back the exact
  kwarg names / manifest-shape correction needed.
