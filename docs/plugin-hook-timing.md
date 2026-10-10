# Plugin hook timing (HERMES-FORK-026)

Disabled by default. After Elliot installs the published source through the native
fork-update workflow, enable in the intended profile with:

```sh
hermes config set plugins.hook_timing.enabled true
```

Rollback, without modifying plugin behavior or restarting the gateway:

```sh
hermes config set plugins.hook_timing.enabled false
```

The real config setter/readonly loader/dispatcher consumer is exercised by
`test_real_config_set_activation_disable_and_unidentified`. An already-running
process needs the new source loaded before it can instrument dispatches; no install
or restart is performed by this change. After source loading, the config cache
notices changes on later dispatches. Disabling stops new spans; queued records and
late completion of already-timed timeout workers can still drain. A bounded idle
daemon remains, writing status only. Revert the intent commit for source rollback.

## Events and interpretation

Files are profile-local, under `get_hermes_home()` as captured by the native plugin
manager (`home_path`):

- `runtime/hermes-timing/plugin-hook-events.jsonl`: append-only scalar JSON records.
- `runtime/hermes-timing/plugin-hook-status-<process_id>.json`: atomic status per
  process (written, dropped, failed, excluded, queued, updated_at), at most once
  per second under load and approximately once per second while idle. A stale status
  is not evidence the process is alive; counts are process-local and best effort.

Events: `dispatch`, `callback_wait`, `callback_execution`. Schema 1 includes
`process_id`, unique `dispatch_id`, hook allowlist name (unknown names become
`other`), mode, zero-based `callback_index`, stable native owner `plugin_id`,
`start_ns`, `end_ns`, elapsed `duration_ms`, wall-clock `observed_at`, and outcomes.
Attribution uses exact callback references on the manager's existing active
registration handles and canonical plugin keys (including category/name keys).
Untracked config/shell hooks, malformed keys, and callbacks shared by conflicting
owners emit `plugin_id: "unknown"`; index is the explicit fallback, not a guessed
module/name. No callback names, memory addresses or source paths are exported.
Indices are registration-order positions, not persistent callback identities
across reloads. Dispatch totals include bookkeeping, signature narrowing, failure
reporting, and gaps. First gap is dispatch start to first callback admission;
subsequent `gap_ms` is previous caller-wait end to next callback admission.

`callback_wait` measures how long this caller waits, including native admission,
thread creation, timeout and reporting. `callback_execution` measures actual
callback invocation/resolution: abandoned timeout workers emit independently when
they finish, possibly after dispatch end, out of file order. Join by dispatch ID
and index, not by adjacency. `admission_ms` is callback admission to execution start,
including native lock/thread scheduling. Async coroutine execution starts when its
native loop schedules the timed await, separating loop admission from body runtime;
sync callbacks on the async path remain inline. For awaitable-returning synchronous
factories, coroutine execution excludes the factory's synchronous creation cost
(which stays included in caller wait). These are wall elapsed spans, not CPU time.
Never sum caller-wait and execution as independent costs. Async cancellation can
run cleanup or be suppressed by callbacks, exactly as native `wait_for` permits.

Outcomes include ok, error, timeout, aborted, cancelled, partial dispatch, and
skipped_suppressed_or_running / skipped_worker_cap / skipped_worker_start. Skipped
callbacks have caller wait but no execution event. A permanently hung callback has
no completion event. Queue drops or crashes also create partial traces. Error text
and exception type are not exported; existing native logging is unchanged.

**The `duration_ms` supplied to `subagent_stop` is CHILD runtime, not hook runtime.**
It is never copied. Every exported duration comes from monotonic start/end. Stop
correlation uses `parent_session_id`/`parent_turn_id` when supplied. Session and turn
IDs (including parents) are raw native tokens, suitable for direct observer/session
joins. Only exact strings matching `[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}` are retained;
malformed text is omitted, never normalized, truncated or hashed. This includes
canonical timestamp/UUID sessions and colon-delimited native turn tokens. These IDs
and plugin keys are identity metadata, not anonymized data; token-shaped arbitrary
values cannot be semantically distinguished from legitimate IDs. Plugin keys are
bounded to 256 characters and slash-separated `[A-Za-z0-9][A-Za-z0-9_.-]*` segments.
Non-string IDs, request/response bodies, args, results, errors, model/provider
settings, credentials, child runtime and arbitrary kwargs are never queued.

## Conservative scope and bounds

No session or parent-session identifier: no spans. A single daemon per profile
(maximum eight profile writers per process, shared across manager reloads) reads
`state.db` with SQLite `mode=ro`, never creating/migrating the database. It uses the
actual `gateway_routing.entry_json` -> `SessionEntry.origin` ->
`SessionSource.platform/scope_id` (deprecated `guild_id` fallback). Only a verified
snapshot route agreeing on Discord guild `1517646536505557132` is persisted.
Multiple agreeing routes are accepted; any contradictory matching route fails
closed. For subagent_stop, route the parent when available. Hook kwargs claiming
guild/platform are not evidence. Unknown, rotated, stale, missing/malformed and
unreadable routes are excluded or counted failed. The background writer reuses a
session-to-approval snapshot for at most one monotonic second, refreshing on the
next event after expiry. It closes the read-only connection after every refresh.
Expiry clears trust before reading; any SQLite/JSON refresh error discards the
snapshot and throttles retries for one second. Slow refreshes cannot grant an
already-expired snapshot. Conflicts deny the matching session; missing origins
also deny it. No expired snapshot is used, but resets/route conflicts introduced
inside the one-second window can still label records using the last verified
snapshot until expiry. This bounded freshness tradeoff replaces per-event scans;
it is not instantaneous routing validation. No legacy JSON fallback or child
ancestry guessing.

Only projected scalar metadata reaches the fixed 1024-record queue. Dispatch uses
`put_nowait`, never awaits disk/routing, retries, joins, or backpressure. Full queues
drop samples. Writer initialization uses a non-waiting lock; contention can omit
whole dispatches. Status counters cannot count those omitted spans. The queue is
fixed-size; the routing snapshot scales with current native routing entries.
Append-only disk usage is not rotated automatically and needs retention
by the analysis operator. Concurrent process appenders can produce an incomplete
last record; readers must tolerate it. Abrupt exit loses queued events. Timing,
identifier validation and queue submission add small but nonzero scheduling overhead.

## Coverage: not all plugin execution

Covered when callers use `PluginManager.invoke_hook` / `ainvoke_hook`, including
pre/post tool/LLM/API hooks, session and subagent_stop. Built-in shell callbacks
registered as hooks are timed as a whole (not shell subprocess internals).
`plugins_activation._GATEWAY_TRANSFORM_HOOKS` names ordinary hook registrations:
transform_llm_output, transform_tool_result, transform_terminal_output,
pre_gateway_dispatch, gateway_platform_event and pre_command. Their dispatcher
runs are instrumented **only if a usable session ID and verified route exist**.
`gateway/run_inbound.py` currently passes event/gateway/session_store to
pre_gateway_dispatch without session_id: it is deliberately excluded, not claimed
covered. Terminal transforms commonly lack a session ID and are likewise excluded.
LLM/tool result transforms using the common hook dispatcher are eligible when
session ID exists; persistence/output application outside callbacks is not timed.

Not covered: `PluginDispatchMixin.invoke_middleware` and its
`hermes_cli.middleware` LLM/tool request/execution mutation pipeline; event bus
`_deliver_event`; prompt-section rendering; plugin load/register/unload; plugin
commands/tools/platform handler factories; first-party Relay `_observe` in
`hermes_cli.lifecycle` (runs before plugin dispatch); streaming callbacks in
`agent/plugin_stream_hooks.py` (use iter_hook_callbacks and separate bounded
per-consumer workers). Broad 'all plugin runs' therefore remains incomplete; those
surfaces need separately assessed approval and correlation plumbing, not a silent
expansion of the approved dispatcher patch.
