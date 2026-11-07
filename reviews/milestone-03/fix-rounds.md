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

---

# Milestone 3 — Fix Round 6 (Antigravity FR5 re-review, 2026-11-07)

**Review under fix:** Antigravity's FR5 re-review (HiveMind handoff
hof_01M6V6W8TGKSDSG9CW; the full g1/g2 review files live on Kyle's
laptop only and are NOT in this repo — this round works from the 6
findings as handed to the builder, plus the exact live kwargs traces
Antigravity sent in handoff hof_01M6VANB718PNZVHNP at the builder's
request).
**Verdicts being fixed:** M1/M2 **ACCEPTED** (unchanged); M3 **still
FAILS** — the static contract test now passes (`hermes plugins doctor`
exit 0, offline tests 52/53), but live Hermes Agent v0.23.0 runtime
kwargs delivery has 6 defects: subagent events and session-boundary
events are dropped as invalid, human-input outcomes and tool error
types degrade to unknown, parallel tool calls collapse onto one dedup
key.
**Branch:** `main` (HEAD `ca6fe35`). **Date:** 2026-11-07. **Builder:** Muse.
**Fix-round commit (local git):** `92d1de3` — "Fix Round 6 (M3): live Hermes runtime kwargs fixes" (5 files, +624/−22).

> **How to read this round:** same as FR5 — every finding with
> severity (P1 / blocking acceptance, per Antigravity), the reviewer
> repro, root cause, the smallest targeted correction, and the
> regression test that proves it. All six corrections are in
> `observability/hermes_plugin/__init__.py` (`_event_from_kwargs`,
> `_callback`) and `observability/hermes_plugin/observer.py`
> (`_validate`). Nothing here changes an interface, a dependency
> version, or unrelated production config. The OTel pins are unchanged
> (1.45.1).

## Findings

### FR6-01 — (P1): subagent kwargs not mapped; start/stop dropped as invalid

- **Reviewer repro:** live `subagent_start` /
  `subagent_stop` deliveries rejected by `observer._validate` as
  invalid_events. Fire sites: `tools/delegate_tool.py:356`
  (subagent_start), `tools/delegate_tool_results.py:382`
  (subagent_stop).
- **Live kwargs:** start — `parent_session_id, parent_turn_id,
  parent_subagent_id, child_session_id, child_subagent_id, child_role,
  child_goal`; stop — `parent_session_id, parent_turn_id,
  child_session_id, child_role, child_summary, child_status,
  duration_ms`.
- **Root cause:** `_event_from_kwargs` picked `session_id` from
  `('session_id', 'session', ...)` and `turn_id` from `('turn_id',
  'turn', ...)` — none of which the live subagent events carry — so
  both came out `''` and validation rejected them. `request_id` had
  no `child_subagent_id` early enough, `status` never checked
  `child_status`, and `parent_request_id` never checked
  `parent_subagent_id`.
- **Correction (`__init__.py`):** session_id from
  `('parent_session_id', 'session_id', 'session', 'child_session_id',
  'subagent_session_id', 'agent_session_id')`; turn_id from
  `('parent_turn_id', 'turn_id', 'turn', 'subagent_turn_id')`;
  request_id per FR6-05 (below); status falls through to
  `child_status` when no `status` kwarg is present;
  parent_request_id from `('parent_subagent_id',
  'parent_request_id', 'parent_id', 'parent_tool_call_id',
  'parent_request')` — the FR5 aliases (`parent_tool_call_id` et
  al.) are kept after the two new keys so the FR5 subagent-parenting
  test still passes.
- **Test:** `test_fr6_subagent_live_kwargs_mapping` — unit-level
  mapping of both exact live shapes plus end-to-end acceptance
  (accepted 3 with session end, invalid_events 0, one
  `subagent.spawn` span exported with session/turn/request from the
  parent/child ids).

### FR6-02 — (P1): session-boundary events dropped; `_end_session()` never runs

- **Reviewer repro:** live `on_session_finalize` /
  `on_session_reset` deliveries dropped as invalid_events. Fire
  sites: `hermes_cli/lifecycle.py:83,97` (on_session_finalize),
  `hermes_cli/cli_session_mixin.py:513` (on_session_reset).
- **Live kwargs:** `session_id, platform, reason` — no `turn_id`
  is sent; teardown happens outside of turns.
- **Root cause:** `_validate` exempted only human-input events
  from requiring `turn_id`; finalize/reset arrived with `turn_id ==
  ''` and were rejected, so `_end_session()` never executed.
- **Correction (`observer.py`, `_validate`):**
  `on_session_finalize` and `on_session_reset` join the turn_id
  exemption list; a missing/invalid turn_id on those events gets
  the stable default `"session-boundary"` (same pattern as
  `"human-input"` for human-input events).
- **Test:** `test_fr6_session_boundary_without_turn_id` — a tool
  span is opened, then finalize + reset arrive with the live shape
  (no turn_id); accepted 3, invalid_events 0, and the orphaned tool
  span is exported as incomplete, proving `_end_session()` ran.

### FR6-03 — (P1): human-input outcomes degrade to `unknown`

- **Reviewer repro:** `on_human_input_resolved` outcomes
  `'timeout'` and `'error'` recorded as `unknown`. Fire site:
  `tools/human_input_hooks.py:72`.
- **Live kwargs:** `request_id, session_id, outcome='timeout' |
  'error' | 'cancelled' | 'provided'` — no `status` kwarg.
- **Root cause:** `_event_from_kwargs` only called
  `_map_status_to_outcome` when `'status'` was in kwargs. A bare
  `outcome` outside the bounded enum fell through to `outcome =
  "unknown"`, so `'timeout'` (enum: `'timed_out'`) and `'error'`
  (enum: `'observed_failure'`) never matched.
- **Correction (`__init__.py`):** a bare `outcome` kwarg not in
  `_OUTCOMES` is passed through `_map_status_to_outcome`, so
  `'timeout' -> 'timed_out'`, `'error' -> 'observed_failure'`,
  `'provided' -> 'observed_success'` (new `"provided"` entry in
  `_STATUS_TO_OUTCOME`; `'cancelled'` already passes through
  unchanged).
- **Test:** `test_fr6_human_input_outcome_normalization` — unit
  table for all four live values plus end-to-end: a timed-out
  approval closes with ERROR span status.

### FR6-04 — (P1): tool error type degrades to `Unknown`

- **Reviewer repro:** `post_tool_call` with
  `error_type='tool_error'` recorded as `Unknown`. Fire site:
  `model_tools.py:704` (post_tool_call).
- **Live kwargs:** `tool_name, tool_call_id, status='error',
  error_type='tool_error', error_message, error=None`.
- **Root cause:** `_event_from_kwargs` only called
  `_map_error_type` when `'error'` was in kwargs. With
  `error=None` it took the raw `error_type='tool_error'`, and
  `_validate` enforces `ERROR_TYPES = {'LLMError', 'ToolError',
  'Timeout', 'Cancelled', 'Unknown'}` — lowercase `'tool_error'`
  is not in the enum, so it degraded to `'Unknown'`.
- **Correction (`__init__.py`):** a bare `error_type` kwarg is
  passed through `_map_error_type`, so `'tool_error' ->
  'ToolError'`. (An `error` value, when present, still wins.)
- **Test:** `test_fr6_tool_error_type_normalization` — unit-level
  mapping of the exact live shape plus end-to-end: the closed
  `tool.exec` span carries `error.type == 'ToolError'` with ERROR
  status.

### FR6-05 — (P1): parallel tool calls collapse onto one dedup key

- **Reviewer repro:** in parallel tool execution, secondary tools
  are dropped as duplicates. Fire site: `model_tools.py:656,706`
  (`_CallIds.hook_kwargs()`).
- **Live kwargs:** both `tool_call_id` and `api_request_id` are
  supplied; multiple parallel tools share the same `api_request_id`.
- **Root cause:** `_pick` used order `('request_id',
  'api_request_id', 'tool_call_id', ...)` for every event type.
  Because `api_request_id` precedes `tool_call_id`, all parallel
  tools got `request_id = api_request_id`, generating identical
  dedup keys in `handle_event` and dropping the secondaries.
- **Correction (`__init__.py`):** for `pre_tool_call` and
  `post_tool_call`, `request_id` is picked from `('tool_call_id',
  'request_id', 'call_id', 'api_request_id')`; all other events
  keep `('request_id', 'api_request_id', 'child_subagent_id',
  'subagent_id', 'call_id')`. (For subagent events the FR6-01 order
  and this order resolve identically — neither carries
  `request_id`/`api_request_id` — so `child_subagent_id` still
  wins there.)
- **Test:** `test_fr6_parallel_tool_call_ids_stay_distinct` —
  unit-level precedence for tool vs non-tool events plus
  end-to-end: two parallel tools sharing one `api_request_id`
  export as two distinct `tool.exec` spans with request ids
  `tc-a`/`tc-b`, duplicates 0.

### FR6-06 — (P1): callback exceptions could escape into the host runtime

- **Reviewer repro:** defensive gap, no live crash observed — a
  malformed delivery that raises inside the kwargs -> event
  mapping step (before `handle_event`'s own try/except) would
  propagate out of the callback into the Hermes host runtime.
- **Root cause:** `_callback` called
  `observer.handle_event(_event_from_kwargs(...))` unguarded;
  `handle_event` never raises, but the mapping step had no trap.
  (Concrete crash vector: an unhashable `outcome` value, e.g. a
  list, breaks the `in _OUTCOMES` check with `TypeError`.)
- **Correction (`__init__.py`):** `_callback(**kwargs)` wraps the
  mapping + handle call in `try/except` and returns `None` on
  failure — zero exceptions escape into the host. (Loss is silent
  at the callback; a mapping failure means no event was formed to
  count.)
- **Test:** `test_fr6_callback_exception_trap` — a callback
  delivered with `outcome=["not", "a", "string"]` returns `None`
  without raising, accepts nothing, and normal delivery still
  works afterwards.

## Details NOT resolvable from the code (for the reviewer)

1. **Subagent stop pairing.** The live `subagent_stop` kwargs
   carry no subagent id at all (no `child_subagent_id`, no
   `parent_subagent_id`, no `request_id`). Under the FR6-01/FR6-05
   pick orders its `request_id` therefore falls back to the fresh
   `event_id`, so it cannot pair-close the span opened by
   `subagent_start` (keyed on `child_subagent_id`). The span is
   still exported — reconciled as incomplete at turn/session end,
   never dropped from export — but the stop does not close it with
   the stop's outcome. If the intended pairing is via
   `child_session_id` (present on both start and stop) or another
   key, the reviewer should say so; the builder implemented the
   handoff's pick orders exactly and did not invent a pairing key.
   The FR6-01 regression test locks in the current behavior (span
   exported, completeness `incomplete` after session end).
2. **Windows behavior** is verified by reasoning + the reviewer's
   run, not by executing the suite on Windows on this VM. The
   re-review is the Windows verification step for this round.

## Gate status after Fix Round 6

- **M3 acceptance:** NOT DECLARED. Fix Round 6 is a builder claim
  (Implemented + Tested), not acceptance.
- **Next required action:** Hermes (not Antigravity — Kyle
  redirected the FR6 re-review to Hermes on 2026-11-07; he wants
  differing models weighing in rather than a single reviewer for
  everything) pulls staging at the new FR6 commit, verifies it, and
  runs the installed-build / live kwargs contract test in its own
  Hermes runtime, then records its verdict and hands the outcome
  back to claude-muse. The plugin must NOT be enabled in the active
  Hermes profile until the contract test passes, and nothing merges
  to main without Kyle's explicit go-ahead.
