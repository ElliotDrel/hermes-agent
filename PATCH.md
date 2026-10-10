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

Authorization: Elliot *** 1558586766108135495, "Your plan sounds good. Execute
on it.", approved the specific raw MCP schema -> sanitizer -> model definitions ->
tool_describe loss assessment and scoped source fix instead of a WHOOP workaround.
Protected impact: model tool-schema bytes and cached definitions change. Projection
is permissive and drops branch correlations; original handlers enforce alternatives.
Deterministic checks cannot prove every model behavior safe.

Verification: exactly two end-to-end invariant tests in
`tests/tools/test_root_union_schema_visibility.py`, without parametrization or a
case-matrix loop. The schema-only WHOOP fixture passes through real MCP conversion,
registry, cached/uncached model definitions and actual `tool_describe` dispatch:
all five fields retain their server constraints, and raw server validation still
accepts each valid alternative while rejecting incomplete or mixed arguments.
A small representative raw-registry schema exercises model definitions -> actual
dispatch, unanimous boolean coercion across bare-string/dict and nullable
type-array/dict alternatives, enclosing wire ownership, nullable-const nested
rename evidence, and ambiguous names left unguessed while agreed siblings reverse.
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
Retries off; worktree-local `.venv`, test home and TMPDIR/TEMP/TMP;
`--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/mcp-schema-visibility/.test-scratch/normalization-acceptance-final`
and `--tb=short`. Python syntax and scoped whitespace checks passed.

Limits: no ref resolution/relocation, branch-local definition hoisting, allOf field
extraction or general schema validator. Rename evidence does not evaluate patterns,
ranges or branch correlations and uses native values, not speculative coercion.
Existing JSON-container parsing still follows reversal. Ambiguous renames may fail
server validation rather than guess; mixed/opaque projected types stay uncoerced.
Nullable-single-survivor handling and definition-cache policy remain native.

Integration preserves live intent 027; this independent schema intent remains 028.
Upstream candidate correctness fix; retain until stable preserves root-union fields.
Rollback: revert this intent-shaped change. Parent independently reproduced both
normalization defects on the previous head, verified the repaired probes, and reran
all 259 acceptance tests. No runtime/profile writes, installation or gateway restart.
Published source is not installed/running behavior; native installation remains
Elliot's action.
