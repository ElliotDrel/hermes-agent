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
Tool previews retain command/code arguments and file/URL targets. Embedded
preview line breaks are replaced with spaces before the display length limit,
so each tool event occupies one logical line without changing executable input.
Discord client soft wrapping remains native. This is presentation-only.
Only explicit tool_preview_length settings impose a per-preview cap. Shared Discord limits,
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
Refused queued delivery must not fire delivery callbacks. Exact-turn presentation
owners coexist within one run generation; refused/interrupted/cancelled/incomplete
predecessors retain their own obligations and never lend UI to a successor.
Register ownership before every terminal return, including the interrupt recursion
cap. Capture immutable terminal ownership before adapter delivery; finish bounded
confirmed predecessor cleanup before spawning a drain successor. Remove an owned
ID only after transport-confirmed deletion. Two attempts per ID within the existing
25-second budget are terminal: exhaustion retains ownership, not a success claim;
no sweeper, retry service or automatic replay is added.
Final delivery, queue admission/grouping, model inputs, history/cache and compression
policy stay native; progress metadata is nonconversational. Other transports retain
native rendering.

Authorization: exact input/cleanup ownership override, preserving fixed-window
grouping and seal-time edit cutoff:
https://discord.com/channels/1517646536505557132/1558581315274805291/1558648435417284670.
Residual protected risk: callback/guard timing changes turn lifecycle; deterministic
checks cannot prove every concurrent transport/model behavior. Rollback reverts the
scoped source/tests/contract commit; no historical user rows are rewritten.
Touchpoints: platforms/base.py exact callback ownership and delivery-finally handoff;
run_turn.py producer quiescence, progress registration, retained ownership and
bounded deletion; run_turn_runner.py background-review ownership and pending
status sends; run_goals.py terminal-owned deferred notices.
Combined review repair: exact delivery never fires an ownerless same-generation
slot; review release and deferred goals capture the immutable terminal owner.
Chained callbacks retain only unsuccessful siblings, never replay successful ones.
Before cleanup snapshots, await progress cancellation/overflow, stream completion,
long-running sends and already scheduled cleanup-bearing status futures, including
queued predecessors. Freeze compositor sharing before successor admission.
Combined offline verification supersedes the separate-branch totals: 372 passed,
zero failed, exit 0 across 26 named gateway files (120.9s), retries disabled, using
scripts/run_tests.sh -j 1 --tb=short and
--basetemp=C:/Users/2supe/All Coding/Hermes-Agent/discord-ownership-combined/scratch/test-temp/verified.
Worktree-owned .venv, HERMES_HOME and TMPDIR/TEMP/TMP; exact commands and output
retained in worktree scratch/verified-command.txt and scratch/verified-output.txt.
Coverage includes actual recursive turn orchestration, cancel-time late physical
IDs and pending status, ownerless isolation, terminal goals/background release,
bounded failure retention, model-boundary inputs/history prefixes, agent cache,
stream/final/ledger invariants and native restart accepted FIFO. Python compile
and path-scoped whitespace checks pass. Fake agents/transports only, no paid calls
or live Discord validation; no push, staging, activation or gateway restart.
This remains fork-specific progress behavior, not a stock-upstream defect.

## HERMES-FORK-018: Fixed Discord composition window

Ordinary busy queue-mode Discord text reserves one FIFO position and groups
compatible same-sender messages for a fixed 30 seconds. Each physical message
is fetched once at seal to include edits; edits cannot become control commands.
The current answer is delivered before waiting for the next composition window.
Media, other senders, explicit commands and other transports stay separate.
Only the final queued message receives the waiting reaction, removed on admission.

Accepted-input ownership repair (approved specifically in
https://discord.com/channels/1517646536505557132/1558581315274805291/1558648435417284670):
joins require the FIFO owner or its explicitly selected drain capability, cannot
cross an already queued boundary, and stop/reset revokes that capability before
any asynchronous teardown. Capacity refuses admission with a resend notice; the
Discord ingress receipt reports actual acceptance instead of unconditional success.
Sealing retains the owner's original physical ID/raw message; existing Discord
recovery processing hooks account for every member. Waiting-marker removal commits
success only after transport success, serializes concurrent drains and retains one
bounded retry at completion. Preserve fixed deadlines, seal-time edits, sender,
command/media/security boundaries and prior history/system/cache prefixes.

Graceful restart now finishes previously accepted FIFO follow-ups within the native
restart after-turn/drain budget instead of dequeuing and discarding them merely
because `_draining` is true. New human ingress is refused with a resend notice,
including busy/sentinel paths; explicit stop/reset still revokes the selected owner.
The native timeout phase closes successor execution before its first await.
Interrupted workers leave the queue untouched; delivery/seal cancellation returns
an unstarted selected physical event ahead of its promoted successor. Adapter
completion/finally cannot redrain residuals through the cold refusal gate and
mistakenly account them as completed. No DB, service, text-only executable replay,
provider payload rewrite or history migration is introduced.

Touchpoints: run_busy.py, run_agent_cache.py, run_turn.py, run_inbound.py,
run_shutdown.py, platforms/base.py, discord_composition.py, Discord adapter
processing/admission; composition, input-ownership and restart-pending regressions.
Combined review preserves the first physical message ID/raw object for ledger
accounting while reply_anchor_override at seal targets the last physical member
(native reply behavior). Inline busy/control admission and real debounce storage
now stamp acceptance; failed dispatch, denied admission, pause/drain and full FIFO
remain refusals. A debounce path that cannot store the event returns False.
The combined 26-file verification in 017 passes all 372 tests. The unchanged
external-drain watcher additionally passed 16 tests (3.8s) on an isolated rerun;
a broader run hit its known Windows WinError 32 marker-removal race (387 passed,
1 failed). No watcher source or assertions changed. The /update busy-bypass
expectation in test_session_race_guard.py fails identically on unchanged captured
ac297a4c4c (8 passed, 1 failed): maintained 001 deliberately rejects mid-turn update;
no unrelated behavior/test rewrite is included. Prior independent-branch evidence
is superseded by the combined checks above. No production lifecycle commands,
live traffic, remote integration or publishing were performed.
Rollback the intent-scoped source/test/doc commits; do not rewrite old history.
Fork-specific; no upstream report, push, staging or live activation in this task.

Guarantee boundary: accepted FIFO executes before a graceful restart when it fits
the existing budgets. Residual Discord work at timeout uses the existing optional
missed-message history recovery with physical IDs and current ingress/security
checks, not shutdown_flush text as an executable event. Recovery requires enabled
backfill, a healthy ledger, eligible configured channels/threads, readable retained
Discord history within the scan window/limits and remaining attempts. Fresh active
claims may defer recovery for ten minutes; offline recovery does not preserve the
old process's composition grouping or promise globally identical FIFO across lanes.
Arbitrary crash persistence, disabled/unavailable backfill and deleted/inaccessible
messages are not guaranteed; no durable queue subsystem was added. Two failed marker
removals retain reaction ownership but exhaust retries; no sweeper is added.
Deterministic fake-transport/model-boundary tests do not prove live/model behavior.

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

Rename transaction correction: native `/rename` owns a thread from admission through
confirmation and conditional persistence; duplicates report pending, never DB-only
"already titled". Automatic native renames share that admission. Explicit names
are validated unchanged against Discord's documented 1–100 character limit before
mutation (https://docs.discord.com/developers/resources/channel). Fresh GET/PATCH/GET
confirms the visible name before title/provenance CAS; native uniqueness, Bot Chat,
lineage and ended-session constraints stay enforced. A newer manual decision wins.

Rename-local single-attempt HTTP uses connected native auth/proxy/user-agent and
known native route/global cooldown state, with a 15-second transport budget. `429`
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

## HERMES-FORK-028: Inert pinned staging and explicit user activation

`hermes update --stage SHA --branch live` prepares exact published source and a
separate relocatable environment without changing active HEAD/refs/dependencies,
profile configuration or gateway lifecycle. Reuse native managed uv, locked sync,
profile update lock, receipts, holder classification and rename retry. Install
locked dependencies with `--no-install-project --no-editable`; Hermes forbids
non-Nix wheels. The existing relative checkout binding and native module launcher
survive whole-tree promotion. Rebuild the three declared Hermes console entrypoints
from CPython's bundled installer stub, bound to the final install path;
ACP/editor paths and installer checks remain present without building a project wheel.
Restore declared enabled plugin/lazy/tool dependencies
in the candidate only; conflicts refuse preparation.

Explicit user `/restart` captures the validated pin and uses native drain/Windows
watcher. Activation refuses changed/dirty revisions or live holders, retains the
previous installation, rolls back promotion/import failures and starts the fresh
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
