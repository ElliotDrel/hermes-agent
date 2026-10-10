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

## HERMES-FORK-027: Inert pinned staging and explicit user activation

`hermes update --stage SHA --branch live` prepares exact published source and a
separate relocatable environment without changing active HEAD/refs/dependencies,
profile configuration or gateway lifecycle. Native managed uv resolution, locked
installation/streaming, profile update lock, receipts, holder classification and
rename retry are reused. No pm layer, generation manager, custom installer,
supervisor, daemon or force-venv path is introduced. Declared enabled plugin and
native lazy/tool dependencies target the candidate only; conflicts refuse it.
Relative checkout import binding survives whole-tree source+venv promotion.

Explicit authorized user `/restart` captures the validated pin and uses native
drain/Windows watcher. Candidate interpreter activates only after gateway exit,
refuses changed/dirty revisions or live holders, retains previous installation,
rolls back promotion/import failures, and uses fresh native gateway start with
readiness/exact code_sha verification. Activation never fetches moving live.
Preparation /update no longer creates installation/watch markers or generates
restart permission; /update stable alone selects stable rebase preparation.

Authorization: change-specific *** override after routing/model-boundary
assessment: https://discord.com/channels/1517646536505557132/1558297938780885114/1558582253687406788.
Protected scope is explicit current user command rendering/admission, not ordinary
history/system/tool/schema mutation. Cached/uncached real provider-wire and resume
regressions remain; deterministic tests cannot prove every model's behavior.
Rollback source via this intent commit, retain candidate/previous data, and have
Elliot perform any required activation. No runtime edits/restarts or publication.

Touchpoints: hermes_cli/update_stage.py, native update parser/main/windows holder
scan, fork_update_entry.py/commands.py, gateway/slash_commands.py/run_shutdown.py;
tests/hermes_cli/test_update_stage.py, tests/gateway/test_staged_restart.py and
narrow update-route regression adaptation. User/bootstrap docs:
website/docs/user-guide/staged-fork-updates.md.

Verification: isolated worktree `.venv` via uv sync --extra dev --extra messaging
--frozen --no-install-project. Canonical 11-file acceptance: 133 passed, 0 failed,
no skips; real disposable Git origin/installed/candidate and actual uv venv/locked
wheel installation plus declared six plugin restoration/relocation. Covers moving
published live after stage, exact pin activation, unchanged active HEAD/venv,
dependency failure, live gateway refusal, tampering, wrong token, rename rollback,
native launch payload and success/failed receipt. Native start/readiness are mocked
at the last transport boundary; no live gateway or paid model calls. Earlier
constraint-path failure reproduced with real uv (space-containing absolute
--constraint split); candidate-relative constraint path fixes it. Syntax, CLI
--help and scoped diff checks pass. Final rerun command/result follows below.

Limitations: Windows standalone single-profile detached owner only (Startup/Task
login persistence is not a runtime supervisor); unsupported owners/platforms fail
clearly. Read-only actual owner probe accepts this machine's detached runtime.
No UI builds, full fleet, undeclared pip/native dependency preservation, managed
Python generation replacement or preparation-time state migrations. Failed source
candidates and previous installations are retained, not auto-deleted. Native lock
semantics remain; host crash between two tree renames still requires retained-tree
recovery, not a new journal. Old installed 62c55abdee /restart cannot activate this
first candidate: publish compatible source, run the new staging module against the
old root, then Elliot performs native gateway stop and candidate-token activation
from a terminal outside both trees. Only subsequent loaded handlers offer
/restart-only activation. This is tested/committed source, not installed readiness.

Final acceptance after all source/test edits: 11 files, 133 passed, 0 failed,
0 skipped, exit 0, 60.4s. Also verifies relocated real uv console-script launcher.
Exact command (worktree root, own `.venv`):
```bash
TMPDIR='C:/Users/2supe/All Coding/Hermes-Agent/staged-update/.test-scratch/tmp' \
HERMES_TEST_FILE_RETRIES=0 bash scripts/run_tests.sh -j 1 \
 tests/hermes_cli/test_update_stage.py tests/gateway/test_staged_restart.py \
 tests/gateway/test_fork_update_entry.py tests/gateway/test_restart_notification.py \
 tests/gateway/test_restart_drain.py tests/gateway/test_restart_redelivery_dedup.py \
 tests/gateway/test_restart_service_detection.py tests/hermes_cli/test_update_command.py \
 tests/hermes_cli/test_update_lock.py tests/hermes_cli/test_update_list_venv_holders.py \
 tests/hermes_cli/test_venv_holder_classifier.py \
 --basetemp='C:/Users/2supe/All Coding/Hermes-Agent/staged-update/.test-scratch/commit-acceptance' \
 --tb=short
```
Edited-file compileall, `git diff --check` and real CLI `update --help` passed.
Disposable test scratch/venvs are worktree-local and excluded from the source commit.
No paid calls, live gateway lifecycle, source publication or runtime writes.

Focused follow-up within approved 027 scope: whitespace token parsing now keeps
`/update   stable` and `/update\tstable` in stable-rebase mode while preserving
literal command text. Actual candidate-executable activation runs in a disposable
Windows subprocess from the tree being renamed; production owner/holder checks,
rename/probe/rollback and receipt code execute, with only gateway launch/readiness
and SHA attestation mocked. Windows relocation succeeded; no activation source
change was necessary. The small fixture borrows development site-packages solely
to import the production activation helper.

Canonical follow-up: the same 11 files named above, 142 passed, 0 failed,
0 skipped, exit 0, 86.6s. TMPDIR/TEMP/TMP and HERMES_HOME were isolated;
--basetemp ended in .test-scratch/scope-final. The real-handler whitespace test
initially lacked plugin discovery (four fixture failures); discovery now matches
existing routing fixtures. Syntax and scoped diff checks passed.

Full-source viability is NOT established: a clean disposable clone of committed
05b1e75b73d99203832d26d497cbf05e4032173c with an isolated profile and managed uv
reached real locked resolution (258 packages) but `_prepare_environment` failed:
`uv sync --no-editable` asks for a Hermes wheel, which setup.py explicitly forbids
outside Nix. CLI help/import probing therefore never ran. No Nix-build bypass
was used. Evidence: C:/Users/2supe/Hermes-Workspace/cache/scratch/
full-stage-05b1e75b73-managed/result.json and terminal proc_a025055e7188 output.
The first probe lacked isolated managed uv; a copied uv binary in the scratch
profile resolved that harness prerequisite before reproducing the build blocker.
The dependency-install strategy needs a separate focused correction before
claiming production staging or restart readiness; fixture success is not proof.
The documented old-handler bootstrap still requires Elliot's terminal stop/token
activation; it does not satisfy a Discord-only first deployment. That unresolved
bootstrap limitation is separate from the dependency-build blocker.
No push, runtime writes or gateway lifecycle actions were performed.