# Maintained Hermes changes

Base: official stable v0.23.2, `ae2a81ccc00e9b7c06301523e5bb09608397149c`.
`main` stays exact official stable; `live` adds the intents below. Preserve these behaviors when rebasing.

## HERMES-FORK-023: Native Windows update drain

Windows updates use the normal restart exit budget, including its after-turn wait.
Upstream's marker-first crash custody (#92091) is kept: the planned-stop marker and
crash-ledger hook precede the socket pause. A gateway that accepts a non-forced pause
sets `_update_pause_pending`, and the planned-stop watcher defers to that
restart-when-idle wait; it also fires only on a marker seen on two consecutive ticks,
so the ACK lands first. The fire is a loop-thread callback that re-checks the flag
(loop FIFO runs the earlier-queued pause request first) and, if it declines, the
watcher keeps polling. `restart_when_idle._run_restart` clears the flag in a `finally`
around `stop()`; a failure before that point (e.g. in the idle wait) is not covered.
Old/unreachable gateways still stop via the marker. The
existing drain reporter shows active work while waiting. Native timeout, forced-stop
fallback, install and restart remain native.

Touchpoints: hermes_cli/update_cmd_windows.py, gateway/run.py
(`_run_planned_stop_watcher`), gateway/run_bootstrap.py (`_pause_for_update_handler`).
v0.23.2 verification: 53 passed, 0 failed (4 POSIX-only skipped) across
test_planned_stop_watcher, test_windows_update_drain, test_control_socket_pause,
test_restart_drain, test_update_concurrent_quarantine and the Windows cold-start
lifecycle suite (incl. the loop-recheck and flag-release tests). The live Windows e2e
test_pause_custody was not run.

## HERMES-FORK-001: Rebase preparation with native installation

Discord `/update` enters the profile's hermes-fork-update skill as an explicit
prepare-only deploy-existing-live user turn. `/update stable` selects stable rebase
preparation. Neither grants install/drain/restart permission. Ordinary admission,
authorization, session history and cached system inputs remain intact; busy
sessions reject. CLI `hermes update` remains backward-compatible. Native pinned
staging and user activation (028) replace the old detached-install handoff.

The update skill reviews keep/adapt/retire decisions in the current run's thread.
Established preferences remain settled; proposed behavior changes wait for Elliot's
decision. Published live may be ahead of the installed checkout even when stable
has not changed. Preparation accepts that clean ancestor state, while unpublished
or divergent installed commits require review. Installation is skipped only when
installed and prepared revisions match; running revision is verified separately.

Preparation reports through its invoking agent turn without arming the old
install-pending marker/watcher. Ordinary native updates retain their reporting.

## HERMES-FORK-016: Per-run Discord update reporting

Reuse the invoking thread; create one when invoked outside a thread. Stream native
output and deliver completion to that run, falling back to its configured main chat
on delivery failure. Never inherit a previous home thread. Preserve undelivered
receipts, inspect returned send failures, and mark notices nonconversational.
Preparation can precede the normal restart wait, so the watcher includes that budget.
Share public-thread creation, requester membership and archive settings with
ordinary auto-threads.

## HERMES-FORK-005: Discord visual-only response metadata

A bounded transform_gateway_response hook receives the completed agent's live
model/provider, compressor.last_total_tokens, threshold_tokens and compression_count.
Only Discord final send/edit and queued responses consume its display string.
SQLite/JSON, model-facing messages, post-turn observers and cache prefixes keep the
original answer. Self-authored Discord reply previews and anchored backfill strip
recognized trailing metadata from input copies; earlier stored history is untouched.
The workspace formatter performs no route DB reads, network model metadata or
subscription-quota refresh. Percent is labeled "of compaction trigger" and uses the
effective runtime threshold, including small-context floors/caps, not the nominal
window or configured raw cap. Missing telemetry is not fabricated.
Touchpoints: `gateway/response_metadata.py` (new: `transform_discord_response`,
`prepare_delivery_response`, `delivery_text`, footer strip); `gateway/run_turn.py`
(completion path after persistence, queued first-response lane, streamed-edit
reconciliation using `delivery_response`/`delivery_transformed`);
`gateway/run_turn_runner.py` (read-only telemetry keys on the turn result);
`hermes_cli/plugins*.py` (hook registration); Discord adapter strips (`_keep`, reply
preview). Not ported: fork commit b14784c1d4 (durable ctx/compaction metadata) wrote
session meta and changed transform_llm_output payloads; v0.23.2 telemetry is read
live instead. The progress-header percent change (39197b980c) belongs to 017.
When the body already streamed to a split or `__no_edit__` message, the display-only
copy cannot be edited in: the turn is marked `already_sent` and that turn's footer is
dropped rather than re-sending the whole body (review fix; queued lane shares it). A
rejected in-place footer edit (e.g. a Discord 429) takes the same path: the streamed body
stands, the footer is dropped, nothing is posted twice (not for plugin response_transformed).
v0.23.2 verification: tests/gateway/test_response_metadata.py (13), the Discord
free-response suite and run_progress_topics suite (55, incl. four streaming/queued
matrix cases and the failed-footer-edit no-resend case asserting history and result messages stay footer-free) all pass.
Approval: Elliot granted a change-specific manual override on 2026-11-17 (T3 session)
for re-porting this settled behavior onto v0.23.2 after a per-intent assessment of
model-facing effect, failure mode and rollback; it covers the already-committed footer
stripping from Discord reply previews and backfill input (fb3a1caceb).

## HERMES-FORK-015: Windows search pattern transport

Regex and glob arguments use pattern quoting, while filesystem paths keep native
path translation. This prevents backslashes such as \d and \( becoming slashes.
Literal backslash-n remains distinct from a regex newline; multiline parsing is
selected when ripgrep requires it, without a false newline-intent advisory.

Touchpoints: `tools/file_operations_search.py` (glob/`-name`/`--include` and
exclusion-glob args use `_escape_shell_arg(..., translate_path=False)`;
`_pattern_needs_multiline`; the multiline note only when `_pattern_has_regex_newline`).
Regex patterns already use `translate_path=False` upstream (v0.23.2), so no
`file_operations.py` change or new quoting helper is carried. Search-command bytes
only; no model-facing schema, prompt or history change.
Approval: Elliot granted a change-specific manual override on 2026-11-17 (T3
session) to re-port this settled behavior onto v0.23.2 after a per-intent
assessment of model-facing effect, failure mode and rollback. Residual risk: a
literal backslash-n pattern now runs under `--multiline`, which changes match
spans for such patterns only. Rollback: revert this commit.
v0.23.2 verification: `test_search_pattern_backslash_windows.py` (6 passed) plus
auto_multiline, error_guard, pattern_arguments, zero_match_and_multipath,
native_rg, macos_protected_search, engine_selection and hidden_dirs suites pass;
the one failure (`test_search_error_guard` grep-fallback hard error) is
pre-existing on upstream ae2a81ccc0.

## HERMES-FORK-007: Provider duration metadata for quota footers

`AccountUsageWindow.duration_seconds` carries the provider-reported window length:
Codex `limit_window_seconds` and Anthropic's named account-wide five_hour/seven_day
windows (18,000/604,800 seconds). Upstream labels (Session/Weekly/"Current session")
are unchanged; Opus/Sonnet `scope=model` windows stay distinct with no duration, so
they cannot masquerade as account-wide quota. Absent/non-numeric durations stay None.
Touchpoints: `agent/account_usage.py` (`_usage_windows`, `_window_duration`).
No provider request or authentication path changes. Note: the workspace footer plugin
currently classifies from the raw wham/usage payload's `limit_window_seconds` itself
and does not read this field; it is available for consumers of the snapshot.
v0.23.2 verification: 26 passed across the three account_usage suites.

## HERMES-FORK-017: One Discord progress message

Opt-in display.platforms.discord.progress_compositor: single_message (config.yaml only,
Discord only; a global value never replaces Slack cards or other transports) shows each
turn's progress in one owned temporary message. It is posted concurrently with the turn
(never gating the worker or pre-agent compression) and edited in place. The top header
refreshes every minute with elapsed minutes, iterations, action and available context
usage. Percentage is last_total_tokens / threshold_tokens read from the live compressor,
the same effective compaction trigger the 005 delivery footer uses. Missing usage is
omitted; missing trigger omits percentage; values above 100% stay visible.
Canonical activity is append-only: repeated tools, interim output, compression
start/completion/recovery, and accepted steering stay chronological. Fitting is pure:
render the newest contiguous tail, report hidden tool/message counts, clip oversized
entries without rewriting history. Tool previews retain command/code arguments and
file/URL targets; embedded line breaks become spaces; only an explicit
tool_preview_length caps a preview. Shared Discord limits, secret redaction, bounded
transport, typed edit-failure backoff and one-message ownership remain.

Single-message progress is temporary regardless of cleanup_progress. Ordinary turns delete
it only after confirmed terminal delivery (a delivered error reply counts); refused or
empty delivery and interrupted/cancelled/incomplete turns keep it. Scheduled heartbeats
show normal progress by default (display.heartbeat_progress, normal per-platform
overrides) and delete their own progress on completion, including intentional silence;
false hides execution and pre-agent compression progress, including hold-expiry notices,
without hiding actionable failures. Each turn deletes only its own ID: two attempts per
ID inside one 25-second budget, ID dropped only on transport-confirmed deletion,
exhaustion retains the message (no sweeper or retry service). A predecessor's compositor
is closed before a queued successor starts and is never lent to it. Other transports
retain native rendering. This is presentation-only: progress metadata is
nonconversational and no model input, history, cache, admission, FIFO, interrupt or
compression policy changes. Delivery confirmation reaches cleanup through a ContextVar
set around the unchanged callback firing; no callback fires, orders or times differently.

Touchpoints: gateway/progress_compositor.py (new); gateway/run_turn_progress.py
(start_progress_compositor, _composed_status, _send_composed_progress, tool-line,
status-callback and emit branches); gateway/run_turn_runner.py (interim commentary joins
the log; no stream consumer for commentary alone); gateway/run_turn.py (display mode,
cleanup flag, concurrent start, heartbeat gating/cleanup, cap and queued registration,
predecessor close); gateway/run_turn_bubble_cleanup.py (composed delete with retry,
register gating); gateway/run_turn_hygiene.py (pre-agent compression breadcrumb, wait
updater, hold-expiry; explicit HygieneTurnHoldExceeded re-raise because awaits leave no
caller exception frame); gateway/platforms/base.py + base_post_delivery.py
(delivery_confirmed ContextVar); gateway/run_busy.py + run_inbound.py (accepted-steer
progress line); gateway/session_state.py, turn_context.py, run.py, display_config.py,
hermes_cli/config_defaults.py; plugins/platforms/discord/adapter.py (typed edit failure
results: retryable/retry_after/error_kind).

Not ported on v0.23.2 (turn lifecycle, not display; each needs a change-specific
override): the exact-callback-owner mechanism (owner= on register/pop/fire, the
_post_delivery_owner ContextVar, ownerless-slot isolation, stale-result slot rules);
suppressing sibling post-delivery callbacks (background-review release, goals notices)
when delivery is refused; terminal-owned deferred goals/background release in
run_goals.py and run_turn_runner.py; awaiting producer quiescence and predecessor cleanup
before spawning a drain successor beyond upstream's existing bubble-cleanup handoff;
refusing hygiene work for a turn displaced during the initial post (the post is now
concurrent); an awaited acknowledgement before worker start. Adaptations: heartbeat
turns keep upstream's quiet interim-assistant wiring (agent callback attachment is not
display); /steer command acknowledgement text is unchanged upstream text.
Prior authorization (exact input/cleanup ownership override) covered the pieces above,
not this port: https://discord.com/channels/1517646536505557132/1558581315274805291/1558648435417284670.
Heartbeat progress deletion and hold-expiry status edits run detached and bounded, never
awaited on the turn: neither the running-session guard, the next turn's start nor result
delivery waits on Discord transport (the compositor is closed synchronously and the
turn's own IDs are fixed before the task starts). Ordinary-turn cleanup keeps upstream's
post-delivery callback handoff.
Rollback: revert the 017 commit; no stored rows change.
v0.23.2 verification (offline, fake agents/transports, scripts/run_tests.sh -j 1): 49 gateway/config
files, 670 passed; the only 2 failures, tests/hermes_cli/test_config.py EnvWriteDenylist
near-miss writable cases (git_config_parameters, ld_preload), fail identically on
upstream ae2a81ccc0. Plus 7 adjacent Discord/busy/response-metadata files, 67 passed.
Post-transfer fix: `_progress_build_message` and `_run_agent_queued_followup` read the compositor
attributes with `getattr` defaults (off/None), so partial turn contexts (Slack/queued fakes)
stay untouched; test_slack.py and test_discord_triggering_note_persistence.py pass again.
New/ported 017 suites: progress_compositor (38), composed_progress_lifecycle (27),
compression_progress (17), exact_turn_progress_cleanup (9), progress_steer (7),
discord_message_lifecycle (6), display_config (17), run_cleanup_progress (9),
run_progress_topics (54, incl. the 005 footer cases). 005 agreement: tests assert the header
percent equals last_total_tokens/threshold_tokens read from the same compressor
fields as the footer. Python compile and path-scoped whitespace checks pass. No live
Discord, paid calls, push, staging or restart.
Review fixes: a transport timeout on an edit is retried with bounded backoff (editing stops
only after three consecutive timeouts or a non-retryable error). A timed-out initial send is
not cancelled (that orphaned a created message with no id): its single call is left to report,
a late success is adopted as the owned message, and `settle()` waits for it (bounded) so cleanup
learns the id; it is still never repeated. The session authority's drain
(`fire_post_delivery`, `_run_own_post_delivery`) now reports its delivery verdict through
`delivery_confirmed` (False when the reply failed/was refused, the turn was never committed, or
it was cancelled; None when none was owed), so refused delivery keeps progress there as in the
adapter path. Turn-context readers of `progress_compositor_mode` default to "off" when the context has
no such field, so duck-typed contexts in stock tests keep stock (uncomposed) behavior; a timed-out
initial send is only freed by `settle()`, which the pending-send test asserts.
This remains fork-specific progress behavior, not a stock-upstream defect.

## HERMES-FORK-018: Discord receipt acknowledgement and merged-message identity

Every accepted Discord message is acknowledged the moment it arrives, before its
attachments download: ⏳ when its conversation is busy (it queues or merges), 👀 when
idle. A turn marks every message it carries 👀 at start (⏳ removed) and ✅/❌ at its
end. Gated on the existing `reactions` setting. Reaction calls are detached tasks,
serialized per message and bounded (10s), so a Discord 429, failure or hang never
delays admission, the turn or delivery and never raises into them. A message dropped
before it ran (`/stop`, `/new`, held-burst or text-debounce discard, a replaced pending
slot, stale-lock heal, full queue, refused/fenced admission, any exit before dispatch such
as a failed attachment download or mention-only drop, burst commit failure) loses its mark
through `gateway/receipt_drops.py` (events bind their adapter; marks exist only if
reactions were enabled when set, so drop paths read no config). A message folded into an
already running turn (steer) switches ⏳ to 👀 at once. Marks not recorded in memory
(restart, 1024-cap eviction, a timed-out call) get an idempotent ⏳ removal at start and end.
Graceful shutdown waits (5s) for pending reactions before cancelling tasks. The recovery
column `emoji_ack` now means "the 👀 was scheduled" (the call is detached; its result is
unknown).

`MessageEvent.absorbed_message_ids` (ordered, deduped, head included, set only when
more than one) is appended by `absorb_turn_input`, so every merge site carries it, and
is persisted in the durable ingress envelope only when more than one (single messages
keep their fingerprint; old rows load with none). Discord replies anchor to the latest
id unless `reply_anchor_override` exists. The model-facing triggering line names the
latest id and, only for merged turns, appends "(merged N messages)". Recovery rows of
every absorbed id are marked with the turn's outcome. Stable's configurable busy
debounce is unchanged; the fixed 30s composition window, `discord_composition.py` and
the post-window ⏳ are not ported (see PATCH-ARCHIVE.md).

Approval: Elliot, 2026-11-17, T3 session, change-specific manual override after
assessment (protected: merged-message identity and the model-facing triggering line).
Old part-B (truthful admission, restart drain, review gaps) is not ported: v0.23.2's
durable authority FIFO already provides acceptance receipts, queue-full refusal and
restart persistence; nothing turn-control was left missing.

Residual risk: reaction 429s are cosmetic (marks may lag or be missing). The
triggering line changes model-facing bytes once per merged turn. Still uncovered: shutdown
clears of the pending slot (`run_shutdown`, adapter `cancel_background_tasks`) keep ⏳ on
purpose, since those inputs are flushed to disk and recovered after restart, where the
recovered turn clears it; runner-level unauthorized refusals after dispatch (Discord
pre-filters; an idle one ends ✅/❌ through the processing hooks). A mark of a message that is
never processed stays until the 1024-entry cap evicts it. A paused-FIFO row stays ⏳ by design. Upstream
burst merging needs the same kind, so a standalone TEXT followed by PHOTO messages
still runs as separate turns (unchanged, turn-control). Restored events also issue one
best-effort ⏳ removal. Each mark keeps its target (the live message, else the channel to build a
partial message in: the parent channel for an auto-thread starter), captured before a clear drops the
state; an event's `raw_message` is used only for its own id, never for a merged-in message.
Recovery claim for merged ids: processing start upserts a `processing` row for every id the
turn carries (the head's channel/thread/parent columns, or the restored source's), and a steer
folded into a running turn (`receipt_drops.join_event` -> adapter `on_event_joined_turn`, written
off-loop) does the same, so the reconnect scan skips B and C while the head's claim is live instead
of dispatching them ahead of it. Every claim and completion write is one monotonic upsert over every carried id, head
included (a restored envelope has no live message, so its source supplies the columns); status
only moves processing, then failed/cancelled, then responded, so a steer's delayed claim that lands
after the turn completed cannot undo it. The reconnect scan also skips ids owned by an unresolved
durable envelope (queued, started or unknown, absorbed ids included; one profile-scoped read per
scan, `receipt_drops.pending_envelope_message_ids`): the FIFO runs them, so a second admission would
run the merged input and then its member again. This closes an inherited upstream gap (the same
duplicate existed for any queued message). Residual: the scan snapshot is read once, so an
envelope admitted after it started is covered only by the dedup/claim guards already in place.
Rollback: revert the 018 commit. Fork-specific; no upstream report.

Touchpoints: gateway/platforms/event.py, platforms/base.py (anchor, legacy debounce),
session_envelope.py, session_ingress.py, session_ingress_burst.py, run_inbound.py,
receipt_drops.py (new), hermes_state_runtime.py (pending_native_admissions), plugins/platforms/discord/adapter_reactions.py, adapter.py,
tui_gateway/session_history.py and apps/desktop hydration.ts (legacy-note heal regex
accepts the merge count; TS unverified, no node_modules in the candidate).

Verification (v0.23.2 candidate, scripts/run_tests.sh -j 1): new
test_discord_receipt_reactions.py 12 passed (7 original failed on unchanged ae2a81ccc0; the
5 added for the drop, dispatch-guard, shutdown, unrecorded-mark and steer-join fixes failed
before them), test_discord_reactions.py 4 passed. 138 adjacent busy/burst/ingress/authority/
queue/discord/017/005 files (run in two chunks; Windows 0xc0000142 child crashes are
environmental): 1270 passed, 28 failed. All 28 fail identically on ae2a81ccc0
(feishu 24, serial-reader 2, attachment-receipts 1, session_authority 1). The tui server file
times out at the runner's 300s here; its 16 triggering-note/history tests pass (24 base-failing
tests there are pre-existing). No live Discord, push, staging or restart.

## HERMES-FORK-021: Scoped Discord session messages

An owned Discord turn can send_session_message to an existing conversation of
the same user/profile. Reuse native route resolution, adapter admission and busy
FIFO. Process-local capability supplies identity, generation and shared chain
budgets; tool arguments cannot forge them. Existing routes only, no direct history
writes or bot self-message bypass. Peer content cannot authorize gateway control.
Other transports and delegated/background execution receive no capability.

v0.23.2 transfer: approval Elliot granted a change-specific manual override on
2026-11-17 (T3 session) for re-porting this settled behavior after a per-intent
assessment of model-facing effect, failure mode and rollback; scope is the settled
intent only. Model-facing effect: one `send_session_message` schema in the named
`session_messaging` toolset (never `_HERMES_CORE_TOOLS`), present only on an owned
Discord turn (`GatewayTurnMixin._with_session_messaging` after display settings, on
the owning task; `_resolve_turn_toolsets` strips it from config/background/cold-review
runs; `agent.disabled_toolsets` is the rollback switch), plus the peer text as an
internal, non-control user-role input. Touchpoints: `gateway/session_messaging.py`,
`tools/session_message_tool.py`, `toolsets.py`, `gateway/run_turn.py` (`_run_agent`
binds the capability inside the profile scope; `_with_session_messaging`),
`gateway/session_automation.py` (`admit_route_wake(identity=)`, `session-message` added
to `_ROUTE_WAKE_PRODUCERS` so queued wakes follow a route rotation).
Adaptations: delivery commits a route wake into the destination's durable authority
FIFO (`admit_route_wake`, as kanban/loop wakes do) and falls back to one
`admit_internal_event` through the destination adapter when the target has no committed
conversation; the old private busy-queue branch and the `run_inbound` busy guard are
gone (v0.23.2 already queues internal events without steering). Internal events never
use `session_ingress.admit_message` (`_hm_authority_admission` excludes them), and a
durable wake cannot carry a runtime object, so the shared chain budget is found again by
the delivery's message id in a bounded (512) per-messenger record; an evicted or
restart-lost record fails closed (a reply gets no budget). `send` binds the sender's
profile scope on the event loop (tool workers hop threads), and the destination must
share profile and user. Residual risk: a durable wake runs as the destination's
committed human source (peer text is labelled but not a distinct provenance), adding the
schema changes the tool set of every Discord session, and a restart drops reply budgets.
Rollback: revert the 021 commit (disable via `agent.disabled_toolsets` first).
v0.23.2 verification: 11 passed in tests/gateway/test_session_messaging.py (unit,
schema scope, real GatewayRunner + DiscordAdapter + authority A->B: idle start, busy
durable FIFO order, budget by id, cross-profile/user refusal) and
tests/agent/test_session_message_payload.py (offline wire: role, resume, stable tools,
cache on/off); 13-file adjacent run (registry, toolsets, tools config, wake/automation,
kanban wake) and 9-file turn/busy run passed; the 2 failures in
tests/gateway/test_authority_automation.py also fail on upstream ae2a81ccc0
(WinError 10106 in a clean-env subprocess), not attributed to this change.

## HERMES-FORK-002: Discord personal drafts

Ignore explicit draft/drafts markers (optional slash; whitespace or colon boundary)
before dispatch, history backfill and reply previews. Unrelated words remain input.
Fixed settled behavior only: no configurable markers.
Touchpoints: `plugins/platforms/discord/adapter_drafts.py` (`is_discord_draft_message`);
`adapter.py` `_handle_message` start (live and recovered backfill both pass through it),
`_fetch_channel_context._keep` and the reply-preview read. Drops inbound text only; no
schema, stored history, prompt or cache change.
Approval (protected inbound path): Elliot granted a change-specific manual override on
2026-11-17 (T3 session) for re-porting this settled behavior onto v0.23.2 after a
per-intent assessment of model-facing effect, failure mode and rollback.
Residual risk: a message intentionally starting with the word "draft" is never seen.
Rollback: revert the 002 commit.
v0.23.2 verification: test_discord_free_response.py and test_discord_connect.py, 65
passed, 0 failed, exit 0 (live and recovered dispatch, backfill, reply preview, markers).

## HERMES-FORK-004: Discord auto-thread rate limits

Confirmed thread-create `429` records the parent channel retry deadline and skips
seed fallback/retry. A fallback `429` removes its false seed when one exists.
Ordinary transient retries and configured thread settings remain native.
While a channel cools down, later mentions are skipped silently: at most one
`auto_create_failed` notice per cooldown window per parent channel (the old fork
posted one per mention; extra traffic only extends the 429). State lives in
`plugins/platforms/discord/adapter_thread_cooldown.py` (`AutoThreadCooldown`), not the
adapter facade. Verification: test_discord_channel_controls.py (10 passed) incl. the
one-notice-per-window test.

## HERMES-FORK-012: Semantic titles and Discord rename

Concise Title Case automatic titles use the
original opener (up to `5000` characters), excluding bound skill bodies. Bare /rename
regenerates from up to `10000` characters of clean history; explicit /rename sets the
thread and session title, including during an active turn, with actionable errors.
Opt-in discord.rename_manual_threads also titles user-created threads; fresh
Discord-name comparison preserves a human rename made during title generation.
Main input/history, provider routing and cached system/tool inputs remain native.

Rename transaction correction: native `/rename` owns a thread from admission through
confirmation and conditional persistence; duplicates report pending, never DB-only
"already titled". Automatic native renames share that admission. Explicit names
are validated unchanged against Discord's documented 1–100 character limit before
mutation (https://docs.discord.com/developers/resources/channel). Fresh GET/PATCH/GET
confirms the visible name before title/provenance CAS; native uniqueness, Bot Chat,
lineage and ended-session constraints stay enforced. A newer manual decision wins.

Rename-local single-attempt HTTP uses connected native auth/proxy/user-agent and
known native route/global cooldown state, with one 15-second transport budget that covers
the channel lookup (`fetch_channel` on a cache miss) and the edit together, so a stalled lookup
cannot hold the rename slot; a failure before the edit is sent (lookup timeout, guard) is a plain
`RenameLookupFailed`/original error, not an uncertain edit. `429`
returns retry-after immediately and retains a cooldown, never an edit retry; no
shared max_ratelimit_timeout policy mutation. Cancellation closes transport and
retains command ownership through settlement of any non-cancellable SQLite worker.
Timeout/5xx/read-back failure after sending explicitly report an uncertain visible
outcome, not a fabricated rollback. A confirmed visible change whose DB commit
fails is reported separately. Unsupported relay adapters refuse `/rename` safely
until they offer fresh-name confirmation; relay auto-title paths remain untouched.

Protected override: Elliot's rename-only approval:
https://discord.com/channels/1517646536505557132/1558651185500782623/1558652307569119293.
Changes: slash_commands_session.py, hermes_state_titles.py, Discord adapter and
thread_rename.py; tests cover actual native imports, synthetic HTTP, real SQLite,
pending/manual/boundary/cancellation, exact punctuation and lengths, rate limits,
rejections and uncertainty, plus adjacent title/cache/auxiliary-wire invariants.
No schemas, stored model messages/system prompt, provider policy, session routing,
or subagent ownership repair. No migration, production DB write, manufactured
Discord traffic or gateway restart. Discord has no atomic name compare-and-edit;
a human change between the final read and PATCH remains possible, as does external
state movement after read-back. Atomic CAS preserves newer saved decisions but
cannot prove nondeterministic model behavior. Rollback: scoped revert before deployment.
Verification: 153 tests passed across 11 named files with isolated dev environment,
canonical runner `-j 1` and retries disabled; syntax and scoped diff checks passed.
Parallel files with an explicitly shared pytest basetemp collided on Windows;
serial per-file verification avoids that harness collision without source changes.
Upstream disposition: fork-only rename transaction correction; session-ownership
issue is intentionally excluded.

v0.23.2 transfer: approval Elliot granted a change-specific manual override on
2026-11-17 (T3 session) for re-porting these settled behaviors after a per-intent
assessment of model-facing effect, failure mode and rollback; scope is the settled
intent only. Title work is auxiliary-call only: main input/history, tool schemas,
provider routing and cached prompts are unchanged. Re-implemented at current
locations: `agent/title_generator.py` (opener limit `5000`, Title Case prompt,
`generate_regenerated_title`/`format_regenerated_title_context`, `10000` history
limit), `hermes_cli/commands.py` (`/rename`, `gateway_only`, `busy_policy="dispatch"`),
`gateway/run_busy.py` (`_PLAIN_COMMANDS`), `gateway/slash_commands_session.py`
(`_handle_rename_command` + ack/CAS transaction), `hermes_state_titles.py`
(`validate_session_title`, `set_session_title_if_current` via the shared
`_set_session_title_in_transaction`; `_resolve_title_conflict(dry_run=)`),
`plugins/platforms/discord/thread_rename.py` (new) and adapter (`rename_thread`
routed through `rename_once` inside upstream's `SemanticThreadRenames.attempt`,
`claim/release_thread_rename`, `get_thread_name`, `rename_manual_threads`),
`gateway/run_topics.py` + `run_turn_progress.py` (manual-thread lane),
`hermes_cli/commands_platforms.py` (`rename` demoted to `/hermes rename` on Slack to
protect Telegram parity), locales (`platform.discord.command.rename.*`, English text).
Adaptations: v0.23.2 already carries the original-opener title input
(`title_user_message`, tests/gateway/test_auto_skill_title_input.py), so the old
`agent/turn_context.py`/`run_turn.py` display_metadata hunk is NOT carried (it changed
no main-model bytes; skipping it avoids persisting extra history metadata). Consequence:
`/rename` regeneration omits an auto-skill-bound opening section rather than guessing
(existing scaffold heuristic). `rename_manual_threads` is config-only; the old
`DISCORD_RENAME_MANUAL_THREADS` env fallback is dropped (non-secret config belongs in
config.yaml). Residual risk: as above, plus the adjacent-test fixtures that faked
`thread.edit` now fake `rename_once`. Rollback: revert the 012 commit.
v0.23.2 verification: 277 passed, 0 failed across the 10 rename/title files
(rename transaction, thread rename, rename command, title generator, regenerated
input, auto-skill input, lane, slash commands, pin, config); 503 passed, 3 skipped
across 12 adjacent files (i18n parity, commands, busy policy, state titles, discord
connect/free-response); 285 passed across slash registry/parity/Slack (exit 0).
Post-transfer fix: `test_discord_auto_thread_prompt_parity.py` now fakes `rename_once` like its
sibling pin test (its fake client had no `http`); 459 passed, 1 skipped across the 16-file
017/018/012 adjacent set.

Round-4 correction (upstream e2e `test_discord_thread_rename_ordering.py`): the rename no
longer bypasses discord.py. `rename_once` guards on the connected client's channel, sends through
that channel's own `edit` (native auth/proxy and shared route/global rate limits) and confirms
the applied name from Discord's PATCH reply; `read_thread_name` (used by `/rename`) does the
fresh lookup via `HTTPClient.get_channel`. `_NativeLimits` and every private discord.py access
are gone, so the round-3 fallback question is moot. Behavior change: no separate pre-PATCH GET
inside `rename_once` or post-PATCH read-back GET (`/rename` still reads fresh first), and a
native 429 with a short retry_after may be waited out by discord.py inside the 15-second budget
rather than returned instantly; over-limit waits raise `RateLimited` and are retained as cooldowns.
A timeout during such a wait reports an uncertain outcome. The upstream test is unchanged.

Always-on rename acknowledgement (scoped approval
https://discord.com/channels/1517646536505557132/1558660005300019211/1558937879563210804):
an admitted native thread rename reads the current visible name and sends a
nonconversational acknowledgement before validation, database preparation or
auxiliary generation. Show current title and either requested title/count or
`Generating new title…`; the normal returned reply reports success or failure.
Overlength requests show exactly the first 100 code points with a marked cutoff
and full count, without truncating executable input. Failed acknowledgement
refuses mutation. Existing context/adapter/pending refusal gates stay intact;
unavailable current-name reads report their error rather than invent a title.
No rename retries, stored history, title-generation inputs or persistence/CAS
policy change. Residual protected timing risk: acknowledgement adds an async
boundary while ownership remains held; fresh adapter compare-and-edit/CAS gates
still reject newer decisions. Rollback the scoped source/test/contract commit.
Verification: 19 passes in four named rename/title suites, retries off, isolated
home/env/temp, including real SQLite unchanged-history/refusal/CAS invariants,
ack-before-generation/validation, Unicode100 cutoff and delivery-refusal tests.
Syntax and diff checks pass; no live Discord test, activation or restart.

## HERMES-FORK-013: Seven-day manual threads

Manual /thread and Discord-tool creation default to `10080` minutes. Auto and
handoff threads use `discord.auto_thread_archive_duration` (config.yaml, bridged into
adapter `extra` by `_apply_yaml_config`), validated against Discord's 60/1440/4320/10080
and falling back to `10080` when unset/invalid. Preserve shared public-thread creation
and requester membership.
Touchpoints: `plugins/platforms/discord/adapter.py` (`DEFAULT_THREAD_ARCHIVE_MINUTES`,
`_auto_thread_archive_duration`, slash/`_create_thread` defaults, auto and handoff
creation), `tools/discord_tool.py` (create_thread default and schema description).
Documented in `website/docs/user-guide/messaging/discord.md` (`discord.auto_thread_archive_duration`).
Protected-boundary approval: Elliot, T3 session, 2026-11-17, change-specific manual
override (tool schema description/default 1440 -> 10080, config reader, defaults).
Residual risk: the discord tool description text changes the cached prefix once at
activation, and the model may now pick 10080 by default. Rollback: revert this commit.
v0.23.2 verification: 90 passed, 0 failed across the five Discord slash/tool/yaml/
auto-thread suites (serial per-file runner); the 1440 literals were at adapter
4468/4753/5283/5384/5396/5504/5517 and discord_tool 379/520/610.

## HERMES-FORK-003: Convergent Discord command synchronization

Native safe synchronization treats unspecified installation types as Discord-owned
application defaults. An API-populated default must not trigger delete/recreate.
Explicit installation types, contexts and other managed fields remain compared.
Native fingerprint persistence, pacing, `429` cooldown and timeout policy stay intact.

## HERMES-FORK-026: Runtime work notifications at the active parent boundary

Work completions/progress enter the running parent's existing steer
buffer and its post-tool/pre-request drains; idle keeps the native wake path.
No event bus, second inbox, child fanout, tool schema or system-prompt changes.
The shared gateway notification admission stamps process-local provenance; plain
internal flags/metadata cannot promote peers, plugin requests or handoffs. CLI's
active monitor and TUI/Desktop's owner-checked poller use the same parent helper.
API-server delegation remains a durable delivery with client-owned next turn.

Typed mixed-buffer parts preserve order and distinct user/runtime markers. Runtime
payloads never acquire OUT-OF-BAND USER authority (lookalike openers are quoted).
Runtime-only rows are internal_notification, not compression's human-intent anchor;
append-only persistence and cached prefixes stay native. No backwards insertion
into an already-sent prefix. Finalizer admission closes atomically with its drain;
late events retry/wake normally. Gateway mixed leftover handoffs retain typed human
markers/display kind and a separate FIFO position; runtime-only handoffs use shared
active admission before the existing FIFO fallback, never borrowing human authority.
Human FIFO, Discord's busy-message debounce, explicit approvals/control, peer
assignments, claims/dedup and owner/continuation lookup remain existing mechanisms.
Close admission on exceptional exits while retaining accepted work. Promote
startup-pending notifications when the parent is ready; refund refused claims.
Native Codex app-server retains its existing wake policy. Do not cancel requests
or force tool yields. Explicit hard-stop cancellation keeps the native clear policy.

## HERMES-FORK-027: Compaction recovery and canonical usage continuity

Usage anchors fingerprint the canonical content selected by the existing
substitute_api_content wire helper (valid nonempty string sidecar on user/assistant,
otherwise content), not both display and wire copies. Clean finalization/SQLite
reload no longer invalidates an unchanged request. A full priced-prefix digest
also rejects earlier rewrites/reordering even when the last priced row survives.
Persist/restore retain that digest; legacy last-row-only anchors fail closed until
the next real provider reading. No provider payload, cache key, schema, role/order,
clean display storage, transcript migration or old-history rewrite is introduced.

Dropped on v0.23.2: the earlier pruning-only rearm (delta b: `_finish_compaction_boundary`
`elif` plus `pruning_backoff`). It is unreachable for any in-tree engine: stock `compress()`
returns the unpruned list and `_candidate_rejected` returns first. Dead code without E2E
proof is not wired in; reopen if a context engine that prunes without progress is adopted.

Touchpoints: `agent/usage_anchor.py` (`message_fingerprint` projects a copy through
`substitute_api_content`; `api_content` dropped from `_FINGERPRINT_KEYS`).
Upstream v0.23.2 already carries the priced-prefix digest (`base_prefix_fp`,
`_priced_prefix_fingerprint`) and persistence, so only the fingerprint delta is ported.
Approval: Elliot granted a change-specific manual override on 2026-11-17 (T3
session) to re-port this settled behavior onto v0.23.2 after a per-intent
assessment of model-facing effect, failure mode and rollback. Residual risk: the
fingerprint change only affects local token accounting and alters no provider payload
bytes. Rollback: revert this commit.
v0.23.2 verification: test_compaction_recovery_anchors (14), test_usage_anchor, anti_thrash,
infinite_compaction_loop, turn_context_compaction, split_turn, mid_turn_compaction: 70 passed
in total, 0 failed.

## HERMES-FORK-028: Inert pinned staging and explicit user activation

`hermes update --stage SHA --branch live` prepares exact published source and a
separate relocatable environment without changing active HEAD/refs/dependencies,
profile configuration, PM install state or gateway lifecycle. Reuse native PM
(`pm.build_environment`: pinned toolchain, locked `all` extra, `no_install_project`;
Hermes forbids non-Nix wheels), profile update lock, receipts, holder classification
and rename retry. A relative checkout `.pth` survives whole-tree promotion.

v0.23.2 shape (supersedes the managed-uv/console-stub design): module
`hermes_cli/update_pin_stage.py` plus sibling `update_pin_stage_env.py`. The name
`update_stage` is upstream's desktop progress helper and is never modified or
shadowed. PM keys dependency state by checkout path, so no generation can be built
at the candidate path and moved: activation commits the PM generation and
launchers at the final path in the promoted interpreter (`pm.sync_venv`,
generation collection, `publish_launchers`) as the last step before launch; a
failed sync commits nothing and rolls the trees back. A pre-PM install's first
generation carries the retained previous tree's extras; afterwards PM's ledger owns
them. Plugin dependencies are PM's discovered selection; a conflict fails the sync
(no plugin is disabled) and is only seen at activation, not staging. The marker is
`.<install>.pin-stage.json` (schema 1, same fields as before): the pre-v0.23.2
loaded `/restart` handler reads `.staged-update.json` and would run
`-m hermes_cli.update_stage --activate-stage`, which upstream's helper ignores,
stranding a drained gateway. The legacy name is still read, so an old-staged pin
activates. Old loaded code cannot stage this lock anyway (its Python 3.11 `uv sync
--locked` is refused by the 3.14-only lock), so a candidate is staged by running
`-m hermes_cli.update_pin_stage --stage` from a reviewed checkout (see the user guide).

Explicit user `/restart` captures the validated pin and uses native drain/Windows
watcher. Activation refuses changed/dirty revisions or live holders, retains the
previous installation, rolls back promotion/import/dependency-publication failures and starts the fresh
native gateway. All post-promotion readiness/import work runs in the fresh promoted
interpreter, polling exact live PID/code_sha rather than trusting native CLI exit
status or stale status. Receipt dependencies load before relocation; stage/activate
steps name previous_sha/target_sha explicitly. Promotion/probe failures roll back;
after any launch attempt never rename or blindly retry a possibly booting generation.
Remove the consumed marker before launch; a failed start retains both generations,
writes a failed receipt and requires manual native-start/recovery inspection.
Never fetch moving live
at activation. Prepare-only `/update` does not arm install/watch markers or grant
restart permission; `/update stable` alone selects stable rebase preparation.


Windows standalone single-profile detached owner only; unsupported ownership fails.
No UI builds, fleets, undeclared dependency preservation, runtime Python replacement
or state migrations. Keep failed candidates and previous installations. A host crash
between tree renames still needs retained-tree recovery, not a new journal.
Staged/published is not installed or running; activation is user-owned.

Restart preflight correction: supervisor detection for the current PID reads the
native launch context, never blocking IPC back to its own event loop. Candidate
validation runs in `asyncio.to_thread`; restart admission is rechecked after that
yield. Existing explicit-user authorization, candidate refusal, native drain,
watcher and immutable pin remain intact. No control-client transport rewrite.
Authorization: Elliot's change-specific `allow`, following the self-query deadlock
assessment, https://discord.com/channels/1517646536505557132/1558660005300019211/1558929874331111561.
Residual protected risk: validation now yields while state can change; admission
recheck and native activation validation constrain races, not all concurrency.
Rollback: revert these scoped source/tests/contract edits before deployment;
retain both generations after launch rather than renaming a possibly live tree.
Verification: five gateway files passed 71 tests including three new regressions:
loop responsiveness/refusal, restart admission after validation yields, and an
actual disposable Windows named-pipe round trip plus no-self-query owner check.
Native disposable staging/promotion/rollback suite passed all six with explicit
worktree `UV_PYTHON` (the canonical runner cleans that variable and its fixture
initially produced three WinError448 interpreter-discovery errors, not passes).
External-supervisor/control suites passed seven, skipped sixteen platform tests;
skips are not Windows coverage. Syntax/scoped whitespace checks passed. No live
restart, promotion, paid request, model payload/history or runtime mutation.
A corrected candidate cannot change already-loaded preflight code; user-operated
native stop then pinned candidate activation is a recovery route, not a repeated
first-install bootstrap. Future loaded `/restart` uses the corrected path.

Review fixes. Every activation outcome that leaves the old install in place ends with its
gateway running (`update_pin_stage_recovery.recover_gateway`): this includes failing to take the
update lock (a concurrent updater) and the watcher firing at its drain deadline while the gateway
is still exiting (activation then waits up to 60 s for it to leave, else the gateway would never be
restarted). Once `activate()` returns the tree is promoted: the marker is consumed best-effort
(missing/locked tolerated) and a failure before launch never "recovers" the old tree; a marker
that outlives its candidate directory is removed by `/restart` with an explicit message, never a
permanent refusal. If `pm.sync_venv` committed and a later publication step failed (exit 75), the
rollback re-syncs PM to the restored previous tree from the candidate's interpreter and fails
truthfully (naming `hermes update`) when that cannot be done. Staging re-declares the installed
tree's other git remotes (e.g. `upstream`, push URLs included) in the candidate, read-only on the
install. Promotion replaces the install with a fresh single-branch clone, so local stashes, other
local branches and ignored build outputs of the old tree remain only in `.previous-<token>`.

Touchpoints: hermes_cli/update_pin_stage.py + update_pin_stage_env.py + update_pin_stage_recovery.py, hermes_cli/main.py
(`cmd_update` hook), hermes_cli/subcommands/update.py (`--stage`), update_cmd_windows.py
(`project_root` scan arguments), gateway_supervised_restart.py (own-PID preflight),
gateway/slash_commands.py (`/restart`), gateway/run_shutdown.py + run_shutdown_staged.py
(watcher), website/docs/user-guide/staged-fork-updates.md.

v0.23.2 re-port. Authorization: Elliot's change-specific manual override, granted
2026-11-18 (T3 session) after the re-port assessment, to carry the settled 001/016/028
behavior onto stable v0.23.2. Residual protected risk: the restart/activation path
now also depends on PM's explicit sync and launcher publication running at the final
path under an update lock (a failed sync commits nothing; an interrupted one is
PM's own recoverable transaction), and a plugin-dependency conflict is found at
activation, not staging. No model-facing payload, history or prompt path is touched.
Rollback: revert the 028 commit before deployment; after activation retain both
generations (`.previous-<token>`) rather than renaming a possibly live tree.
Verification (Windows host, canonical runner): `test_update_pin_stage` 7 passed
(real Git/uv/relocation/promotion from the executing candidate interpreter, rollback
on swap and on dependency-publication failure, marker-name isolation, extras carry),
run under a short scratch root (the fixture moves to the drive root when `tmp_path` is deep,
since the cloned candidate plus venv exceed Windows MAX_PATH under a long TMPDIR),
`test_staged_restart` 5 passed (incl. real named-pipe owner preflight and the
candidate-rooted watcher argv). A real `pm.build_environment` build of a fixture
project (isolated home, real PM toolchain) produced a relocatable environment whose
fresh-interpreter probe imported candidate code. Not exercised: `pm.sync_venv` and
launcher publication on a full Hermes tree (no disposable Hermes install with a PM
store), an end-to-end activation from the candidate venv of a full lock, and live
Discord/gateway restart.

## HERMES-FORK-030: Truthful delegated process completion ownership

Build on upstream PR #132827 pinned at
`c0f3831463acdd5c4b27c19e899f2a008382c8b8`, cherry-picking only its six
intent commits with original authors retained. Shared nonmutating delegate proof
and compression ownership preflight distinguish terminal boundaries from transient
lookup/rotation uncertainty. Suppress child-owned process noise before profile-scoped
batching and watch admission; read live ownership so explicit handoff still delivers.
Real delegation/closeout results are exempt. Preserve 026's active-parent steer
buffer and native idle wake, claims, retries, user boundaries and generation/CAS gates.

The terminal result now reports armed notification intent separately from current
child-policy suppression; it no longer claims notification is disabled while a
watcher is armed. Enabled child policy remains enabled. Child teardown and explicit
handoff rules remain native. A current human route on a former worker is authoritative,
even with malformed historical provenance: own/descendant completions stay there;
parent/sibling completions cannot switch away and end that conversation. No rename,
historical repair, prompt/schema/provider-policy change or production migration.

Authorization (protected ownership/timing and future model-facing result):
https://discord.com/channels/1517646536505557132/1558630048498454599/1558652630803288205.
Risk: legitimate cross-boundary completions are conservatively dropped rather than
rewriting an already-affected route; durable delegation records remain readable.
Deterministic offline evidence cannot prove all model behavior or live transport.
Rollback: revert 030's adaptation and its six attributed prerequisite commits as one
intent; no database rollback/repair. No runtime activation performed.

Touchpoints: gateway/run_notifications.py, tools/process_registry.py,
process_registry_notifications.py, terminal_tool_background.py and gateway/tools tests.
Import conflict adaptation retains the fork's local delivery-target helper.
Additional adaptation protects current former-worker routes and truthful launch receipts.
A current route whose row is missing stays retryable and one with unreadable provenance is
terminal for parent/sibling completions; own/descendant completions still deliver
(the stock relay-followup test helper therefore supplies the chat's current human row)
(completion_current_route_history 17 and async_delegation_session_binding 84 pass).
328 tests passed, zero failed/skipped, exit 0 in 310.0s across 12 named files via
`scripts/run_tests.sh -j 1 --basetemp=<isolated-worktree-scratch>/pytest-final --tb=short`:
async_delegation_session_binding, background_process_notifications, completion_owner_retry,
completion_current_route_history, completion_admission, completion_delivery,
completion_session_boundary, gateway/agent runtime_notification_bridge,
terminal_child_notification_state, terminal_watcher_arming, subagent_process_handoff.
Includes real SQLite route/history/durable dispositions, A/B/A profile policy/live
handoff, registered terminal-result dispatch, and cached/uncached offline provider
requests with prefix/persistence/resume/compaction invariants. Syntax and scoped diff checks
pass. Use serial workers when explicitly forwarding one shared pytest basetemp:
parallel subprocesses otherwise collide on Windows open SQLite fixture directories.
Upstream remains a pinned unmerged salvage; #131597 was not imported.

v0.23.2 transfer: the six salvaged #132827 commits are upstream (dc29b0d2e9, 38c82c81fb,
0b86710cef, a6d1c24946, a481580f25, 9fd8de912a) and are not carried. Only the
fork-only current-route protection and truthful-receipt adaptations remain active.

v0.23.2 re-port (2026-11-17): Elliot granted a change-specific manual override on
2026-11-17 (T3 session) for re-porting this settled behavior; scope is the settled
intent only (commits 496866cebc and ef7f73edd8, nothing else). Touchpoints now:
gateway/run_notifications_ownership.py (`_lookup_completion_owner` refuses to switch
away from a former-worker current route; upstream moved it out of run_notifications.py),
tools/terminal_tool_background.py (`notification_suppressed`, live-policy `subagent_note`,
`heartbeat_ignored`, heartbeat armed only when the policy surfaces), tools/terminal_tool.py
(promoted-note receipt no longer promises delivery under suppression). Residual risk and
rollback as above: revert the 030 commit. v0.23.2 verification: 202 passed, 0 failed,
exit 0 across 11 files (async_delegation_session_binding, background_process_notifications,
completion_owner_retry, completion_current_route_history, completion_admission,
completion_delivery, completion_session_boundary, completion_ownership_proof,
terminal_child_notification_state, terminal_watcher_arming, subagent_process_handoff);
the old gateway/agent runtime_notification_bridge suite does not exist on v0.23.2.

Parent integrated concurrent live schema/progress fixes (intent 029 remains schema;
this intent is 030), then independently verified 356 passing tests across those 12
files plus root_union_schema_visibility and composed_progress_lifecycle (exit 0,
294.8s). Independent review found foreground timeout promotion still promised a
notification while child policy suppressed delivery. The effective registered
terminal dispatch now verifies both explicit-background and foreground-promotion
receipts. Fixed the existing promotion-note consumer, then reran three terminal/
handoff suites: 12 passed, 0 failed, exit 0, 22.6s. No live transport evidence claimed.

## HERMES-FORK-029: Preserve root-union tool fields

Before removing unsupported root `anyOf`/`oneOf`, project inline branch fields
into model-facing arguments. Normalize fragments using the existing model pass
before comparing alternatives, while preserving wire keys. Keep identical fields
once, preserve conflicting schemas as nested `anyOf`, and expose a native type
only when every normalized alternative explicitly agrees. Root properties retain ownership and constraints; only shared
branch requirements become global. Raw MCP/server validation schemas are unchanged.

Touchpoints: `tools/schema_sanitizer.py` and `tools/arg_coercion.py`. Coercion uses
projected wire properties; reversal respects enclosing and supplied keys and each
branch's actual exposed rename map. Native types, const/enum, required keys and
closed objects can disambiguate a rename. Agreed siblings still reverse when
another key is ambiguous; unknown, unmatched, ambiguous or colliding keys remain
unchanged for server validation. Bare-string schema fragments use existing
normalization; boolean/non-dict fragments pass through without crashing.
Projection, model exposure and branch-reversal evidence share bare-string,
type-array, nullable-union and ref-sibling normalization. Agreed types retain
nullable hints; a null discriminator keeps all nullable rename candidates possible.
Root requirements and legacy flags are collected before normalization can prune them.
For native lists, select compatible inline union arms before item descent. Their
item schemas use the existing per-key agreement logic; enclosing items keep
ownership, opaque viable arms block guesses, and supplied wire keys never get
overwritten. No general resolver or new validation layer is introduced.
Unambiguous JSON-container decoding precedes the single key-reversal pass,
using projected model-spelled properties. String-compatible and opaque arms retain
the original string. The later raw type-array pass cannot override that decision;
recursive dispatch normalization uses the same guard. Existing standalone walker
and unrelated scalar coercion remain native; restored wire names are not fed
through a second reverse map.

Authorization: Elliot *** 1558586766108135495, "Your plan sounds good. Execute
on it.", approved the specific raw MCP schema -> sanitizer -> model definitions ->
tool_describe loss assessment and scoped source fix instead of a WHOOP workaround.
Protected impact: model tool-schema bytes and cached definitions change. Projection
is permissive and drops branch correlations; original handlers enforce alternatives.
Deterministic checks cannot prove every model behavior safe.

Root-union branches that are local `$ref`s are resolved against the root schema
(`resolve_local_schema_ref`) before projection, so model exposure and the matching
reversal see the referenced fields; one small regression test covers it (review fix).

Verification: two end-to-end invariant tests (plus that one `$ref`-branch regression)
in `tests/tools/test_root_union_schema_visibility.py`, without parametrization or a
case-matrix loop. The schema-only WHOOP fixture passes through real MCP conversion,
registry, cached/uncached model definitions and actual `tool_describe` dispatch:
all five fields retain their server constraints, and raw server validation still
accepts each valid alternative while rejecting incomplete or mixed arguments.
A small representative raw-registry schema exercises model definitions -> actual
dispatch, unanimous boolean coercion across bare-string/dict and nullable
type-array/dict alternatives, enclosing wire ownership, nullable-const nested
rename evidence, and ambiguous names left unguessed while agreed siblings reverse.
One array-or-string payload contract now verifies sanitized required item fields
reverse through actual dispatch and validate against the original server schema.
The same invariant decodes container-only JSON-encoded objects and arrays back to
validated wire arguments, while mixed array/string, object/string, type-array,
opaque-ref and nested mixed fields retain their accepted strings. No tests,
parametrization or matrices were added.
It bypasses MCP pre-normalization deliberately so nullable-discriminator mismatches
remain observable. Server validation spells out the nonstandard bare-string
boolean solely in its validation copy; the registered schema stays untouched.

Review findings [4239463297](https://github.com/ElliotDrel/hermes-agent/pull/1#discussion_r4239463297)
and [4239463299](https://github.com/ElliotDrel/hermes-agent/pull/1#discussion_r4239463299)
were fetched and reproduced before source edits on `46c6396ed070f3a5be82e9af29b74f62f1cbecef`:
both boolean forms left `"true"` as strings, and nullable-const selection left
`a_b` instead of chosen wire key `a@b`. The same isolated probes now report both
booleans coerced and the selected wire name reversed. Exactly the original two
test functions remain; no parametrization, extra tests or case-matrix loop.

The earlier two-invariant fixture failed on an isolated unchanged git archive of
`eaca181c20a641d27d9132195d7e55fd6632449b` (WHOOP properties empty; projected
confirm_flag absent). Both current invariant tests pass on this branch. The archive received only the
new test file and WHOOP schema fixture; production source was not changed.
Canonical `scripts/run_tests.sh -j 1` acceptance: **259 passed, 0 failed, exit 0**
across `tests/tools/test_root_union_schema_visibility.py`,
`tests/tools/test_schema_sanitizer.py`, `tests/tools/test_schema_boolean_required.py`,
`tests/tools/test_model_tools.py`, `tests/tools/test_mcp_tool.py`,
`tests/tools/test_tool_search.py`, `tests/tools/test_registry.py`, and
`tests/agent/test_tool_arg_coercion.py`. Only the established unchanged-base Windows
exclusions remain: `test_tool_defs_cache_key_sees_config_replacement_with_pinned_mtime`
and `test_windows_location_vars_passed_without_secrets`. No other suite was edited.

**v0.23.2 re-port (2026-11-17).** Approval: Elliot granted a change-specific manual
override on 2026-11-17 (T3 session) to re-port exactly this settled behavior onto
v0.23.2 after a per-intent assessment of model-facing effect, failure mode and rollback.
Residual risk unchanged (model tool-schema bytes for root-union tools; permissive
projection; deterministic tests cannot prove model behavior). Rollback: revert this
commit. Adaptation to upstream drift: `unrename_tool_args(params_schema, args,
root_schema=None)` keeps its `$ref` support (`resolve_local_schema_ref` is applied at
every walk level, to array `items` and to union arms, against the projected root);
`coerce_tool_args` keeps its `$ref`-aware property resolution and `root_schema`
threading. `_preserve_root_union_properties` returns the schema untouched when there
is no non-empty root `anyOf`/`oneOf`, so upstream path refs such as
`#/properties/limit/anyOf/0` still resolve; with a root union, root property fragments
are normalized (a path ref through a root property's own union is the only unresolved
edge). The sanitizer's single-tool path now calls `_normalize_schema_node`
(sanitize + nullable-union collapse + ref-sibling strip) on the projection. The
two-test contract gained one `$ref`'d object property (`ref item`) to prove renamed
keys round-trip through the projection and `$defs`.
v0.23.2 verification: 2 passed (`test_root_union_schema_visibility.py`); 92 passed in
visibility, `test_tool_arg_coercion`, `test_model_tools`, `test_schema_boolean_required`,
`test_schema_sanitizer`, plus 87 passed in `test_tool_search`/`test_registry`, and
`test_mcp_tool` 102 passed. Failures, all identical on unchanged upstream ae2a81ccc0:
`test_tool_defs_cache_key_sees_config_replacement_with_pinned_mtime` and two
`test_mcp_tool` `TestBuildSafeEnv` cases ("Could not determine home directory").
Retries off; worktree-local `.venv`, test home and TMPDIR/TEMP/TMP;
`--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/mcp-schema-visibility/.test-scratch/normalization-acceptance-final`
and `--tb=short`. Python syntax and scoped whitespace checks passed.

Review finding [4239505862](https://github.com/ElliotDrel/hermes-agent/pull/1#discussion_r4239505862)
was fetched and reproduced on `1722d9383d04552a3a96619184a602a93d7bc5c2`:
the extended existing dispatch invariant failed because `payload[0].bad_key`
remained exposed instead of restoring required `bad key` (1 passed, 1 failed,
exit 1). The list path previously read outer `items` before inspecting union arms.
After the scoped repair, the same eight canonical isolated suites passed:
**259 passed, 0 failed, exit 0**, 66.4s; exactly two invariant tests remain.
Same two exclusions, retries off, worktree-local `.venv`/home/TMPDIR/TEMP/TMP,
`--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/mcp-schema-visibility/.test-scratch/array-union-acceptance`
and `--tb=short`. An isolated actual-dispatch probe also verified supplied-key
collisions/unknown keys, string payloads, conflicting array item maps and an
opaque viable arm remain unchanged. Python syntax/scoped whitespace checks passed.
Parent independently reran the combined acceptance: 259 passed, no failures;
syntax and scoped whitespace checks pass. No install/live/lifecycle operations.

Latest finding [4239517814](https://github.com/ElliotDrel/hermes-agent/pull/1#discussion_r4239517814)
was also fetched and reproduced before its repair: with the array-arm fix present,
encoded objects retained nested exposed keys and encoded array-or-string payloads
remained strings (1 passed, 1 failed, exit 1). The mixed array/string decoding
expectation is superseded below: its string arm legitimately accepts that input.
Decoding unambiguous containers before reversal repairs the operation ordering
without double-renaming. That combined acceptance on the
same eight files: **259 passed, 0 failed, exit 0**, 70.2s; same two exclusions and
isolation, retries off, `--tb=short`, and
`--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/mcp-schema-visibility/.test-scratch/container-order-acceptance`.
Exactly the original two invariant functions remain.

Review finding [4239544570](https://github.com/ElliotDrel/hermes-agent/pull/1#discussion_r4239544570)
was fetched and reproduced on `9eae5c59743681265d7b9db1f00845f5fbd562a5` before
production edits: actual dispatch decoded valid JSON-looking mixed/opaque strings
into containers (1 passed, 1 failed, exit 1). The extended representative invariant
now preserves those strings, including nested fields through the later coercion
walk, while native union lists still reverse and container-only encoded object/array
arms decode before reversal and validate against the original schema. The raw
object/string type-array guard also prevents later scalar decoding. Exactly two
invariant functions remain; no general validator or ref resolution was introduced.
Current eight-suite isolated canonical acceptance: **259 passed, 0 failed, exit 0**,
76.0s, same two exclusions, retries off, local `.venv` and test home/TMPDIR/TEMP/TMP,
`--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/mcp-schema-visibility/.test-scratch/string-union-acceptance-final`.
Python syntax, scoped whitespace and AST checks passed (two tests, no decorators).
Parent independently reran all 259 acceptance tests, syntax and whitespace checks;
no live, installation or lifecycle operations were performed.

Limits: no ref resolution/relocation, branch-local definition hoisting, allOf field
extraction or general schema validator. Rename evidence does not evaluate patterns,
ranges or branch correlations and uses native values, not speculative coercion.
Schema-guided JSON-container parsing precedes reversal. Ambiguous renames may fail
server validation rather than guess; mixed/opaque projected types stay uncoerced.
Array-arm selection uses native container type evidence only; it does not evaluate
whole-array constraints/correlations, resolve refs or add tuple/prefixItems support.
An existing item combinator is retained rather than overwritten by an arm combinator.
Container decoding reuses the existing schema walker; no combinator resolver for
deeper JSON-encoded elements/subfields is added. JSON-looking scalar strings are
only decoded in dispatch where the model schema permits the corresponding container
kind and explicit type evidence rules out a string interpretation. Opaque arms
conservatively block decoding; full constraint satisfiability is not evaluated.
Nullable-single-survivor handling and definition-cache policy remain native.

Integration preserves live intent 027; this schema intent is 029; live staging retains 028.
Upstream candidate correctness fix; retain until stable preserves root-union fields.
Rollback: revert this intent-shaped change. Parent independently reproduced both
normalization defects on the previous head, verified the repaired probes, and reran
all 259 acceptance tests. No runtime/profile writes, installation or gateway restart.
Published source is not installed/running behavior; native installation remains
Elliot's action.
