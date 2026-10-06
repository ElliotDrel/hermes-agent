# Maintained Hermes changes

Base: official stable v2026.9.24, f97608f178d1ffeca59860195ab7da295f7c8e5f.
`main` stays exact official stable. The gateway runs `live`, which carries only
the entries below. Rebase these commits on each stable release; audit their intent
even when Git reports no conflicts. Retired intent and high-level implementation
notes live in PATCH-ARCHIVE.md. That archive is not a restoration backlog.

## HERMES-FORK-023: Native Windows update drain

Windows updates use the normal restart exit budget, including its after-turn wait.
The socket pause is attempted before the planned-stop marker; the marker is only
the fallback when no valid ACK arrives. Otherwise the marker invokes direct stop
and bypasses normal agent draining. The existing drain reporter shows active work
while waiting. Native timeout, forced-stop fallback, install and restart remain native.

Touchpoints: hermes_cli/update_cmd_windows.py. No gateway turn-loop changes.
Verification: test_windows_update_drain.py initially failed for the short deadline
and missing reports; socket-first regression initially observed marker-before-socket.
Final focused/adjacent Windows suite: 43 passed. Tests simulate process lifecycle;
they do not kill a live agent. Upstream candidate bug fixes, not a replacement updater.

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

Touchpoints: hermes_cli/fork_update_entry.py, hermes_cli/commands.py,
gateway/run_inbound.py, gateway/run_busy.py, gateway/slash_commands.py.
Authorization: Elliot explicitly approved restoring the skill request route and
replacing the update skill, then authorized this minimal implementation after testing
plain stable. Protected scope is the explicit update user-turn rendering/admission;
no provider/system-prompt/tool-schema/history mutation is added. Offline request
tests cover Discord/Telegram, authorization, busy admission, unchanged prefix,
schemas/options and multi-turn resume with caching on/off. Passing deterministic
tests do not establish every possible model behavior.

## HERMES-FORK-016: Per-run Discord update reporting

Reuse the invoking thread; create one when invoked outside a thread. Stream native
output and deliver completion to that run, falling back to its configured main chat
on delivery failure. Never inherit a previous home thread. Preserve undelivered
receipts, inspect returned send failures, and mark notices nonconversational.
Preparation can precede the normal restart wait, so the watcher includes that budget.

Touchpoints: plugins/platforms/discord/adapter.py, gateway/run_notifications.py.
Verification: thread/fallback regressions failed before the change and pass afterward;
focused streaming and restart tests also run. Next-release full Discord-triggered
rebase/install is an acceptance check after deployment, not claimed from unit tests.

Integrated acceptance: 114 source tests and 6 real-Git/handoff helper tests passed.

Main-channel regression: `/update` must call the existing thread helper with its
supported arguments. The thread-creation test now exercises that real helper,
mocking only Discord I/O; it reproduced the unsupported `reason` argument before
the one-line fix. Existing-thread and thread-failure paths remain covered.

Thread settings: `/update` uses the shared slash-thread helper, which explicitly
creates public threads and joins the requesting user (including the seed-message
fallback). Update and ordinary auto-threads share `discord.auto_thread_archive_duration`;
the existing seven-day setting is passed through the normal profile config bridge.
Tests exercise the real helper, membership, fallback, and equal configured durations.

## HERMES-FORK-005: Durable response-footer metadata

Restore provider context usage and the lifetime compaction count to the existing
output hook consumed by the workspace model-context-suffix plugin. Persist counts
through agent rebuilds, gateway reloads, compression rotation and Codex compaction.
Native early output transformation remains unchanged: the footer is saved with the
response and replays in subsequent history, as explicitly approved by Elliot.

Restoration authorization: Elliot approved 005/007 after reviewing this data flow
and cache/history impact. No system prompt, schema or request routing changes.
Touchpoints: agent/turn_finalizer.py, agent/context_compressor.py,
agent/codex_runtime.py. Source restored from archived 852cef97.
Verification: 24 tests passed across test_transform_llm_output_persistence.py,
test_compression_anti_thrash_persistence.py and test_codex_app_server_compaction.py
using scripts/run_tests.sh -j 1. Real SQLite and cached/uncached request tests cover
one footer per response, persisted response replay, and durable count restoration.
No live activation observed. Tests cannot guarantee nondeterministic model behavior.
Upstream candidate: richer output-hook metadata. Remove when stable supplies it.

## HERMES-FORK-015: Windows search pattern transport

Regex and glob arguments use pattern quoting, while filesystem paths keep native
path translation. This prevents backslashes such as \d and \( becoming slashes.
Literal backslash-n remains distinct from a regex newline; multiline parsing is
selected when ripgrep requires it, without a false newline-intent advisory.
Touchpoints: tools/file_operations.py and tools/file_operations_search.py.
Restored from 852cef97 after Elliot explicitly approved the tool-result impact.
Existing tests exercise real Windows search results, backslashes, globs and newline
semantics: scripts/run_tests.sh -j 1 tests/tools/test_search_pattern_backslash_windows.py
tests/tools/test_search_auto_multiline.py, 9 passed. Native schemas remain unchanged.
Candidate upstream correctness fix; retire when stable preserves these semantics.
Source-only restoration, no live activation or provider calls.

## HERMES-FORK-022: Restore paired task snapshots across turns

Recognize the registered todo_list name, legacy todo alias and a single-call
native tool_call bridge when restoring task state. Preserve existing call-ID
pairing and user-boundary checks; unrelated, malformed and multi-call results
cannot seed the store. Native tool schemas and cached request inputs stay intact.
The TUI consumes the same name predicate. No new persistence layer is introduced.

Touchpoints: tools/todo_tool.py, run_agent.py, model_tools.py,
tui_gateway/tool_progress.py. Restore the stable-layout adaptation of upstream
PR https://github.com/NousResearch/hermes-agent/pull/125161 from archived a2b80f0135.
Elliot explicitly approved the history/resume restoration after its boundary review.
Existing temporary-history and real-agent request tests cover cached/uncached
resume, saved task merging, compression rendering and hostile/unpaired history.
Verification via scripts/run_tests.sh -j 1: todo restoration 13, three todo tool
suites 37, TUI events 6, retention parity 11, native test_run_agent.py -k todo 6,
and registry/distribution 45 tests passed. No live activation observed; model
behavior remains nondeterministic. Retire when the selected stable includes this
upstream fix; do not file a duplicate implementation.

## HERMES-FORK-007: Provider duration metadata for quota footers

Expose provider-reported Codex window durations to the existing workspace footer,
label recognized five-hour and weekly windows, and retain unknown fallback labels.
Anthropic's named account-wide five_hour/seven_day windows supply 18,000/604,800
seconds explicitly. Model-specific caps remain distinct and cannot masquerade as
account-wide quota. No provider request or authentication path changes.

Touchpoint: agent/account_usage.py. Elliot approved restoring accurate quota
metadata alongside 005, including the discovered Anthropic contract correction.
Archived code omitted Anthropic durations; the new real-parser regression failed
with ('5-hour', None) != ('5-hour', 18000) before the correction. Afterward,
scripts/run_tests.sh -j 1 tests/agent/test_account_usage.py
tests/agent/test_account_usage_fetch.py passed 23 tests, including unknown durations.
Candidate upstream metadata improvement; retire when stable exposes these fields.
Source-only verification; no live activation or network/provider calls performed.
## HERMES-FORK-017: One Discord progress message

Opt-in display.platforms.discord.progress_compositor: single_message acknowledges
each turn before execution and edits one owned temporary message for compression,
tools, thinking, interim output and accepted steering. Final delivery stays native;
confirmed success awaits bounded cleanup, while failed or incomplete turns retain
the breadcrumb. Status notices are nonconversational; permanent edit failure never
creates a replacement. Other transports retain native presentation.

Touchpoints: gateway/progress_compositor.py, display_config.py, run_turn.py,
run_turn_runner.py, run_busy.py, run_inbound.py, session_state.py, turn_context.py,
platforms/base.py and the Discord adapter.
Authorization: Elliot approved this exact restored feature after scope/risk review
on 2026-10-05, including pre-agent compression progress, steering and cleanup.
Transfer preserves the curated archived implementation, adapting compression test
fixtures to stable's hard_msg_limit setting. Focused verification uses
scripts/run_tests.sh -j 1 with test_progress_compositor.py, test_progress_steer.py,
test_compression_progress.py, test_run_cleanup_progress.py, test_run_progress_topics.py,
test_queued_final_ledger.py, test_display_config.py and test_discord_message_lifecycle.py.
Deterministic payload/backfill tests cover history exclusion; live behavior awaits
the user's update-flow installation test. Revert this intent commit to roll back.

## HERMES-FORK-018: Fixed Discord composition window

Ordinary busy queue-mode Discord text reserves one FIFO position and groups
compatible same-sender messages for a fixed 30 seconds. Each physical message
is fetched once at seal to include edits; edits cannot become control commands.
The current answer is delivered before waiting for the next composition window.
Media, other senders, explicit commands and other transports stay separate.
Only the final queued message receives the waiting reaction, removed on admission.

Touchpoints: gateway/discord_composition.py, run_busy.py, run_turn.py,
platforms/base.py and the Discord adapter. Elliot explicitly approved restoring
this protected timing/grouping behavior after review on 2026-10-05.
Verification: scripts/run_tests.sh -j 1 tests/gateway/test_discord_composition_buffer.py
passed 16 tests after updating the archived fixture to stable's delivery adapter
resolver. No model/provider routing changes. Live installation remains pending.
Revert this intent commit to restore native queue handling.

## HERMES-FORK-021: Scoped Discord session messages

An owned Discord turn can send_session_message to an existing conversation of
the same user/profile. Reuse native route resolution, adapter admission and busy
FIFO. Process-local capability supplies identity, generation and shared chain
budgets; tool arguments cannot forge them. Existing routes only, no direct history
writes or bot self-message bypass. Peer content cannot authorize gateway control.
Other transports and delegated/background execution receive no capability.

Touchpoints: gateway/session_messaging.py, run_turn.py, run_inbound.py,
tools/session_message_tool.py and toolsets.py. Elliot approved restoring this exact
protected input/tool feature after scope/risk review on 2026-10-05. Adapted the
archived adapter lookup to stable's existing _delivery_adapter_for; no new resolver.
Verification: scripts/run_tests.sh -j 1 tests/gateway/test_session_messaging.py
tests/gateway/test_session_message_lifecycle.py tests/gateway/test_session_message_worker.py
tests/agent/test_session_message_payload.py. All 19 tests pass, including actual
worker dispatch, cached/uncached request snapshots, FIFO, persistence and refusals.
Explicit disabled_toolsets: [session_messaging] disables it; revert this intent
commit for source rollback. Live installation and nondeterministic behavior remain
for the user's update-flow test.

Restoration verification for 017/018/021 used 15 named files, including the
current updater route and Discord slash checks. Earlier aggregate pass counts
combined runs from different fixture revisions and are not final-SHA acceptance.
The integrated candidate must rerun these suites before publication. Two pre-existing Windows image-URI expectations fail identically on the
pre-restoration native-updater-source baseline: test_run_agent_queued_message_delivers_first_response_media
and test_run_agent_queued_message_delivers_streamed_first_response_media in
test_run_progress_topics.py. Those two are excluded from the passing rerun only;
no unrelated media implementation or expectation was changed. Progress fixture
adaptations preserve stable's hard_msg_limit and steer target wording.
## HERMES-FORK-002: Discord personal drafts

Ignore explicit draft/drafts markers (optional slash; whitespace or colon boundary)
before dispatch, history backfill and reply previews. Unrelated words remain input.
Elliot approved restoring this scoped input filter after reviewing its boundary.
Verification: focused Discord marker, ingress, backfill and reply tests.
Upstream disposition: retain while this personal draft convention is used.

## HERMES-FORK-004: Discord auto-thread rate limits

Confirmed thread-create429 records the parent channel retry deadline and skips
seed fallback/retry. A fallback429 removes its false seed when one exists.
Ordinary transient retries and configured thread settings remain native.
Verification: direct429, channel cooldown and fallback429 tests.
Upstream disposition: retain until native rate-limit behavior is equivalent.

## HERMES-FORK-012: Semantic titles and Discord rename

One coherent title customization: concise Title Case automatic titles use the
original opener (up to5000characters), excluding bound skill bodies. Bare /rename
regenerates from up to10000characters of clean history; explicit /rename sets the
thread and session title, including during an active turn, with actionable errors.
Opt-in discord.rename_manual_threads also titles user-created threads; fresh
Discord-name comparison preserves a human rename made during title generation.
Main input/history, provider routing and cached system/tool inputs remain native.

Authorization: Elliot explicitly requested all title customization as one patch,
then said "execute on this" after the scoped boundary/verification review.
Verification: title generation, regenerated history, bound-skill input, rename and
manual-thread suites pass; deterministic request tests cover cache on/off and
resume without modifying main input. Discord fetch/edit has no atomic compare,
so a narrow human-rename race remains. Restore via reviewed inverse and restart.
Upstream disposition: retain the custom title workflow until native equivalent.

## HERMES-FORK-013: Seven-day manual threads

Manual /thread and Discord-tool creation default to10080minutes. Auto, update and
handoff threads use validated discord.auto_thread_archive_duration (native1440
when unset/invalid). Preserve shared public-thread creation and requester membership.
Verification: real shared helper, manual default, tool request and configured/invalid
archive-duration tests. Upstream disposition: retain this retention preference.

017 transfer correction: pre-agent compression also uses stable's native delivery
adapter resolver. The final fixture adaptation exposed the remaining obsolete
call (10 compression tests failed before correction). Compression tests now use
the real GatewayRunner delivery resolver with registered adapter data, avoiding
a stub that could mask another removed API. This fixes a swallowed AttributeError
that skipped pre-agent compression when single-message progress was enabled.
Corrected native-resolver compression suite: 17 passed via scripts/run_tests.sh -j 1.
## HERMES-FORK-003: Convergent Discord command synchronization

Native safe synchronization treats unspecified installation types as Discord-owned
application defaults. An API-populated default must not trigger delete/recreate.
Explicit installation types, contexts and other managed fields remain compared.
Native fingerprint persistence, pacing,429cooldown and timeout policy stay intact.

Evidence: read-only live comparison found all67 existing commands differed only
in this API-populated field, explaining repeated recreation and rate limits.
Verification: real discord.py AppCommand serialization in isolated subprocesses
reproduces unnecessary recreation; equivalent defaults now produce no mutations,
while explicit mismatches still recreate. No Discord writes occur in tests.
Upstream disposition: focused native comparison bug fix; old startup policy omitted.

021 fixture correction: the cached/uncached provider-wire fixture now names
_delivery_adapter_for, matching the restored production API. Audit of every
restored Python file found no remaining _adapter_for_source reference. The two
payload cases reproduced failure before this fixture correction and passed after.
Exact corrected source checks: compression suite 17 passed after 5b8d9adff4;
payload suite 2 passed after this fixture-only correction. Other earlier results
are checkpoints, not a claim that the complete final commit was rerun.

## HERMES-FORK-024: Keep Windows updater lock checks lightweight

The post-swap updater must inspect its parent's update lock without importing
messaging dependencies it may need to replace. Reuse the existing stdlib-only
recovery PID probe on Windows; retain the zombie-aware gateway probe on POSIX.
The native dependency guard, installer, recovery and restart flow remain intact.

Evidence: the parent-lock probe imported gateway.status, whose package imports
reached httpx and brotlicffi. The updater then refused its own loaded DLL before
refreshing Discord dependencies. Core recovery did not include that optional
dependency, so the same refusal repeated.
Verification: a fresh-process post-swap regression failed before the correction
and passed afterward; lock, handoff and self-lock suites passed (47 tests, one
Linux-only skip). A disposable Windows venv also verified that the current uv
can upgrade brotlicffi while the original parent has its DLL mapped, so no extra
parent-exit mechanism is needed. Live Discord update verification is pending.
Upstream disposition: retain until native lock checks avoid these eager imports.

Live follow-up: 94d41fae passed the lock check but the backend installer imported
authentication code later and mapped brotlicffi again. The earlier uv experiment
used a cache-hardlinked DLL; repeating with a copied DLL reproduced Access is
denied (os error 5) and a partially removed package. Thus that experiment did not
establish safety of the live installation. See 025 for the additional correction.

## HERMES-FORK-025: Avoid premature HTTP imports and report failed updates

Construct the existing Codex response-cap stream class inside its response hook.
Provider-registry reads used by updater subprocess environment preparation must
not import httpx and map compression DLLs before replacement. The stream body,
response cap, errors and network requests are unchanged. Elliot specifically
approved this lazy-import correction on 2026-10-05. Revert this commit to undo it.

Failed optional-backend refreshes retain the native incomplete marker even when
unrelated core probes pass. Both native completion banners check that marker and
return incomplete to the existing restart/receipt flow. Print full installer
errors; do not claim the previous backend survived a failed uninstall.
Touchpoints: auth_codex.py, update_cmd_deps.py, update_cmd_maint.py.

Evidence: actual installer preparation imported auth_codex's top-level HTTP
subclass. A copied, loaded brotlicffi DLL reproduced uv's os error 5; the broken
live package was repaired through the native installer with the gateway stopped.
Focused checks: installer boundary stays free of HTTP/compression imports; normal
and oversized auth responses preserve the cap; a failed refresh cannot print
success even with healthy core probes. Nine affected checks passed. Live native
update verification follows publication. Retain until stable fixes these paths.

Live follow-up: native update installed 88fb830d, restarted a healthy gateway and
correctly recorded partial after detecting an empty old brotlicffi dist-info
directory. Removing that verified empty directory restored metadata discovery.
The run also exposed an upstream exit-code mismatch: fleet verification wrote a
partial receipt but returned zero when restart alone succeeded. update_cmd_fleet.py
now exits one for incomplete installation after clearing a completed restart's
marker. This keeps the native detached wrapper from overwriting failure with zero.
Computer-use driver refresh succeeded (already current, 0.34.0) on this run.
