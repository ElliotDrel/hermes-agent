# Maintained Hermes changes

Base: official stable v2026.9.24, f97608f178d1ffeca59860195ab7da295f7c8e5f.
`main` stays exact official stable. The gateway runs `live`, which carries only
the entries below. Rebase these commits on each stable release; audit their intent
even when Git reports no conflicts. Prior customizations remain on backup branches
and are not part of this running version.

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
