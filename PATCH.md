# Maintained Hermes changes

Base: official stable v2026.9.24, `f97608f178d1ffeca59860195ab7da295f7c8e5f`.
`main` stays exact official stable; `live` adds the intents below. Preserve these behaviors when rebasing.

## HERMES-FORK-023: Native Windows update drain

Windows updates use the normal restart exit budget, including its after-turn wait.
The socket pause is attempted before the planned-stop marker; the marker is only
the fallback when no valid ACK arrives. Otherwise the marker invokes direct stop
and bypasses normal agent draining. The existing drain reporter shows active work
while waiting. Native timeout, forced-stop fallback, install and restart remain native.

## HERMES-FORK-001: Rebase preparation with native installation

Discord `/update` enters the profile's hermes-fork-update skill as the explicit user
turn. Ordinary admission, authorization, session history and cached system inputs
remain intact. Busy sessions reject the command. The CLI `hermes update` stays native.
The skill prepares a separate clone, rebases live onto official stable, resolves
conflicts, audits this file and tests before publishing main/live. A small handoff
calls the existing native detached updater with --branch live --yes. Native Hermes
owns dependencies, migrations, drain, install, restart and its receipt.

The update skill reviews keep/adapt/retire decisions in the current run's thread.
Established preferences remain settled; proposed behavior changes wait for Elliot's
decision. Published live may be ahead of the installed checkout even when stable
has not changed. Preparation accepts that clean ancestor state, while unpublished
or divergent installed commits require review. Installation is skipped only when
installed and prepared revisions match; running revision is verified separately.

The native watcher is armed for this request before preparation. A run ID binds the
handoff to that marker, and a pending run rejects a concurrent request. No custom
installer, dependency repair, health daemon or subprocess supervisor remains.

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

## HERMES-FORK-015: Windows search pattern transport

Regex and glob arguments use pattern quoting, while filesystem paths keep native
path translation. This prevents backslashes such as \d and \( becoming slashes.
Literal backslash-n remains distinct from a regex newline; multiline parsing is
selected when ripgrep requires it, without a false newline-intent advisory.

## HERMES-FORK-022: Restore paired task snapshots across turns

Recognize the registered todo_list name, legacy todo alias and a single-call
native tool_call bridge when restoring task state. Preserve existing call-ID
pairing and user-boundary checks; unrelated, malformed and multi-call results
cannot seed the store. Native tool schemas and cached request inputs stay intact.
The TUI consumes the same name predicate. No new persistence layer is introduced.

## HERMES-FORK-007: Provider duration metadata for quota footers

Expose provider-reported Codex window durations to the existing workspace footer,
label recognized five-hour and weekly windows, and retain unknown fallback labels.
Anthropic's named account-wide five_hour/seven_day windows supply 18,000/604,800
seconds explicitly. Model-specific caps remain distinct and cannot masquerade as
account-wide quota. No provider request or authentication path changes.

## HERMES-FORK-017: One Discord progress message

Opt-in display.platforms.discord.progress_compositor: single_message acknowledges
each turn before execution and edits one owned temporary message. The top header
refreshes every minute with elapsed minutes, iterations, action and available
context usage. Percentage uses last_total_tokens / threshold_tokens.
Missing usage is omitted; missing trigger omits percentage;
valid values above 100% remain visible. No budget or compression-policy change.
Canonical activity is append-only: repeated
tools, interim output, compression start/completion/recovery, and accepted steering
stay chronological. Fitting is pure: render the newest contiguous tail, report
hidden tool/message counts, and clip oversized entries without rewriting history.
Tool previews retain command/code arguments and file/URL targets; only explicit
tool_preview_length settings impose a per-preview cap. Shared Discord limits,
secret redaction, bounded transport, backoff and one-message ownership remain.

Single-message progress is temporary regardless of the legacy cleanup flag.
Ordinary turns delete it only after confirmed terminal delivery, including error
replies; refused/empty delivery and incomplete/cancelled ordinary turns retain it.
Scheduled heartbeats show normal progress by default and delete their own progress
on completion, including intentional silence. display.heartbeat_progress (with
normal per-platform overrides) can hide execution and pre-agent compression
progress, including hold-expiry notices, without hiding actionable failures or
material final results. Close the owned compositor before deletion; do not reuse
closed pre-agent messages across queued successors, even within one generation.
Refused queued delivery must not fire delivery callbacks. Final delivery, queue
admission/grouping, model inputs, history/cache and compression policy stay native;
progress metadata is nonconversational. Other transports retain native rendering.

## HERMES-FORK-018: Fixed Discord composition window

Ordinary busy queue-mode Discord text reserves one FIFO position and groups
compatible same-sender messages for a fixed 30 seconds. Each physical message
is fetched once at seal to include edits; edits cannot become control commands.
The current answer is delivered before waiting for the next composition window.
Media, other senders, explicit commands and other transports stay separate.
Only the final queued message receives the waiting reaction, removed on admission.

## HERMES-FORK-021: Scoped Discord session messages

An owned Discord turn can send_session_message to an existing conversation of
the same user/profile. Reuse native route resolution, adapter admission and busy
FIFO. Process-local capability supplies identity, generation and shared chain
budgets; tool arguments cannot forge them. Existing routes only, no direct history
writes or bot self-message bypass. Peer content cannot authorize gateway control.
Other transports and delegated/background execution receive no capability.

## HERMES-FORK-002: Discord personal drafts

Ignore explicit draft/drafts markers (optional slash; whitespace or colon boundary)
before dispatch, history backfill and reply previews. Unrelated words remain input.

## HERMES-FORK-004: Discord auto-thread rate limits

Confirmed thread-create `429` records the parent channel retry deadline and skips
seed fallback/retry. A fallback `429` removes its false seed when one exists.
Ordinary transient retries and configured thread settings remain native.

## HERMES-FORK-012: Semantic titles and Discord rename

Concise Title Case automatic titles use the
original opener (up to `5000` characters), excluding bound skill bodies. Bare /rename
regenerates from up to `10000` characters of clean history; explicit /rename sets the
thread and session title, including during an active turn, with actionable errors.
Opt-in discord.rename_manual_threads also titles user-created threads; fresh
Discord-name comparison preserves a human rename made during title generation.
Main input/history, provider routing and cached system/tool inputs remain native.

## HERMES-FORK-013: Seven-day manual threads

Manual /thread and Discord-tool creation default to `10080` minutes. Auto, update and
handoff threads use validated discord.auto_thread_archive_duration (native `1440`
when unset/invalid). Preserve shared public-thread creation and requester membership.

## HERMES-FORK-003: Convergent Discord command synchronization

Native safe synchronization treats unspecified installation types as Discord-owned
application defaults. An API-populated default must not trigger delete/recreate.
Explicit installation types, contexts and other managed fields remain compared.
Native fingerprint persistence, pacing, `429` cooldown and timeout policy stay intact.

## HERMES-FORK-024: Keep Windows updater lock checks lightweight

The post-swap updater must inspect its parent's update lock without importing
messaging dependencies it may need to replace. Reuse the existing stdlib-only
recovery PID probe on Windows; retain the zombie-aware gateway probe on POSIX.
The native dependency guard, installer, recovery and restart flow remain intact.

## HERMES-FORK-025: Avoid premature HTTP imports and report failed updates

Construct the existing Codex response-cap stream class inside its response hook.
Provider-registry reads used by updater subprocess environment preparation must
not import httpx and map compression DLLs before replacement. The stream body,
response cap, errors and network requests are unchanged.

Failed optional-backend refreshes retain the native incomplete marker even when
unrelated core probes pass. Both native completion banners check that marker and
return incomplete to the existing restart/receipt flow. Print full installer
errors; do not claim the previous backend survived a failed uninstall. Fleet
verification returns failure for incomplete installation even when restart succeeds.

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
Human FIFO, Discord's 30-second composition, explicit approvals/control, peer
assignments, claims/dedup and owner/continuation lookup remain existing mechanisms.
Close admission on exceptional exits while retaining accepted work. Promote
startup-pending notifications when the parent is ready; refund refused claims.
Native Codex app-server retains its existing wake policy. Do not cancel requests
or force tool yields. Explicit hard-stop cancellation keeps the native clear policy.

## HERMES-FORK-027: Compaction recovery and canonical usage continuity

An accepted pruning-only transcript rewrite now arms the existing next-provider
recovery verdict after commit. Below-threshold provider prompt usage can rearm the
per-pressure-episode attempt budget; equal/above-threshold and missing usage cannot.
No summary success is fabricated: structural backoff and fallback-quality streaks
remain distinct. Preserve the pruning backoff across the native boundary callback's
session-state rebind. Unchanged/equal-copy, aborted, empty, growing/rejected, cancelled,
lock-skipped and failed-commit attempts never arm a new recovery verdict.
Attempt caps, rough-estimate deferral and failure guards remain native. Do not change configured context budgets.

Usage anchors fingerprint the canonical content selected by the existing
substitute_api_content wire helper (valid nonempty string sidecar on user/assistant,
otherwise content), not both display and wire copies. Clean finalization/SQLite
reload no longer invalidates an unchanged request. A full priced-prefix digest
also rejects earlier rewrites/reordering even when the last priced row survives.
Persist/restore retain that digest; legacy last-row-only anchors fail closed until
the next real provider reading. No provider payload, cache key, schema, role/order,
clean display storage, transcript migration or old-history rewrite is introduced.

## HERMES-FORK-028: Preserve root-union tool fields

Before removing unsupported root `anyOf`/`oneOf`, expose inline branch properties
in the model-facing argument object, retain identical fields once, and represent
conflicting field schemas with a nested `anyOf`. Existing root properties remain
constraints, and only requirements shared by every branch become global.

Touchpoints: tools/schema_sanitizer.py and tools/arg_coercion.py; reverse renamed
projected keys and coerce against projected wire properties even without root props.
Nested reversal uses each branch's exposed rename map, not another projection.
Native types, const/enum, required keys and additionalProperties: false can rule
out candidates; otherwise reverse only when all possible mappings agree. Unknown,
ambiguous or unmatched values pass through for original server validation. No ref
resolution or general schema validator is introduced. MCP registry/server validation
schemas remain unchanged. Parent fixed branch-local boolean-required flags and root
key reversal; this follow-up repairs projected coercion and nested branch reversal.
Nullable-single-survivor handling, root-combinator rejection compatibility and
model definition-cache policy remain native.

Integration: merged current live; preserve its compaction intent 027 and assign
this independent schema intent 028. No force push or runtime installation.

Authorization: Elliot *** 1558586766108135495, "Your plan sounds good. Execute
on it.", approved the specific raw MCP schema -> sanitizer -> model definitions ->
tool_describe loss assessment and scoped source fix instead of a WHOOP workaround.
Protected impact: model tool-schema bytes and cached definitions change; this
permissive projection drops branch correlations, so handlers still enforce the
original alternatives and deterministic tests cannot prove every model behavior.

Verification: copied only WHOOP whoop_feedback inputSchema into a schema-only
fixture; real MCP conversion, registry definitions, cached/uncached loading and
actual tool_describe bridge preserve title, description, call_id, confirm and draft_id.
The same two regressions fail on unchanged eaca181c20 with properties: {}; adjacent
cases cover shared/conflicting fields, required intersection, empty/null/opaque
branches, property refs/definitions, root constraints and nested schemas.

Canonical parent acceptance: scripts/run_tests.sh -j 1 with eight named files
(test_root_union_schema_visibility, test_schema_sanitizer, test_schema_boolean_required,
test_model_tools, test_mcp_tool, test_tool_search, test_registry and test_tool_arg_coercion),
277 passed, exit 0. Syntax and scoped whitespace checks passed.
Two unrelated Windows failures reproduced in an unchanged git-archive baseline
and were excluded: test_tool_defs_cache_key_sees_config_replacement_with_pinned_mtime
and test_windows_location_vars_passed_without_secrets; neither was modified.

Review follow-up: findings 4239284359/4239284362 reproduced before source edits
through handle_function_call -> coerce_tool_args -> real registry dispatch: four
RED cases (anyOf/oneOf string boolean unchanged; integer payload mapped to a b).
Root-only projection now supplies wire properties for coercion. Nested reversal
retains each branch's independent collision suffixes and changes keys only when
simple native value/schema evidence leaves agreeing candidates. Tests include the
actual root-projected conflicting payloads, existing nested unions, both branch
orders, arrays, const/enum/required/closed-object evidence, bool versus integer,
nullable ambiguity, opaque refs, identical mappings, unmatched/ambiguous values,
conflicting projected scalar/container types and supplied-key overwrite refusal.
Original schema validation checks the repaired successful dispatch payloads.

Final isolated canonical review acceptance: same eight named suites above,
313 passed, 0 failed, exit 0 (84.5 seconds), including 56 root-union and 19 argument
coercion cases. HERMES_TEST_FILE_RETRIES=0; worktree-local HERMES_HOME/TMPDIR/TEMP/TMP;
--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/mcp-schema-visibility/.test-scratch/review-acceptance-final
--tb=short. The same two established Windows baseline exclusions remain; no new
exclusions or modified baseline tests. Python compile and scoped diff checks pass.
Parent independently reran all 313 cases successfully before integration.

Worktree-local .venv, test home and scratch only; no runtime dependency/profile
writes, model/server calls, installation or gateway restart. Root union references
are not resolved or relocated, branch-local definitions are not hoisted, and allOf
field extraction remains out of scope. Rename evidence is deliberately not full
schema validation (no refs, patterns, ranges, or branch correlations). Ambiguous
renames remain unchanged and can fail server validation rather than guess. Branch
selection uses native values, not speculative scalar coercion; pre-existing JSON
container parsing occurs after reversal and is not a generalized union decoder.
Conflicting projected field schemas without one explicit type remain uncoerced.

Upstream candidate correctness fix; retain until stable preserves root-union fields.
Rollback: revert this intent-shaped commit. Published source is not installed or
running behavior; native installation remains Elliot's action.
