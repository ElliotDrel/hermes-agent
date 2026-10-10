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
  process (written, dropped, failed, excluded, queued, updated_at). A stale status
  is not evidence the process is alive; counts are process-local and best effort.

Events: `dispatch`, `callback_wait`, `callback_execution`. Schema 1 includes
`process_id`, unique `dispatch_id`, hook allowlist name (unknown names become
`other`), mode, zero-based callback index, process-local callback identity
(`hex(id(cb))`), `start_ns`, `end_ns`, elapsed `duration_ms`, wall-clock
`observed_at`, and outcomes. No plugin/callback names or source paths are retained.
Callback IDs may be recycled after unload; do not attribute across registrations
or processes. Dispatch totals include bookkeeping, signature narrowing, failure
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
IDs (including parents) are SHA-256 digests, not raw strings: the daily analyzer
must apply SHA-256 to native identifiers before joining. IDs longer than 256 chars,
non-string IDs, request/response bodies, args, results, errors, model/provider
settings, credentials, child runtime and arbitrary kwargs are never queued.

## Conservative scope and bounds

No session or parent-session identifier: no spans. A single daemon per profile
(maximum eight profile writers per process, shared across manager reloads) reads
`state.db` with SQLite `mode=ro`, never creating/migrating the database. It uses the
actual `gateway_routing.entry_json` -> `SessionEntry.origin` ->
`SessionSource.platform/scope_id` (deprecated `guild_id` fallback). Only a current
route uniquely agreeing on Discord guild `1517646536505557132` is persisted.
Multiple agreeing routes are accepted; any contradictory matching route fails
closed. For subagent_stop, route the parent when available. Hook kwargs claiming
guild/platform are not evidence. Unknown, rotated, stale, missing/malformed and
unreadable routes are excluded or counted failed, never labeled as guild work.
Route reads occur when writing, so a reset between dispatch and write can lose an
otherwise valid sample. No legacy JSON fallback or child ancestry guessing.

Only projected scalar metadata reaches the fixed 1024-record queue. Dispatch uses
`put_nowait`, never awaits disk/routing, retries, joins, or backpressure. Full queues
drop samples. Writer initialization uses a non-waiting lock; contention can omit
whole dispatches. Status counters cannot count those omitted spans. Memory is
bounded; append-only disk usage is not rotated automatically and needs retention
by the analysis operator. Concurrent process appenders can produce an incomplete
last record; readers must tolerate it. Abrupt exit loses queued events. Timing,
hashing and queue submission add small but nonzero scheduling overhead.

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
