# Hermes fork patch ledger

This file records every deliberate behavior change carried by
`ElliotDrel/hermes-agent:main` relative to the selected stable release of
`NousResearch/hermes-agent`.
Each active entry has a stable ID and lives in the same normal commit as the
behavior it describes. `hermes update` performs deterministic rebases. The
`hermes-fork-update` skill decides whether each entry still belongs after an
upstream change.

## Fork baseline

- **Upstream:** `https://github.com/NousResearch/hermes-agent.git`
- **Running branch:** `ElliotDrel/hermes-agent:main`
- **Update channel:** official stable calendar-version tags
- **Migration base:** Hermes `0.20.6` (`v2026.8.27` release line), captured at
  official commit `4f22543509d1b91dc45bcb369447126c5eb14fb7`

Verify the ledger and fork delta with:

```text
git status --short --branch
git log --oneline --decorate --first-parent
git diff <recorded-upstream-sha>..HEAD
scripts/run_tests.sh -j 1 tests/hermes_cli/test_fork_update.py
```

## Entry contract

Each active entry must state its intent, observable behavior, code touchpoints,
verification, and upstream disposition. Retire an entry instead of deleting its
history when upstream makes the behavior unnecessary.

## Active patches

### HERMES-FORK-001: Resumable PatchMD fork updates (retired)

- **Retirement, 2026-10-03:** Elliot explicitly authorizes candidate-only retirement
  after the protected shutdown/turn/input assessment. Restore updater, shutdown,
  marker, control-socket and notification code to pinned official stable
  `v2026.9.24` (`f97608f178d1ffeca59860195ab7da295f7c8e5f`). Remove
  `hermes_cli/fork_update.py` and custom-only legacy updater tests. Preserve
  unrelated progress, auxiliary/title and Windows venv behavior, ledger history,
  and the maintained-fork gates in `AGENTS.md`. The history below describes the
  retired implementation; it is no longer an active contract.
- **Scope:** Baseline restoration precedes replacement routing in separate local
  commits. No live source, dependency, profile, checkpoint, publication, installer
  or gateway control action is authorized. `fork-update-state.json` stays intact.
  The existing workspace skill owns the replacement workflow, not updater code.

- **Intent:** Keep the installed checkout on fork `main` while preserving every
  deliberate local behavior as reviewable commits rebased onto official stable.
- **Behavior:** With `updates.channel: stable` and
  `updates.fork_strategy: rebase`, `hermes update` resolves the newest official
  stable release tag, records that exact tag and commit with a recovery ref,
  and rebases onto the release commit rather than untagged `upstream/main`.
  It pushes with an explicit force-with-lease, completes the normal post-update
  pipeline, then exits `3` until the `hermes-fork-update` skill audits this
  ledger. Gateway `/update` reports that exit as an intentional judgment
  handoff rather than a failure, explains what remains, and tells the user to
  start a thread from the completion message and ask the agent to run the
  audit. On Windows, update pause uses the same graceful drain, interruption,
  and resume-pending path as `/restart`, while capping the update-specific
  after-turn wait at two minutes; its acknowledgement also covers normal and
  cron drain budgets so a busy gateway cannot trigger an early process-tree
  kill that also terminates its detached updater. Before rebasing, the updater
  creates a detached recovery worktree at the verified pre-update commit. If a
  rebase pauses on conflicts, Windows restarts the gateway from that clean tree
  while the installed checkout retains its active conflict state. Conflicts
  remain as an active Git rebase and resume through the `hermes update --continue`
  command; `--abort` restores the recorded backup without deleting it. Before
  any normal source write, the root `AGENTS.md` requires the current-turn
  `hermes-fork-change` skill gate. Before update, rebase, or audit work, it
  requires the current-turn `hermes-fork-update` skill gate. The update gate
  owns audit-required source edits.
- **Touchpoints:** `AGENTS.md`, `hermes_cli/fork_update.py`, `hermes_cli/update_cmd.py`,
  `hermes_cli/subcommands/update.py`, `hermes_cli/config_defaults.py`,
  `gateway/control_socket.py`, `gateway/run.py`, `gateway/run_shutdown.py`,
  `gateway/run_notifications.py`, and the focused updater, control-socket, and gateway-notification tests.
- **Verification:** Isolated repositories exercise stable-tag selection while
  ignoring a newer untagged upstream commit, shallow-checkout history repair,
  clean rebase, conflict pause, resolved continuation, abort restoration,
  ancestry checks, explicit lease push, and the exit-`3` boundary before
  post-update work. Gateway notification regressions cover both live streaming
  and restart-recovery delivery of the PATCH.md audit handoff.
- **Recovery/shutdown repair and protected override (2026-10-02):** After the
  timing, interruption/resume, bounded-test and rollback assessment, Elliot
  directly approved `ok execute on this. no other seesions are running` in
  Discord message `1555575601753104446`, thread `1555552001478107226`.
  Scope is conflict-safe recovery and update-pause shutdown coordination.
  Shutdown sequencing, staging architecture, live updates and automatic gateway
  restarts are excluded. Supported manual recovery remains the fallback.
- **Incident and mechanism:** The update stopped the gateway, paused the rebase
  on conflicts, then failed with `Could not restart every paused Windows
  gateway`. Cached pre-update `hermes_cli.gateway` later imported the rebased
  Windows helper, whose signature rejected `source_root` before spawning.
  Independently, a marker callback queued before socket pause called immediate
  `stop()` after coordinated waiting began, interrupting active conversations.
- **Repair contract:** Recovery delegates to a bounded fresh interpreter rooted
  in the verified pre-update recovery tree. Source imports and the existing
  detached watcher use that tree; profile/home and shared venv dependencies are
  preserved. Failed launches retain their retry obligations. Ordinary restarts
  keep their existing path. Update marker and socket scheduling must use one
  existing coordinated restart wait, including the two-minute update cap.
  Generic stop markers, real signals and takeover keep their original behavior.
  Marker metadata must come from the same validated record as its identity.
- **Repair touchpoints:** `hermes_cli/update_cmd_windows.py`, `gateway/run.py`,
  `gateway/status.py`, `tests/hermes_cli/test_conflict_safe_update_recovery.py`,
  `tests/gateway/test_update_pause_coordination.py`, and the legacy boolean mock
  fixtures in `tests/hermes_cli/test_cmd_update.py`.
- **Tests-first evidence:** Offline parent baseline-function replay from
  `8d61d27d5a`, without rewriting the installed checkout, reports
  `17 failed, 8 passed`. Failure assertions include `Queued marker bypassed the
  accepted after-turn wait`, `Update marker bypassed coordinated after-turn
  restart`, and the exact generic recovery error above. The initial repair
  passes all `25` new tests; the unchanged adjacent gate passes `178` tests
  before and after the edits. Independent review then catches an unlabelled
  production marker writer and separate metadata/identity reads; the final
  repair must cover the real writer, not only constructed test markers.
  Five older updater tests fail identically on baseline because bare MagicMock
  returns are interpreted as paused maintained-fork results. Explicit legacy
  `True` returns repair those fixtures. The corrected updater plus model-boundary
  selection reports `59 passed`, including real offline provider-request
  snapshots across cached/uncached multi-turn compaction adoption and Telegram
  isolation. No model-request assembly, schemas, cache or persistence code is
  changed.
- **Review corrections and final parent gate:** The production update marker
  now explicitly labels `pause-for-update`. A record-returning consume helper
  supplies the validated identity and operation in one read; legacy boolean
  consumers retain their API. Real-writer tests cover both callback orderings.
  Recovery diagnostics report only allowlisted reason, stage and exception
  class through a bounded file read, never arbitrary exception text, argv or
  environment. Correction tests first report `11 failed, 18 passed` for the
  missing writer label, duplicate reads and missing diagnostics; the final
  focused gate reports `30 passed`. The combined named offline gate reports
  `355 passed, 1 deselected in 98.19s`. The deselection is
  `TestReadProcessCmdlinePsFallback::test_ps_fallback_when_proc_unavailable`,
  whose `/usr/libexec/bluetoothuserd` POSIX expectation fails identically with
  the original function loaded from `8d61d27d5a` on Windows (`1 failed`).
  The six edited Python modules/tests compile and `git diff --check` passes.
  The actual AIAgent snapshot tests cover message bytes/data, schemas, request
  options, cached/uncached multi-turn history and compaction adoption, including
  Telegram isolation. Final independent read-only review reports `passed: true`
  with empty security and logic-error lists. These checks are offline and do
  not prove live readiness.
- **Recovery test harness incident:** The initial delegated RED invocation did
  not stub every watcher creation boundary and attempted real watcher spawns.
  The autouse creation guard was installed before rerunning RED and editing
  production. Parent process inspection finds no detached restart watchers;
  gateway PID `2380` still reports the original `8d61d27d5a` runtime. This is
  not a live activation test, and the earlier invocation is not claimed hermetic.
- **Repair verification command:** On this host, the source venv lacks pytest.
  Use the disposable pytest interpreter with process-local access to installed
  dependencies; do not sync or modify the running gateway's environment:
  ```text
  uv run --no-project --with pytest --with pytest-asyncio python -c "import pytest,sys; sys.path.append('C:/Users/2supe/AppData/Local/hermes/hermes-agent/venv/Lib/site-packages'); raise SystemExit(pytest.main(['-q','tests/gateway/test_update_pause_coordination.py','tests/hermes_cli/test_conflict_safe_update_recovery.py','tests/hermes_cli/test_cmd_update.py','tests/gateway/test_restart_after_turn.py','tests/gateway/test_restart_resume_pending.py','tests/gateway/test_restart_drain.py','tests/gateway/test_restart_drain_recovery_dedup.py','tests/hermes_cli/test_windows_update_restart_reconciliation.py','tests/gateway/test_compression_progress.py::test_real_detached_adoption_and_model_requests_ignore_display','tests/agent/test_session_message_payload.py','tests/agent/test_prompt_cache_boundary.py','tests/agent/test_prompt_cache_scope.py','--basetemp=C:/Users/2supe/Hermes-Workspace/.scratchpad/updater-repair-final']))"
  ```
- **Repair residual risk and activation:** Offline tests cannot prove live
  process creation, Discord reconnect, nondeterministic turn behavior or
  readiness after a real conflict. Recovery still uses the shared venv and can
  fail if dependencies are damaged. Interpreter site initialization and inherited
  package directories remain trusted; the helper-source check is not a sandbox
  or a complete editable-install/import-isolation proof. A child timeout can be
  ambiguous if its
  detached watcher was already spawned; retain the obligation and let the
  existing fleet/readiness checks decide. The active gateway must be restarted
  manually before `/update` uses the changed shutdown handler. No live update
  or restart is performed for this repair. To roll back, revert only this
  repair's source commit with review, then restart manually; no session or
  configuration rewrite is needed.
- **Stable-port updater verification:** Upstream fresh-interpreter post-swap
  handoff remains intact; redundant fork synchronization is omitted in the
  new finish seam. Installed fork/recovery/continuation/pause/launcher-refresh
  suites report `78 passed`. Native-path shallow cloning uses Git transport
  with `--no-local --depth 1`; current shutdown/handoff APIs replace old mocks.
  Live conflict recovery already reconnects the clean pre-update checkout.
  Remaining installation/activation still belongs to the updater and operator.
- **Continuation control-flow repair, 2026-10-02:** The operator's
  `hermes update --continue --yes` attempt resumed the recorded release, then
  the checkout planner tried a second fork sync because `HEAD` matched
  `origin/main`. `rebase_fork_onto_upstream` correctly refused its own pending
  audit, raising `A maintained-fork patch audit is pending; complete it before
  starting another rebase.` The failure occurred before dependency installation;
  the operator restored the gateway separately. Successful continuation now
  passes an explicit `fork_sync_complete` flag to the planner. Only that path
  skips the redundant rebase; fresh updates retain upstream checks. The original
  pre-update head still forces the normal installation/handoff path even with
  zero origin commits. Audit, conflict, pause, and restart guards are unchanged.
  This CLI checkout-planning change does not alter model inputs or turn state.
  The real command and planner, with isolated Git/process/dependency I/O,
  reproduce `1 failed, 1 passed` before correction with
  `Continuation started a second fork rebase`; the installed focused updater,
  fork, conflict-recovery, and Windows reconciliation gate then reports
  `63 passed in 86.81s`. No live continuation or gateway lifecycle action runs
  during verification; the installation checkpoint remains pending.
  Runnable check uses the existing disposable pytest invocation and names
  `tests/hermes_cli/test_fork_update_continuation.py`,
  `tests/hermes_cli/test_cmd_update.py`, `tests/hermes_cli/test_fork_update.py`,
  `tests/hermes_cli/test_conflict_safe_update_recovery.py`, and
  `tests/hermes_cli/test_windows_update_restart_reconciliation.py`.
- **Upstream disposition:** Candidate for upstreaming after the local workflow
  proves stable. Keep active until official Hermes offers an equivalent
  resumable maintained-fork contract.

### HERMES-FORK-002: Discord draft-message exclusion

- **Intent:** Let a user keep visible personal drafts in Discord without those
  messages becoming Hermes input or conversational context.
- **Behavior:** Discord messages whose trimmed content begins with the
  case-insensitive marker `draft`, `drafts`, `/draft`, or `/drafts`, followed
  by the end of the message, whitespace, or a colon, are ignored before
  dispatch. The same filter excludes marked messages from history backfill and
  reply previews without matching unrelated words such as `drafting`.
- **Touchpoints:** `plugins/platforms/discord/adapter.py` and the focused
  Discord connection regressions.
- **Verification:** The focused marker and early-dispatch selection reports
  `18 passed`; the adapter and test module also pass Python compilation.
- **Upstream disposition:** Candidate for upstreaming as an opt-in personal
  draft convention. Keep active while Elliot relies on the marker contract.

### HERMES-FORK-003: Startup-only Discord command sync

- **Intent:** Prevent ordinary Discord reconnects from repeatedly consuming
  the application command-management rate-limit bucket.
- **Behavior:** `discord.command_sync_policy: startup` is bridged from YAML and
  permits one bounded slash-command synchronization per resolved Discord
  application in each gateway process. A converged fingerprint suppresses
  reconnect and adapter-rebuild attempts until the gateway restarts. A timed-out
  or rate-limited reconciliation is recorded as incomplete and may resume on a
  later reconnect after Discord's requested cooldown; a missing application ID
  defers safely.
- **Touchpoints:** `plugins/platforms/discord/adapter.py`, focused Discord
  connection tests, and Discord configuration documentation.
- **Incident:** A clean gateway restart on 2026-09-20 timed out after exactly
  30 seconds while `/skill` already matched Discord's global manifest. A direct
  request to the same Discord application-command endpoint returned `201` and
  immediate readback succeeded. The 2026-09-21 restart loaded fetch-cancellation
  instrumentation, timed out again, and emitted no fetch diagnostic. That live
  result disproved the stale fetch-bucket hypothesis and localized the stall to
  post-fetch comparison, mutation pacing, or a command mutation.
- **Diagnostics:** Temporary fetch-bucket inspection, mutation-stage logging, and
  their diagnostic-only tests were removed after they falsified the fetch theory
  and identified intentional pacing as the timeout site. Production retains no
  private discord.py introspection or temporary stage state.
- **Verification:** Focused tests cover YAML propagation, initial sync, missing
  application IDs, converged reconnect suppression, resumable incomplete
  reconciliation, per-request timeout, the fresh-process boundary, and pacing.
  Temporary instrumentation on the 2026-09-21 live restart reported
  `stage=pacing-before-create:reasoning`, proving the 30-second whole-job deadline
  was expiring during Hermes' intentional 4.5-second inter-mutation sleep. The
  deadline now applies separately to fetch and each mutation, while pacing can
  complete an arbitrary legitimate diff. The pacing regression reproduced RED
  with one of two creations completed and the exact stage warning, then GREEN.
  The full Discord command-sync gate reports `37 passed`; edited modules pass
  Python compilation and `git diff --check`. The clean 2026-09-21 production
  restart completed reconciliation after the paced backlog: `71` total,
  `67` recreated, `3` created, `1` updated, and `0` deleted. The persisted state
  records equal `last_attempt_at` and `last_success_at`, confirming convergence.
  Runnable check:
  `uv run --with pytest --with pytest-asyncio pytest tests/gateway/test_discord_connect.py tests/gateway/test_discord_sync_limit.py --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/discord-sync-stage`.
- **Upstream disposition:** Candidate for upstreaming as a supported sync
  policy. Keep active while Discord command limits remain operationally tight.

### HERMES-FORK-004: Discord auto-thread rate-limit cooldown

- **Intent:** Stop failed auto-thread creation from producing false notices,
  immediate retries, and repeated requests against a cooling-down bucket.
- **Behavior:** A Discord thread-create 429 records a parent-channel cooldown
  from `retry_after`, suppresses seed fallback and retry, and removes any false
  seed message created before a fallback 429.
- **Touchpoints:** `plugins/platforms/discord/adapter.py` and focused Discord
  channel-control tests.
- **Verification:** Focused regressions cover direct and fallback 429s plus
  suppression of additional attempts during the cooldown.
- **Upstream disposition:** Candidate for upstreaming as safer native Discord
  rate-limit handling. Keep active until upstream provides equivalent behavior.

### HERMES-FORK-005: Durable response-footer runtime metadata

- **Intent:** Keep the workspace response footer accurate without asking the
  model to report its own runtime metadata.
- **Behavior:** Final-output hooks receive provider-reported context usage and
  an exact lifetime compaction count that persists across agent rebuilds,
  gateway restarts, Codex app-server compactions, and compression rotations.
- **Touchpoints:** `agent/turn_finalizer.py`, `agent/context_compressor.py`,
  `agent/codex_runtime.py`, and focused persistence regressions.
- **Verification:** Focused tests cover metadata exposure, durable count reload,
  compression-boundary carry, and the Codex app-server increment path.
- **2026-10-02 port decision:** Elliot explicitly accepts retaining the transformed
  footer in persisted response history during this stable-release rebase. The
  upstream finalizer's early, once-only output hook remains in place. This reverses
  the former display-only exclusion; footer bytes can therefore enter later model
  context. Elliot's specific reversal is Discord message `1555608687802982592`;
  continuation approval is message `1555613346903101481`.
  No additional prompt assembly or history rewrite is introduced by the port.
- **Stable-port verification:** The metadata/persistence suites report `64 passed`.
  New real-agent, temporary-SQLite tests exercise cached and uncached two-turn
  requests: metadata reaches the early hook, each footer appears once, the
  saved response matches delivery, and the next request replays that text.
- **Upstream disposition:** Candidate for upstreaming as richer output-hook
  metadata. Keep active while the workspace footer consumes these fields.

### HERMES-FORK-006: Windows gateway venv dependencies

- **Intent:** Let the Windows gateway import dependencies installed in the
  Hermes project virtual environment, including local speech transcription.
- **Behavior:** Preserve upstream's venv console interpreter and hidden-console
  launcher. The VBS launcher also exposes the venv `Lib/site-packages` directory
  on `PYTHONPATH`. Do not restore the obsolete uv-base-interpreter selection.
- **Stable-port verification:** Launcher tests report `24 passed`; the privileged
  live Scheduled Task encoding test is deselected after `Access is denied`.
  A separate actual venv-interpreter probe imports YAML, HTTPX and discord.py
  from this installation's `venv/Lib/site-packages`. No task or gateway is created.
- **Touchpoints:** `hermes_cli/gateway_windows.py` and its focused VBS launcher
  regression.
- **Verification:** The launcher test confirms the generated environment
  includes the venv package directory.
- **Upstream disposition:** Candidate for upstreaming as a Windows uv/venv
  launcher correction. Keep active while the gateway uses this process shape.

### HERMES-FORK-007: Duration-based quota windows

- **Intent:** Report five-hour and weekly subscription usage from provider
  semantics instead of unstable positional response fields.
- **Behavior:** Codex quota windows are labeled from `limit_window_seconds`,
  Anthropic windows carry explicit durations, and unknown durations retain
  their fallback labels without entering duration-specific footer segments.
- **Touchpoints:** `agent/account_usage.py` and focused account-usage tests.
- **Verification:** Focused regressions cover duration labels, retained
  fallback behavior, and parsed duration metadata.
- **Stable-port verification:** The duration parsing/display gates pass `22`
  tests. Assertions adopt upstream `5-hour` for known 18,000-second windows;
  unknown durations retain their positional fallback.
- **Upstream disposition:** Candidate for upstreaming as more accurate account
  usage parsing. Keep active while the footer consumes recognized durations.

### HERMES-FORK-008: Explicit Discord cron continuation targets

- **Intent:** Prevent an agent-created continuable cron job from silently
  opening a dedicated thread beneath a normal Discord channel.
- **Behavior:** `attach_to_session=true` requires an existing explicit numeric
  Discord thread target, while ordinary parent-channel delivery must remain
  non-continuable.
- **Touchpoints:** `tools/cronjob_tools.py` and focused Discord cron-target
  validation tests.
- **Verification:** Focused regressions cover create, update, explicit-thread,
  origin-thread, invalid-ID, and non-Discord cases.
- **Upstream disposition:** Candidate for upstreaming as a safer continuation
  contract. Keep active while Discord delivery can create implicit threads.

### HERMES-FORK-009: Strict auxiliary provider routing

- **Intent:** Let an installation keep auxiliary work on its explicitly chosen
  provider chain instead of silently discovering unrelated credentials.
- **Behavior:** Setting `auxiliary.allow_provider_discovery_fallback: false`
  stops automatic auxiliary routing after the main provider and configured
  fallbacks fail, without probing OpenRouter, Nous, custom, or API-key lanes.
- **Touchpoints:** `agent/auxiliary_client.py`, the auxiliary configuration
  default, and focused routing tests.
- **Verification:** Focused regressions cover both strict routing and the
  backward-compatible discovery default.
- **Stable-port verification:** Both auxiliary routing suites pass `91` tests.
  Upstream selected-provider protection remains; the explicit no-discovery
  setting still prevents unrelated credential discovery.
- **Upstream disposition:** Candidate for upstreaming as an explicit provider
  isolation control. Keep active while auxiliary discovery is otherwise
  implicit.

### HERMES-FORK-010: Dedicated cron runtime storage

- **Intent:** Separate mutable cron runtime state from user-authored job
  definitions so backups, inspection, and upgrades handle each correctly.
- **Behavior:** Cron ledgers, incidents, suggestions, notepad state, telemetry,
  and related mutable data live beneath `cron/runtime/`; existing files migrate
  automatically and backup discovery follows the new layout.
- **Touchpoints:** Cron storage modules, scheduler backup paths,
  `hermes_cli/backup.py`, and focused migration/storage tests.
- **Verification:** Focused tests cover legacy migration and the relocated
  execution and usage-audit data.
- **Stable-port storage revision:** Notepad and suggestions retain upstream
  canonical SQLite open/transaction handling and atomic JSON writes with mode
  `0o600`. Optional explicit paths and call-time profile/runtime resolution
  preserve upstream overrides. Legacy migration uses the existing layout helper;
  a failed move reads the old store, and an existing runtime store wins conflicts.
  Deleted named profiles remain refused. Prompt rendering stays unchanged.
  The parent-installed storage/upgrade/atomic/SQLite/backup-restore gate reports
  `99 passed, 2 skipped`. Both skips are Linux-only. Backup fixtures seed
  `cron/runtime/jobs.json`; empty and partial restore assertions remain intact.
- **Upstream disposition:** Candidate for upstreaming as a clearer persistent
  state boundary. Keep active while runtime files otherwise share the job root.

### HERMES-FORK-011: Native Windows Bash resolution for cron

- **Intent:** Run shell-script cron jobs with Git Bash on Windows instead of
  accidentally invoking the incompatible WSL launcher.
- **Behavior:** Cron resolves Bash through Hermes' verified local environment
  helper and rejects System32/Sysnative candidates before launching native
  Windows script paths.
- **Touchpoints:** `cron/scheduler_script.py` and the direct Windows
  Bash-resolution verification harness.
- **Verification:** A focused direct harness covers Git Bash selection,
  WSL-launcher rejection, and the missing-interpreter error.
- **Upstream disposition:** Candidate for upstreaming as a Windows reliability
  fix. Keep active until upstream uses the same verified interpreter contract.

### HERMES-FORK-012: Semantic session titles and Discord rename

- **Intent:** Produce concise T3-style conversation titles, let a Discord user
  deliberately regenerate or replace the active session/thread title, and
  optionally apply the generated title to user-created Discord threads.
- **Behavior:** Opening-turn title generation uses the original T3-inspired
  semantic prompt: Title Case, concise wording, umbrella subject/outcome, and
  no process-only titles. It still uses the opening message and an instant
  derived preview, then upgrades from the configured auxiliary model; it does
  not call `/rename`. `/rename [title]` separately regenerates from bounded
  conversation history and the previous title, or sets an explicit title. It
  updates session metadata and the Discord thread, works during an active run,
  and surfaces native rename failures instead of silently hiding them. With
  `discord.rename_manual_threads: true`, the first generated LLM title also
  renames a user-created Discord thread. Hermes captures Discord's exact name
  at message receipt and applies the generated title only if that name remains
  unchanged, so a human rename made while generation is in flight wins. The
  opt-in defaults to `false`; Hermes-created auto-threads keep their existing
  behavior.
- **Incident:** After the upstream split, the initial generator used the
  upstream sentence-case, 3–7-word prompt while bare `/rename` retained the
  custom Title Case regeneration prompt. The installed fork's initial prompt
  no longer matched the original `106d48a5e4` source or this entry's intent.
  A new opening-turn regression failed on the missing semantic prompt before
  restoration, then passed afterward. The upstream example-echo defense stays
  intact with Title Case examples. The slash-command documentation parity
  check also exposed an older omission of `/rename`; the messaging reference
  now describes both its generated and explicit modes.
- **Touchpoints:** `agent/title_generator.py`,
  `tests/agent/test_title_generator.py`, `website/docs/reference/slash-commands.md`,
  gateway slash and mid-run dispatch, session-source metadata, Discord
  configuration defaults and adapter bridging, the Discord adapter, and focused
  title/rename/configuration tests.
- **Verification:** The original manual-thread lane change was verified by
  `110 passed`, including its YAML bridge and no-clobber checks. For this prompt
  restoration, the new regression failed on `recognize this chat weeks later`
  under the old initial prompt, then passed. The focused title, rename,
  Discord command, and website parity selection reports `82 passed, 1
  deselected`; edited Python modules compile and `git diff --check` passes.
  The deselected manual-thread test fails on the untouched baseline because
  this installation's `DISCORD_RENAME_MANUAL_THREADS` override supersedes both
  explicit true and false test adapter values. No paid title call or live
  post-restart observation was made. Runnable check:
  `uv run --with pytest --with pytest-asyncio --with discord.py pytest -q tests/agent/test_title_generator.py tests/gateway/test_rename_command.py tests/gateway/test_session_title_rename_lane.py tests/gateway/test_discord_slash_commands.py tests/gateway/test_slash_command_profile_scope.py tests/website/test_slash_commands_doc_parity.py -k 'not test_manual_thread_initial_name_uses_current_discord_name_only_when_enabled' --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/initial-title-final-0923`.
- **Stable-port adapter revision:** Upstream removes `_adapter_for_source`.
  Manual visible renames now use the existing `_delivery_adapter_for`, with
  the same source and unchanged admission/no-clobber guards. The installed
  title/progress/composition gate passes `286` tests, with two previously
  documented Windows media-URI assertions deselected.
- **Upstream disposition:** Candidate for upstreaming as a richer session-title
  workflow. Keep active while the workspace relies on this naming contract.

### HERMES-FORK-013: Seven-day Discord thread retention

- **Intent:** Keep manually created Discord work threads available for a week
  while allowing auto-created gateway threads to use an explicit duration.
- **Behavior:** Manual `/thread` and Discord-tool creation default to 10080
  minutes; gateway auto-threads and handoff threads read the validated
  `discord.auto_thread_archive_duration` setting, retaining the upstream
  one-day default when it is not configured.
- **Touchpoints:** Discord adapter, Discord tool schema/handler, configuration
  defaults, and focused archive-duration regressions.
- **Verification:** Focused tests cover manual defaults and configured
  auto-thread creation.
- **Upstream disposition:** Candidate for upstreaming as configurable thread
  lifecycle policy. Keep active while week-long manual retention is desired.

### HERMES-FORK-014: Safe Windows Chrome profile attachment

- **Intent:** Reuse real Chrome profiles on Windows without corrupting a live
  default profile and recover reliably when wrapper-based attachment fails.
- **Behavior:** Default-profile launches detect Chrome processes even when no
  explicit user-data-dir flag is present; copied profiles reuse a verified
  DevTools endpoint, launch visibly when requested, and fall back to direct CDP
  after agent-browser attachment fails.
- **Touchpoints:** Browser connection discovery, real-profile launch/attach
  logic, and focused real-profile tests.
- **Verification:** Focused regressions cover default-profile holder detection,
  surviving-copy attachment, headed launch, and direct-CDP recovery.
- **Stable-port verification:** Installed browser-profile tests report
  `65 passed, 2 deselected`. The failure-path fixture now denies both existing
  endpoint probes before injecting snapshot failure. No production substitution
  or Chrome launch occurs. The two excluded mode-bit assertions are POSIX-only
  expectations on Windows; no ACL/security behavior is changed.
- **Upstream disposition:** Candidate for upstreaming as Windows Chrome profile
  hardening. Keep active while the real-profile browser workflow depends on it.

### HERMES-FORK-015: Windows-safe search pattern transport

- **Intent:** Preserve regex, glob, and literal backslash patterns when search
  commands cross the Windows shell boundary.
- **Behavior:** Search pattern arguments are quoted/escaped for Windows without
  changing their meaning, and literal `\\n` stays distinct from a real newline
  unless multiline search is actually required.
- **Touchpoints:** `tools/file_operations.py` and focused Windows pattern tests.
- **Verification:** Focused regressions cover backslashes, quoting, globs,
  literal newline escapes, and multiline detection.
- **Stable-port verification:** Both installed search suites pass `9` tests.
  The LF-specific fixture writes explicit LF instead of Windows CRLF translation.
  Backslash transport and production multiline routing remain unchanged.
- **Upstream disposition:** Candidate for upstreaming as cross-platform search
  correctness. Keep active until upstream preserves the same pattern semantics.

### HERMES-FORK-016: Discord update thread anchoring

- **Intent:** Keep the full Discord update lifecycle and its follow-up patch
  audit conversation in one dedicated thread.
- **Behavior:** Native Discord `/update` reuses the current thread or creates a
  `Hermes update` thread before starting; progress, restart recovery, and the
  eventual judgment handoff target that thread. If Discord cannot create the
  thread, the update does not start and the interaction reports the error.
- **Touchpoints:** Discord slash-command dispatch and focused Discord slash
  regressions.
- **Verification:** Focused tests cover native command wiring,
  creation-before-dispatch, existing-thread reuse, and fail-closed behavior.
- **Upstream disposition:** Candidate for upstreaming as a safer Discord update
  conversation contract. Keep active while the update audit requires agent
  follow-up after gateway restart.

### HERMES-FORK-017: Discord single-message progress compositor

- **Intent:** Acknowledge every Discord turn immediately while keeping live
  progress observable without leaving temporary chatter in successful threads.
- **Behavior:** Opt-in `display.platforms.discord.progress_compositor:
  single_message` sends one temporary reply before agent execution and composes
  enabled tool, thinking, interim-assistant, heartbeat, and routine safe-status
  sources by editing that original message ID. Rendering keeps a rolling bounded
  window below Discord's 2,000-character limit. Retryable edit failures coalesce
  the newest desired state and retry the same ID; permanent failures freeze the
  breadcrumb and never create a replacement. Final answers retain the normal
  delivery path. Confirmed successful delivery deletes the owned temporary ID
  once, while failed, cancelled, interrupted, incomplete, or failed-delivery
  turns preserve it. Action-required and error notices remain standalone. The
  mode is clamped to Discord so global configuration cannot replace another
  platform's progress implementation. Confirmed streamed finals, which return
  `None` to suppress a duplicate send, count as delivered for the one-shot
  cleanup callback. The queued-first-answer path cleans its own progress after
  confirmed text delivery rather than returning before registration. Cleanup
  awaits a bounded deletion before releasing the turn; a refused or timed-out
  delete gets one bounded retry, subject to the 25-second total cleanup budget,
  and warns when any ID remains unattempted or unconfirmed. A refused final
  streamed edit cannot claim delivery. Failed, cancelled, interrupted,
  incomplete, and failed-delivery turns retain the breadcrumb. The gateway
  never deletes a different message ID. Accepted Discord `/steer` and busy-mode
  steering append a bounded `⏩ Steer received` marker to the same ordered tool
  progress queue, so the temporary editable message shows where the steer
  arrived relative to tool updates. Rejected, queued, or non-Discord input does
  not create a marker. The turn-scoped queue reference is dropped on release.
  The separate accepted `/steer` acknowledgement repeats the full steered text
  without its former 60-character preview cut; Discord splits long sends. The
  rolling progress marker stays bounded independently.
- **Incident and hypothesis:** A progress message remained in Discord after a
  final answer; the live log shows the answer send at 16:24:46.804 and gateway
  stop at 16:24:46.926. The old callback launched an unawaited task and ignored
  the adapter's `False` delete result. A 503 earlier that turn affected an edit,
  not a proven delete. Source tracing also found two deterministic omissions:
  queued turns returned before registering cleanup, and streamed finals returned
  `None` without marking delivery at the adapter boundary.
- **Touchpoints:** `gateway/progress_compositor.py`, display resolution, turn
  context/runner lifecycle, `gateway/run_turn.py`, `gateway/run_notifications.py`,
  `gateway/platforms/base.py`, the Discord adapter, and focused compositor,
  lifecycle, queued-delivery, progress, cleanup, interruption, overflow, and
  non-Discord isolation tests, and `tests/gateway/test_progress_steer.py`.
- **Verification:** The cleanup-await regression failed RED with `cleanup
  callback returned before Discord deletion finished`; the queued-first-answer
  regression failed RED with `adapter.deleted == []`; the streamed-final
  regression failed RED with `adapter.deleted == []`. Each passed after its
  focused fix. Independent review caught the whole-callback timeout on multiple
  slow IDs and a transformed-stream edit that marked a refused result delivered;
  both regressions failed RED, then passed. The adjacent gate reports
  `138 passed, 2 deselected`; the two excluded queued-media tests assert a
  POSIX `Path.as_uri()` shape on Windows and failed before this cleanup change. A stale queued-ledger test fixture
  omitted the `.text` field required by the earlier composition behavior and
  was updated to match a real event. A slow callback registered earlier in the
  same post-delivery chain can still exhaust the shared 30-second deadline
  before cleanup begins; that chain-level scheduling limit is outside this
  Discord cleanup patch. No live post-restart cleanup observation exists. Runnable check: `uv run --with pytest --with pytest-asyncio pytest -q
  tests/gateway/test_run_cleanup_progress.py tests/gateway/test_queued_final_ledger.py
  tests/gateway/test_discord_composition_buffer.py tests/gateway/test_run_progress_topics.py
  tests/gateway/test_progress_compositor.py tests/gateway/test_display_config.py
  tests/gateway/test_discord_slash_commands.py -k 'not test_run_agent_queued_message_delivers_first_response_media
  and not test_run_agent_queued_message_delivers_streamed_first_response_media'
  --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/cleanup-review-broad-0923`.
- **Steer-marker verification:** The three accepted-steer routes and turn reset
  failed RED with three missing markers and one uncleared queue; the focused
  check passed GREEN (`53 passed`). The adjacent compositor, busy-origin,
  progress, queued-delivery, cleanup, display, and Discord slash suites reported
  `184 passed, 2 deselected` after accounting for a direct-call test that omits
  a generation number. The two deselections are the previously documented
  Windows POSIX-URI assertions. The marker tests cover ordered placement,
  rejection, non-Discord isolation, bounded multiline previews, deduplication,
  and stale-agent isolation. No live post-restart observation exists. Runnable
  check: `uv run --with pytest --with pytest-asyncio pytest -q
  tests/gateway/test_progress_steer.py tests/gateway/test_progress_compositor.py
  tests/gateway/test_busy_steer_origin.py
  --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/steer-final-focused-0924`.
- **Full acknowledgement verification:** A live post-restart Discord steer
  displayed its marker between tool updates in the editable message, and Elliot
  confirmed seeing it. The new acknowledgement regression failed RED on the
  61- and 1900-character payloads (`2 failed, 1 passed`); the multiline case
  passed on baseline. After removing only the 60-character slice, the focused
  steer, compositor, origin, and Discord slash suites reported `75 passed`.
  The Discord send path splits content above 2,000 characters; no live
  post-restart observation of this acknowledgement change exists. Runnable
  check: `uv run --with pytest --with pytest-asyncio pytest -q
  tests/gateway/test_progress_steer.py tests/gateway/test_progress_compositor.py
  tests/gateway/test_busy_steer_origin.py tests/gateway/test_discord_slash_commands.py
  --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/steer-ack-green-0924`.
- **Pre-agent compression extension and manual protected override:** After the
  timing/history/cache, bounded-test, and rollback assessment, Elliot directly
  said `Builds this.` in Discord message `1555368017964302468`, thread
  `1553613254721282169`. This approval covers a configured 120-second pre-agent
  hygiene hold and 30-second Discord progress updates, not summary routing,
  fallback, history truncation, or a global default change. The supported
  `compression.hygiene_max_turn_hold_seconds: 120` remains an operator config
  change, outside this source patch; the existing source default is unchanged.
- **Compression display behavior:** When detached pre-agent hygiene runs with
  Discord `single_message` enabled, its sole initial reply says
  `⏳ Compacting context`. At elapsed 30/60/90 seconds the compositor edits that
  same ID. Early completion adopts the existing compressed transcript plus
  protected tail through the unchanged adoption path. At the configured hold
  boundary, the same ID says
  `Compression still running; continuing with existing context`, replacing
  the former standalone deferral notice. The existing watermark-fenced worker
  keeps admission, while this turn keeps its original history. TurnRunner
  inherits the compositor only for the matching generation, tracks its ID for
  delivery-gated cleanup, and starts normal tool progress without another send.
  Turn release clears the handoff reference. Initial-send and permanent-edit
  failures never create replacement messages. Other transports, compositor-off,
  and native Codex app-server hygiene retain their existing behavior.
- **Compression safeguards and limits:** The original guarded wait is unchanged.
  A cancellable display task keeps slow Discord edits outside that wait and is
  joined on success, expiry, or cancellation. The existing 30-second no-progress
  timeout can release earlier than 120 seconds; live summary progress permits
  the longer configured hold. Existing total ceiling, unfenced cancellation,
  `/stop`/unwind admission revocation, and commit-in-flight adoption remain
  intact. The 120 seconds bounds inline summary waiting, not agent construction,
  Discord acknowledgement/deferral transport latency, or an admitted commit's
  completion. Initial acknowledgements and each compositor edit now have a
  two-second best-effort transport deadline, separate from the summary hold.
  Timeout cancellation runs inline, freezes display, and never retries the send
  or creates a replacement. The updater remains cancelled and joined at release.
  No worker cancellation is added at watermark-fenced hold expiry.
- **Compression deterministic evidence:** Tests-first RED against unchanged
  production at `418e4c699a` returned `4 failed`: three `IndexError: list index
  out of range` assertions prove hygiene posts no immediate breadcrumb, and
  `test_turn_clear_releases_preagent_compositor` proves the missing handoff reset.
  Expanded GREEN covers virtual 12/95-second completion and 30/60/90 updates,
  exact virtual 120-second release with unchanged history and worker admission,
  background completion, same-ID tool handoff and confirmed-delivery cleanup,
  slow/permanent/retryable edits, cancellation and idle guards, and displaced
  generations. A real executor/fence/adoption path feeds real offline AIAgent
  requests: enabled/off Discord and Telegram requests compare data-identically
  across both cache modes, including schemas/options, canonical history,
  byte-stable system decoration and multi-turn resume. No paid model or real
  Discord call is made. Runnable adjacent gate:
  ```text
  uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_compression_progress.py tests/gateway/test_session_hygiene.py tests/gateway/test_session_hygiene_turnhold_adoption.py tests/gateway/test_hygiene_deferred_work_drain.py tests/gateway/test_hygiene_failure_cooldown_ladder.py tests/gateway/test_codex_hygiene_compaction.py tests/gateway/test_progress_compositor.py tests/gateway/test_run_cleanup_progress.py tests/gateway/test_run_progress_topics.py tests/gateway/test_progress_steer.py tests/gateway/test_busy_steer_origin.py tests/gateway/test_turn_lease.py tests/gateway/test_stale_finalize_suppression.py tests/gateway/test_display_config.py tests/hermes_state/test_compression_watermark_commit.py tests/agent/test_prompt_caching.py tests/agent/test_prompt_cache_boundary.py tests/agent/test_hygiene_timeout_cooldown_isolation.py -k 'not test_run_agent_queued_message_delivers_first_response_media and not test_run_agent_queued_message_delivers_streamed_first_response_media' --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/compression-final2-child-1001
  ```
  This gate reports `355 passed, 2 deselected`. Before excluding the two existing
  Windows media-URI assertions, the smaller adjacent gate reports
  `2 failed, 151 passed`. Both exact failures reproduce with the four edited
  production modules loaded directly from `418e4c699a` in an isolated subprocess,
  without rewriting the live checkout (`2 failed`); expected `Path.as_uri()`
  differs from encoded Windows transport paths. They remain out of scope.
  Source/test Python compilation and scoped `git diff --check` pass.
- **Compression review blockers and correction:** Independent review reproduced
  unbounded initial send and deferral edit, stale worker admission after an
  awaited initial send, and a breadcrumb advancing Discord's conversational
  backfill boundary. Five tests-first regressions were added before correction.
  Initial RED returned `5 failed, 12 deselected`: two cache-boundary assertions
  (`'222'` instead of `None`/`'111'`), initial-send `TimeoutError`, stale detached
  worker `Awaited 1 times`, and an incomplete deferral fence fixture. After the
  fixture matched production, deferral-only RED returned `1 failed, 16 deselected`
  with `TimeoutError` at the pending edit. Commands:
  ```text
  uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_compression_progress.py -k 'pending or displacement or breadcrumb_excluded' --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/compression-blockers-red-child-1001
  uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_compression_progress.py -k pending_deferral --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/compression-deferral-red2-child-1001
  ```
  Compositor transport calls use inline `asyncio.timeout(2.0)` with no shield or
  detached send/edit. The initial attempt is latched against duplicate retries.
  Pending-wire tests patch the cap to `.01` and enforce a `.2` outer safety bound;
  they verify cancellation completed before continuation and no replacement send.
  Generation ownership is rechecked after awaited display initialization and
  immediately before detached hygiene admission, without clearing successor refs.
  Initial metadata copies thread routing plus `non_conversational=True`; real
  DiscordAdapter send/backfill/reply scans through mocked wire verify both cold
  and cached partitions preserve prior human context and exclude the breadcrumb.
  Handoff retains the deferral explanation until actual activity arrives.
  The new deferral regression also exposed `RuntimeError: No active exception to
  reraise` after an awaited transport timeout; the hold handler now explicitly
  raises `HygieneTurnHoldExceeded`, preserving its intended availability outcome.
  An intermediate adjacent run (`2 failed, 358 passed, 2 deselected`) caught an
  overbroad generation guard suppressing two Telegram mock-runner hygiene tests.
  Restricting the new guard to the pre-agent compositor path restores transport
  isolation. The exact adjacent command above, with basetemp
  `compression-blockers-adjacent2-child-1001`, reports
  `360 passed, 2 deselected in 107.05s`. Added Discord backfill gate and static
  checks report `23 passed in 1.38s`, compilation success, and clean scoped diff:
  ```text
  uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_discord_free_response.py --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/compression-backfill-adjacent-child-1001
  python -m py_compile gateway/progress_compositor.py gateway/run_turn.py gateway/run_turn_runner.py tests/gateway/test_compression_progress.py
  git diff --check -- gateway/progress_compositor.py gateway/run_turn.py gateway/run_turn_runner.py tests/gateway/test_compression_progress.py PATCH.md
  ```
  Virtual 30/60/90/120 timing tests remain unchanged in their clock model because
  the display deadline uses `asyncio.timeout`, not their patched `wait_for`.
  All regression/adjacent checks are offline. No summary route, fallback, DB
  commit, history, configuration, gateway restart, commit, or push changes.
- **Compression residual risk, activation, and rollback:** Offline deterministic
  checks do not prove nondeterministic summary quality or live Discord delivery.
  Discord throttling can coalesce or suppress elapsed updates; a permanent
  failure leaves the sole breadcrumb frozen. Timeout cancellation relies on the
  adapter's cooperative cancellation; Discord can accept a send before its
  response is lost, leaving an unknown remote ID that cannot be cleaned up.
  That ambiguous initial attempt is never retried. An admitted commit can finish
  beyond the hold by existing design. Parent review reproduces the correction
  regressions and the combined adjacent plus Discord backfill gate:
  `383 passed, 2 deselected in 105.75s`, using the command above plus
  `tests/gateway/test_discord_free_response.py` and basetemp
  `compression-parent-reviewed-final`. The parent sets and reads back the
  supported workspace `compression.hygiene_max_turn_hold_seconds: 120`;
  its one-line configuration change is versioned separately. No gateway restart
  or live activation is performed. Elliot must manually restart the gateway
  to load the progress source. To roll back,
  restore the prior hold setting and apply a reviewed inverse of only these
  compositor/hygiene handoff hunks, then manually restart. Existing sessions and
  archived history need no rewrite. No summary-model/fallback setting changes.
- **Stable-port adapter revision:** TurnRunner and pre-agent hygiene display
  resolve the existing replying transport through `_delivery_adapter_for`.
  No alias, model/history/queue change, or additional timing behavior is added.
  Tests provide upstream `hard_msg_limit=5000` and use current adapter mocks.
  The installed joint gate reports `286 passed, 2 deselected`; the exclusions
  remain the two baseline Windows media-URI expectations described above.
  Replaying the old pre-agent lookup fails all three tested handoff cases.
- **Upstream disposition:** Candidate for upstreaming as an opt-in Discord
  progress lifecycle. Keep active while Elliot relies on immediate acknowledgement
  and delivery-gated cleanup.

### HERMES-FORK-018: Discord queue-mode composition window

- **Intent:** Give one sender a fixed 30-second composition window for ordinary
  Discord text while a session is busy, without changing explicit `/queue` or
  other transports.
- **Behavior:** The first queued Discord text reserves its FIFO position and
  opens a non-sliding 30-second window. Busy queue-mode text bypasses Discord's
  older short ingress batch so each physical message retains its ID. Later
  text from the same sender and thread with matching reply/security fields
  joins that turn before the deadline; evolving history backfill is not a
  grouping boundary. Other senders, context changes, and post-deadline arrivals
  keep distinct FIFO turns. An early-ending active run waits for the full
  window before starting the composed turn. At seal, each Discord message is
  fetched once to include edits, then text is frozen; an edit cannot promote
  ordinary text into a control command. A waiting sealed item reacts ⏳ only
  on its final Discord message; the marker is removed once when its turn
  begins. Buffered turns suppress 👀/✅/❌. Reaction failures do not determine
  queue boundaries. Media, explicit commands, and non-Discord paths retain
  their existing routing.
- **Touchpoints:** `gateway/discord_composition.py`, `gateway/run_busy.py`,
  `gateway/run_turn.py`, `gateway/platforms/base.py`,
  `plugins/platforms/discord/adapter.py`, and
  `tests/gateway/test_discord_composition_buffer.py`.
- **Verification:** RED first reported `2 failed` with missing composition
  module; focused REDs subsequently caught duplicate marker removal, media
  merging, mention normalization, command edits, and reply-context merging.
  Independent review caught the Discord ingress batch joining physical messages
  before composition and evolving thread history splitting the window; both
  regressions reproduced RED (`2 failed`) then passed with adjacent text-batch
  coverage (`45 passed`). A further RED showed the pre-delivery dequeue blocking
  the current answer until the window closed; the seal wait now occurs after
  delivering that answer, with a focused ordering regression.
  Final reviewed focused/adjacent run reports `140 passed` with
  `DISCORD_REACTIONS=true`, including busy-handler integration, Discord ingress
  batching, and queued delivery hooks. Edited modules pass `python -m py_compile`;
  verification uses `uv run --with pytest --with pytest-asyncio pytest` with
  named gateway suites and an external unique `--basetemp`.
  No live post-restart observation exists; the running gateway has not been
  restarted.
- **Upstream disposition:** Candidate for upstreaming as Discord-specific
  queue composition. Keep active while Elliot uses this conversation contract.

### HERMES-FORK-020: Opt-in windowed session heartbeats

- **Intent:** Extend the existing session heartbeat with daily local-time windows
  and an optional current-time stamp, without another scheduler, plugin, tool,
  system-prompt mutation, or model/provider route.
- **Manual protected override:** After the timing/inclusion, stale timestamp,
  duplicate dispatch, session/history/cache, deterministic-test, and rollback
  assessment, Elliot directly approved `approve protected windowed-heartbeat patch`
  on `2026-09-30` in Discord message `1555059909832216667`, thread
  `1550839413808828456`. This approval applies only to this extension.
- **Behavior:** CLI and messaging gateways share prefix-only parsing for
  `/heartbeat every 15m --windows 07:15-09:00,23:00-00:30 --timezone America/New_York --include-time <prompt>`.
  Windows require an IANA timezone and include the entire ending minute.
  Overnight ranges cross midnight; `ZoneInfo` handles DST without changing the
  global environment. Equal endpoints, overlapping/empty/malformed ranges,
  invalid timezones, and unknown/duplicate/missing options fail validation.
  Options persist through restart and compression. Status exposes all options.
  Legacy records omit new default fields and legacy prompt bytes stay identical.
  Elapsed intervals remain elapsed intervals, not quarter-hour slots; busy or
  missed ticks coalesce, real queued users retain idle-boundary priority, and
  closed windows start no heartbeat turn or backlog. Another thread remains
  independent. Gateway ownership admission rechecks the window after preparation
  and hooks; refused execution retains the existing refund path. Its timestamp
  refreshes both copied model-facing and persistence payloads, not merely
  `event.text`. CLI opt-in queue tokens recheck/refund at dequeue preparation and
  stamp then. Workout reminders restate the outstanding question only when that
  instruction is in the user's configured prompt, not a global core template.
- **Touchpoints:** `hermes_cli/heartbeat.py`,
  `hermes_cli/cli_commands_mixin.py`, `hermes_cli/cli_loops_mixin.py`,
  `hermes_cli/cli_process_notifications.py`, `gateway/slash_commands_goals.py`,
  `gateway/run_goals.py`, `gateway/run_heartbeat_acceptance.py`,
  `gateway/run_turn.py`, `tests/hermes_cli/test_windowed_heartbeat.py`,
  `tests/gateway/test_windowed_heartbeat.py`,
  `tests/agent/test_windowed_heartbeat_payload.py`, and
  `website/docs/user-guide/features/heartbeat.md`.
- **Deterministic evidence:** Initial tests-first RED returned `25 failed`:
  missing `parse_heartbeat_spec` and unexpected `windows` constructor/set options.
  Boundary RED returned `3 failed, 25 passed`: option flags became prompt text,
  the prepared payload retained `09:00:05` instead of execution-boundary
  `09:00:59`, and a `09:01:00` closed-window attempt entered the agent runner.
  CLI queue RED returned `4 failed, 25 deselected` on missing `HeartbeatTick`.
  GREEN below returned `247 passed, 1 deselected`. Coverage includes real
  temp-`HERMES_HOME` database I/O and fresh-process reload; daytime/overnight/end
  boundaries and DST gap/fold; duplicate/refund/busy-user/other-thread behavior;
  restart restoration, reset and compression ownership; actual offline AIAgent
  provider-request snapshots with and without caching, multi-turn resume,
  unchanged canonical history/system prefix, schemas, request options, and
  byte/data-identical window-only versus legacy requests. Telegram, Discord,
  Slack handler parity, native Discord commands, TUI legacy heartbeat behavior,
  registry and website parity suites run alongside the new tests. All eleven
  edited Python modules/tests pass `python -m py_compile`; `git diff --check`
  reports no whitespace errors (Git emits its existing PATCH.md CRLF warning).
  ```text
  uv run --with pytest --with pytest-asyncio pytest -q tests/hermes_cli/test_windowed_heartbeat.py tests/hermes_cli/test_heartbeat.py tests/agent/test_windowed_heartbeat_payload.py tests/gateway/test_windowed_heartbeat.py tests/gateway/test_heartbeat_acceptance.py tests/gateway/test_heartbeat_execution_ownership.py tests/gateway/test_heartbeat_poller.py tests/gateway/test_heartbeat_watch_restore.py tests/gateway/test_heartbeat_session_boundaries.py tests/gateway/test_heartbeat_watch_lifecycle.py tests/gateway/test_compaction_heartbeat_gateway_filter.py tests/tui_gateway/test_heartbeat_tui_tick.py tests/hermes_cli/test_commands.py tests/website/test_slash_commands_doc_parity.py tests/gateway/test_discord_slash_commands.py tests/agent/test_prompt_caching.py tests/agent/test_prompt_cache_boundary.py -k 'not test_telegram_parity' --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/window-heartbeat-child-final2
  ```
- **Known baseline failure:** The unfiltered adjacent gate returned
  `1 failed, 167 passed`: `TestSlackNativeSlashes::test_telegram_parity` reports
  `commands on Telegram but missing from Slack native slashes: ['usage']`.
  Running that exact test with all eight edited production modules temporarily
  restored to `HEAD` reproduced `1 failed` with the same missing command. The
  edits were restored byte-identically; no command registry changes are made.
  An initial request invariant incorrectly froze moving Anthropic cache markers;
  the test now checks byte-stable system decoration and canonical prior messages
  while retaining the existing rolling-marker policy. No cache code changed.
- **Parent verification and bounded correction:** Parent review reproduced two
  CLI queue defects: a dropped old tick rewound a replacement heartbeat's counters,
  and a pause/resume mutated the queued state's reference so the stale tick ran.
  New tests returned `2 failed, 35 deselected` before correction. `HeartbeatTick`
  now snapshots admission state, including a separate windows list, and refunds
  only when both persisted and manager state still equal that exact snapshot.
  The first parent correction returns `249 passed, 1 deselected` and `47`
  new tests pass under the isolated clean-environment runner. Independent review
  then identifies legacy gateway dash normalization, gateway pause/clear/replacement
  during preparation, and timezone-only CLI queue admission gaps. Expanded
  regressions return `7 failed, 49 passed`, including a closed-window refund
  following compression. Gateway opt-in claims now snapshot state, check the
  persisted canonical owner's exact instruction at execution, and refund only
  that claim in the resolved lineage. Controls or newer claims remain untouched.
  Legacy no-option gateway commands retain event-parser normalization; an explicit
  `--` boundary remains new syntax. Timezone-only CLI records use queue tokens too.
  The final command above, with basetemp label `window-heartbeat-parent-final-reviewed`,
  returns `260 passed, 1 deselected`. The isolated clean-environment runner reports
  `58 tests passed, 0 failed` across the three new test files:
  `HERMES_PYTHON=<uv pytest interpreter> bash scripts/run_tests.sh -j 1 tests/hermes_cli/test_windowed_heartbeat.py tests/gateway/test_windowed_heartbeat.py tests/agent/test_windowed_heartbeat_payload.py`.
  Parent also reproduces the known Slack `usage` parity failure with the registry
  and its test unchanged. Compatibility-pointer validation passes. The final
  independent, schema-validated read-only review returns `passed: true`, with
  empty security and logic-error lists. No live activation evidence exists.
- **Residual risk and activation:** Offline fake-clock and fake-wire tests do not
  prove nondeterministic model choices, live delivery, abrupt-death refunds, or
  exact wall-clock reminder timing. A window can close after the checked safe
  preparation/execution boundary. No live heartbeat model call or Discord post,
  heartbeat activation, gateway restart, or cron/config/profile/runtime/update-state
  modification is performed. Source, tests, documentation and this entry ship
  together only after the parent review and verification gate.
  The existing sixpack cron `105faa3a80d3` remains untouched and must stay active
  until Elliot explicitly approves its replacement after parent review.
  Activation requires Elliot's **manual gateway restart**, then an explicit
  `/heartbeat ...` command in the intended session and `/heartbeat status`
  readback. To roll back an activated replacement, clear that session with
  `/heartbeat clear` and re-enable existing sixpack cron `105faa3a80d3` if it was
  later disabled. Source rollback belongs to a reviewed inverse source commit
  and another manual restart; legacy heartbeat records remain compatible.
- **Stable-port verification:** The installed heartbeat and adjacent gate
  passes `204` tests, including all `58` feature cases and cached/uncached
  primary-client snapshots. Only obsolete adapter mock names change.
  CLI child imports inherit the process-local installed dependency directory.
- **Upstream disposition:** Candidate for upstreaming as an opt-in extension.
  Keep active while Elliot relies on conversation-local daily reminder windows.

### HERMES-FORK-021: Scoped Discord session-to-session messages

- **Intent:** Let an owned Discord gateway turn send peer content to another
  existing Discord conversation of the same user and profile. Reuse the current
  session resolver, adapter admission and busy FIFO; add no server or general
  model/history/cache/router machinery.
- **Manual protected override:** After the assessment of tool schemas,
  attributed user-role input, idle/busy timing, history/resume/cache isolation,
  deterministic snapshots and bounded rollback, Elliot directly approved
  `Build this` in Discord message `1555367347462602753`, thread
  `1555229315350921223`, on `2026-10-01`. Approval covers only this feature.
  Supported Bot Mode `message_agent` targets canonical profile Bot Chats and
  does not address arbitrary existing Discord sessions.
- **Behavior:** `send_session_message(target_session_id, message)` accepts only
  exact existing current Discord routing IDs belonging to the runtime sender's
  user and profile. Sender attribution, opaque receipt IDs and budgets come
  from a process-local turn capability, never tool arguments, environment
  variables, or a model-authored prefix. Self-targets, missing/suspended routes,
  unavailable adapters, stale sender generations and cross-user/profile/platform
  targets refuse. Compression resolves the sender only through its owned route's
  profile-local database. The new named toolset is folded into owned Discord
  turns only; CLI, other transports, background jobs and delegated execution
  have no runtime capability. Existing tool-search deferral remains unchanged.
- **Delivery contract:** An idle destination schedules its normal adapter turn;
  a busy destination queues a distinct FIFO item, including the runner's pending
  sentinel. No steering, interruption, composition-window merging, direct
  history mutation, or acceptance of Discord self-messages is added. Destination
  text is labelled agent-origin peer content, below real user instructions and
  approvals, and `allow_gateway_control=False`. Cold admission uses existing
  strict route metadata, so it cannot create or reset a missing destination.
  Replies explicitly use the same tool. Concurrent identical target/text calls
  within a turn deduplicate. Automatic chains share eight sends across branches
  and stop at four hops; the next real user turn starts a fresh budget.
  Results distinguish `accepted`, `delivery=started|queued|refused`, and
  `visible`. Started means scheduled, not model execution/completion. Visible
  destination sends have a 15-second deadline and nonconversational metadata;
  a failed visible post does not revoke internal admission or permit a duplicate.
- **Touchpoints:** `gateway/session_messaging.py`, `gateway/run_turn.py`,
  `gateway/run_inbound.py`, `tools/session_message_tool.py`, `toolsets.py`,
  `tests/gateway/test_session_messaging.py`,
  `tests/gateway/test_session_message_lifecycle.py`,
  `tests/gateway/test_session_message_worker.py`,
  `tests/agent/test_session_message_payload.py`, and
  `website/docs/user-guide/messaging/discord.md`.
- **Deterministic evidence:** Tests-first RED reported `1 error during collection`
  on the absent `gateway.session_messaging` module. Removing only the busy guard
  reproduced `1 failed` with `AssertionError: control dispatch`; restoring it
  passed. Removing only the turn-capability wrapper reproduced `1 failed` on
  missing `session_messaging` in the owned Discord turn's schemas; restoring it
  passed. The focused/adjacent gate reports `216 passed`, covering production
  registry dispatch, real temporary SessionStore/SQLite persistence and reload,
  real compression-child publication and unchanged retained transcript, real
  adapter background admission, human-first FIFO, parallel deduplication,
  queue-cap refusal, chain budgets, stale generation, environment/argument
  isolation, delegated denial, explicit disable and Telegram masking.
  Offline real AIAgent provider-request snapshots cover user-role placement,
  multi-turn resume, byte/data-identical unaffected requests, stable prior
  history/system prefix, actual registered tool schemas, existing tool-search
  assembly and request options, with caching on/off. Existing registry,
  distribution, composition, busy-origin and prompt-cache suites pass alongside.
  The isolated clean-environment runner reports `10 tests passed, 0 failed`.
  All edited Python modules/tests pass `python -m py_compile`; scoped
  `git diff --check` exits `0` with only Git's existing PATCH.md CRLF warning.
  GBrain impact tools returned `permission_denied` for agent callers; local
  definition/call-site searches supplied the fallback review, not a claimed
  indexed blast-radius result. Runnable checks (Windows external basetemp):
  ```text
  uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_session_messaging.py tests/agent/test_session_message_payload.py tests/gateway/test_discord_composition_buffer.py tests/gateway/test_busy_wake_admission.py tests/gateway/test_busy_steer_origin.py tests/agent/test_prompt_caching.py tests/agent/test_prompt_cache_boundary.py tests/tools/test_registry.py tests/tools/test_toolsets.py tests/tools/test_toolset_distributions.py --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/session-message-adjacent-child-final
  HERMES_PYTHON=C:/Users/2supe/AppData/Local/hermes/hermes-agent/.venv/Scripts/python.exe bash scripts/run_tests.sh -j 1 tests/gateway/test_session_messaging.py tests/agent/test_session_message_payload.py --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/session-message-isolated-child-final
  python -m py_compile gateway/session_messaging.py gateway/run_turn.py gateway/run_inbound.py tools/session_message_tool.py toolsets.py tests/gateway/test_session_messaging.py tests/agent/test_session_message_payload.py
  git diff --check -- PATCH.md gateway/session_messaging.py gateway/run_turn.py gateway/run_inbound.py tools/session_message_tool.py toolsets.py tests/gateway/test_session_messaging.py tests/agent/test_session_message_payload.py website/docs/user-guide/messaging/discord.md
  ```
- **Reviewer correction and witnessed evidence:** The admission-ID map retained
  chain records when accepted work was cancelled, discarded or rejected before
  the destination bound its turn. At 4096 orphan IDs, unrelated real user turns
  permanently refused sends. Tests-first RED returned `4 failed`: started/queued
  discard cases report `Discarded admissions permanently exhaust the pending cap`,
  missing trusted peer budgets incorrectly reset, and event-owned chain transfer
  was absent. A separate subprocess loading the pre-correction pending-map code,
  without rewriting the live checkout, reproduced real adapter cancellation RED:
  `1 failed, 4 deselected`, with
  `Cancelled admissions permanently exhaust the pending cap`.
  The correction removes the global pending map/cap. A private runtime-only event
  attribute owns the shared chain reference through idle admission and FIFO
  recursion. Discarded events need no cleanup registry, live queued branches never
  expire, and missing/serialized peer capabilities fail closed instead of receiving
  fresh budgets. The eight-message/four-hop limits and new real-user budgets remain.
- **Joint production-worker coverage:** Offline tests execute the actual
  `_run_agent_inner`, owned gateway executor/ContextVar copy, `TurnRunner.run_sync`,
  real cached AIAgent lookup/reuse, AIAgent tool execution, production registry
  dispatch, adapter background admission, inbound preprocessing, FIFO drain and
  recursive destination worker together. Both conversations reuse their agents
  over two turns, with caching on/off; destination requests contain attributed
  user-role peer input and the exact event-owned shared budget. Only credentials,
  test config and provider/Discord wire I/O are faked; outbound socket connections
  are blocked during this integration. Removing the event-capability consumption
  reproduced `2 failed` with `Destination production worker lost trusted chain
  budget`. Removing only FIFO event propagation reproduced
  `2 failed, 2 deselected` with that same assertion. Restoring each passes.
  The expanded adjacent gate reports `225 passed`, and the clean-environment
  per-file runner reports `19 tests passed, 0 failed`. Compilation and scoped
  `git diff --check` pass. Test harness construction initially rebuilt the first
  agent because lazy plugin discovery changed the registry generation; explicit
  discovery before signature calculation preserves the real cache invariant.
  Parent reruns the expanded adjacent gate: `225 passed in 27.61s` with
  basetemp `session-message-parent-corrected`. Independent read-only review
  reruns all 19 feature tests and reports no security or logic findings.
  Its delegation wrapper reports a schema failure after retry; the parent
  separately parses the exact returned partial JSON and validates every
  required key and type, `passed=true`, and empty finding lists. Wrapper status
  is not treated as proof of approval. No live-activation claim is made.
  Runnable correction checks:
  ```text
  uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_session_messaging.py tests/gateway/test_session_message_lifecycle.py tests/gateway/test_session_message_worker.py tests/agent/test_session_message_payload.py tests/gateway/test_discord_composition_buffer.py tests/gateway/test_busy_wake_admission.py tests/gateway/test_busy_steer_origin.py tests/agent/test_prompt_caching.py tests/agent/test_prompt_cache_boundary.py tests/tools/test_registry.py tests/tools/test_toolsets.py tests/tools/test_toolset_distributions.py --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/session-fix-adjacent-green
  HERMES_PYTHON=C:/Users/2supe/AppData/Local/hermes/hermes-agent/.venv/Scripts/python.exe bash scripts/run_tests.sh -j 1 tests/gateway/test_session_messaging.py tests/gateway/test_session_message_lifecycle.py tests/gateway/test_session_message_worker.py tests/agent/test_session_message_payload.py --basetemp=C:/Users/2supe/AppData/Local/Temp/hermes-pytest/session-fix-isolated-green
  ```
- **Residual risk and rollback:** Offline tests cannot prove nondeterministic
  model authority handling, live Discord transport or eventual completion.
  Admission receipts, deduplication and chain budgets are process-local, not a
  durable message bus. A reset/deletion/shutdown can discard admitted work.
  Admitted events retain their own runtime budgets until execution or discard;
  there is no process-global unconsumed-ID registry. Existing FIFO limits still
  bound queued work. Disable with
  `agent.disabled_toolsets: [session_messaging]` at a deliberate new-session
  boundary and manual gateway restart, or use a reviewed inverse source commit.
  No config/profile/cron/live runtime/update-state write, paid model request,
  real Discord traffic, gateway restart, commit or push is performed by the
  implementation subagent. Source awaits parent review and activation by a
  manual restart; there is no live post-restart evidence.
- **Stable-port adapter revision:** `SessionMessenger._send` now resolves the
  same destination through upstream `_delivery_adapter_for(dst)`. The deleted
  API previously returned a tool error before destination admission. Routing,
  authority, event-owned capability, budgets, schemas and FIFO semantics remain
  unchanged. Persisted installed production passes `211` messaging tests and
  `64` adjacent cleanup/queue tests. Actual idle/FIFO worker dispatch and
  cached/uncached primary-client snapshots run without source substitutions.
- **Upstream disposition:** Candidate for upstreaming as a session-scoped
  Discord edge capability. Keep active while Elliot relies on this contract.

## Stable-release source audit, 2026-10-02

The completed rebase targets `v2026.9.24`, exact upstream commit
`f97608f178d1ffeca59860195ab7da295f7c8e5f`, from source release `v2026.9.14`.
Recovery ref `backup/pre-update-20261002-151329` preserves
`1b39b230abc0a0d4dcacaccdedda0f031a46f8da`. All 49 rebase steps complete;
no commits are explicitly skipped.

- **Keep:** `002`, `003`, `004`, `008`, `011`, `013`, `014`, `015`, `016`,
  `018`, `020`.
- **Revise:** `001`, `005`, `006`, `007`, `009`, `010`, `012`, `017`, `021`.
- **Retire:** `019`, whose selected-upstream implementations are equivalent.
- **Verification:** Sixteen parsed passing JUnit gates contain `1350` distinct
  passing test identities and two Linux-only skips. Counts are deduplicated by
  test classname/name, not added across overlapping gates. The existing Windows
  media-URI, POSIX permission-mode and privileged launcher exclusions remain
  disclosed in their matching entries. An attempted monolithic 74-suite run
  times out at the tool's 420-second limit without JUnit; it has no pass claim.
- **Installation boundary:** Source verification does not complete installation.
  The updater checkpoint stays pending until the normal continuation pipeline
  finishes. On Windows, `--no-gateway-restart` still pauses running gateways
  before dependency work. The operator must run continuation manually; the
  owning agent must not stop/restart its gateway or remove the live recovery
  checkout. After continuation, verify installation, consume the complete
  per-patch evidence with the audit completion tool, and verify live readiness.

## Consolidated local operating record

`PATCH.md` is the sole human-facing registry for this maintained fork. It
contains active behavior contracts, source-change history, verification
requirements, incident context, and explicit operational decisions. Scheduler
runtime state, logs, and temporary audit payloads remain machine data rather
rather than competing registries.

### Operator workflow skills

The behavior contracts live in this fork. The procedures for maintaining this
specific installation live in the paired Hermes workspace, so they survive a
source rebase and do not become upstream product behavior:

- [`hermes-fork-change`](https://github.com/ElliotDrel/Hermes-Workspace/blob/main/skills/software-development/hermes-fork-change/SKILL.md) is required before every installed-source edit. It contains the source-fork commit, diagnosis, verification, and `PATCH.md` evidence contract.
- [`hermes-fork-update`](https://github.com/ElliotDrel/Hermes-Workspace/blob/main/skills/software-development/hermes-fork-update/SKILL.md) is required only for an update, rebase, or patch audit.

### Standalone update workflow, 2026-10-02

Elliot approved the replacement in Discord message `1555706950489210944`:
two skills plus deterministic preparation and activation scripts. This changes
the workspace maintenance procedure, not the product `/update` implementation.

- `hermes-fork-change` owns source edits, customization intent, focused tests,
  the matching ledger entry, and source commits. The update skill owns sequence.
- `hermes-fork-update/scripts/prepare.py` pins the official published stable,
  verifies remote recovery refs, rebases an isolated candidate worktree, and
  binds passing existing tests to its committed SHA without changing the live
  source or dependencies. Git retains unresolved conflicts.
- `hermes-fork-update/scripts/activate.py` uses an external stdlib guardian and
  fresh children for upstream install, maintenance, and lifecycle stages. It
  installs that tested SHA, verifies a fresh gateway, storage, and connected
  platforms, then publishes with the inspected remote lease. Failures restore
  source, snapshotted dependencies, and WAL-safe home state before rechecking
  the original installation. Dependencies and state are copied after pause
  so a live lazy install cannot race the recovery snapshot.
  Failed-candidate home state is preserved before schema/state restoration;
  SQLite sidecars and post-snapshot files are handled from verified archives.
  Known installer lockfile churn is archived before restoration, while unknown
  source changes stop recovery without discarding them. A healthy install with
  failed Git publication remains running as `installed_unpublished`; the same
  helper's `--publish` reconciles it without another lifecycle cycle.
- The core installer is upstream's stdlib-only `_install_repair.run_core_install`.
  Importing the normal CLI first maps `brotlicffi` on this Windows host and
  triggers the native self-lock guard. The core stage runs before those imports,
  requires managed uv explicitly targeting the project venv, and refuses a
  fallback that would install into the external guardian's system interpreter.
- The obsolete workspace `complete_audit.py` and per-patch JSON handshake are
  removed. `fork-update-state.json` is preserved as historical evidence and is
  neither trusted nor cleared by the replacement. The earlier source-audit
  installation instructions above describe the legacy attempt, not this flow.
- Existing product updater, gateway scheduling, and shutdown hooks stay active
  and unchanged. Retiring those protected source paths needs its own data-flow
  assessment and explicit manual override. The owning gateway agent cannot
  launch activation when its standing lifecycle permissions forbid it.
- Helper tests use isolated real Git, state, and dependency directories with
  simulated lifecycle/installer boundaries. Read-only native probes check the
  upstream API seam and external-interpreter install targeting. These checks
  do not establish live installation, detached gateway survival, or reconnect;
  no live update, dependency mutation, config change, or restart occurs here.
  Run `python -m unittest discover -s skills/software-development/hermes-fork-update/scripts -p test_helpers.py -v`
  from the paired workspace and inspect a real activation's `result.json` before
  reporting an update complete.
  The final helper selection reports `16 tests` passing, including real Windows
  detached-child survival after launcher exit, stable-only worktree preparation,
  resumable conflicts, stale evidence invalidation, installer-churn recovery,
  SQLite-sidecar removal, and publication reconciliation without lifecycle work.
  Read-only native probes confirm API compatibility, explicit managed-uv target,
  and external/project interpreter ABI. Claude's one implementation review found
  stale test expectations, dirty rollback, state preservation, publication
  rollback, and candidate-targeting defects; those concrete findings were
  corrected and covered by the final helper gate, without repeated review loops.
- **Scoped lifecycle follow-up:** A late feasibility lookup exposed that the
  first workspace helper called fleet-wide pause/resume APIs despite its
  default-profile-only contract. The replacement uses only the home-scoped
  socket pause, update marker, wait-only exit, and detached-spawn primitives.
  It refuses named/dead profiles, services, foreign/unmapped gateways, shared
  venv holders, and unsupported interpreter layouts before stopping or changing
  dependencies. Inventory is rechecked before installation and recovery;
  original recovery obligations survive failed ACKs, quiesce, and resume.
  No force-kill or fleet lifecycle call belongs in this seam.
  One bounded Claude Opus 5.5 review confirms scoped calls and identifies an
  insufficient lost-ACK recovery budget. Parent RED reports `1 failure, 1 error`:
  `150 not greater than or equal to 1030` and the absent phase-deadline helper.
  Before requesting stop, the fallback now includes the upstream update
  after-turn cap plus normal/cron drain and teardown grace. Lifecycle child
  deadlines refresh from the persisted budget rather than using a shorter fixed
  cap. The final parent helper gate reports `32 tests in 128.308s`, all passing.
  Module and embedded-bridge compilation and scoped whitespace checks pass.
  Lifecycle tests use fixture APIs, including the real bridge fragment; native
  live probes are not run because CLI imports can perform recovery mutations.
  No live activation/restart, installed product code, config, history, or legacy
  checkpoint change is made. Live installation and reconnect remain unproven.

### Shared host conventions

- On this Windows host, run pytest with a unique external
  `--basetemp="C:/Users/2supe/AppData/Local/Temp/hermes-pytest/<label>"`.
  The default pytest location fails with `WinError 5`.
- A Windows virtual-environment gateway appears as a small venv-Python parent
  and a uv-Python child. Treat `hermes gateway status` and gateway state as the
  authoritative runtime identity. Do not terminate the venv parent as a
  duplicate gateway.
- Hermes' default runtime remains the standing Discord agent. Codex App-Server
  is an explicit per-session option because it bypasses the normal provider
  failover and credential-pool path.
- The active auxiliary policy is
  `auxiliary.allow_provider_discovery_fallback: false`. Title generation stays
  explicitly pinned to the configured Luna route.

### Local incident index

The following historical local records are consolidated here. The cited active
patch is the maintenance and verification authority; the old `HERMES-LOCAL-*`
labels are retained only for searchable history.

- **HERMES-LOCAL-001, Discord drafts entered agent context:**
  `HERMES-FORK-002` drops `draft` markers before dispatch and history backfill.
- **HERMES-LOCAL-002, 008, 009, and 013, Discord command sync:**
  `HERMES-FORK-003` owns startup policy propagation, unresolved application-ID
  safety, bounded reconciliation, and resumable incomplete sync.
- **HERMES-LOCAL-003, Discord auto-thread 429 amplification:**
  `HERMES-FORK-004` owns the parent-channel cooldown and no-fallback contract.
- **HERMES-LOCAL-004, 007, 012, and 019, manual Discord rename:**
  `HERMES-FORK-012` owns native registration, active-turn dispatch, semantic
  title generation, explicit titles, rollback, and actionable Discord errors.
- **HERMES-LOCAL-005, Windows venv dependencies:**
  `HERMES-FORK-006` exposes the venv package directory to the gateway runtime.
- **HERMES-LOCAL-006, 015, 016, and 017, response footer and quotas:**
  `HERMES-FORK-005` owns durable footer metadata. `HERMES-FORK-007` owns
  duration-based subscription window labels and preserves cron `[SILENT]`.
- **HERMES-LOCAL-010, Windows search patterns:** `HERMES-FORK-015` keeps regex
  and glob values separate from path translation.
- **HERMES-LOCAL-011, Windows cron Bash:** `HERMES-FORK-011` selects verified
  Git Bash and rejects WSL launchers for native script paths.
- **HERMES-LOCAL-014, real-profile Chrome attachment:** `HERMES-FORK-014`
  preserves copied-profile isolation and direct-CDP recovery.
- **HERMES-LOCAL-018, Discord monitor log rotation:** retired with its
  recurrence monitor. `HERMES-FORK-003` and `HERMES-FORK-004` remain covered
  by their source tests, without a recurring log monitor.

### Retired operational watches and monitors

The following non-patch monitors were intentionally retired by Elliot on
2026-09-19. They have no active cron job, monitor script, or recurring audit.
Recreate a new watch only when fresh production evidence warrants it.

- **HERMES-WATCH-001, Codex long-context pre-stream failures:** retired. No
  local fix was claimed; the prior upstream report was NousResearch/hermes-agent
  issue `#103673`.
- **HERMES-WATCH-002, Windows gateway restart handoff:** retired. No local fix
  was claimed. The maintained-fork update/restart contracts remain in
  `HERMES-FORK-001`.
- **Discord auto-thread regression monitor:** retired. Its former 30-minute
  cron job and deterministic script were removed. Source regressions remain the
  verification mechanism when the affected code changes.

### Legacy update-audit evidence lifecycle

This describes the older product-hook workflow, not the standalone replacement.
`HERMES-FORK-001` uses `fork-update-state.json` as the machine checkpoint for
an active update. It records source and target releases, backup and rebase
heads, and whether the required audit remains pending. A per-patch evidence
payload exists only while `complete_audit.py` validates a pending audit. On a
successful audit, the payload is consumed and removed. The checkpoint retains
only the audited status, exact fork head, patch count, summary, and timestamp.

### Workspace extensions and deferred work

- **Model context suffix:** The profile-local `model-context-suffix` plugin adds
  the active model, provider, context usage, compaction count, and recognized
  quota windows to ordinary final responses. It must preserve a terminal
  `[SILENT]` response unchanged so cron delivery stays suppressed. Its installed
  source bridges are maintained by `HERMES-FORK-005` and `HERMES-FORK-007`.
- **Cron manifest:** `cron/manifest.json` is the reviewable declaration of
  scheduled jobs. `cron/runtime/` is mutable scheduler state and is intentionally
  excluded from the workspace repository. `HERMES-FORK-010` maintains that split.
- **GitHub filing tracker:** `scripts/github_filing_tracker.py` remains a
  read-only workspace integration. It stores durable confirmed goals separately
  from generated runtime state and never mutates GitHub without per-item approval.
- **Windows restart handoff:** The former unresolved watch is retired. Do not
  restart the gateway from its owning agent process. If fresh evidence shows an
  unpaired shutdown, investigate the detached handoff and restore evidence-based
  monitoring rather than relying on this historical note.
- **Discord completion cleanup:** Implemented by `HERMES-FORK-017`; cleanup owns
  only the temporary progress ID and is gated on confirmed final delivery.
- **Discord active-thread archive backfill:** Deferred administration. A future
  implementation must modify only Elliot-owned active threads and read each
  resulting archive duration back.

## Retired patches

### HERMES-FORK-019: Codex GPT-6 catalog context fidelity

- **Intent:** Budget Sol and Luna from the account-scoped Codex catalog, with
  newest-client-first discovery and conservative 272,000-token fallback.
- **Retirement:** Official stable `v2026.9.24` contains both original contracts.
  The catalog helper requests `99.0.0` first and falls back to `0.0.0` after
  rejection or an empty response. Live slug context wins over static fallback.
  Sol and Luna both have the 272,000-token Codex fallback.
- **Evidence:** `agent/model_metadata.py` and `hermes_cli/codex_models.py` are
  byte-identical to the selected upstream commit after the rebase. Their focused
  suites pass within the parent metadata gate. The old patch's historical
  diagnosis and verification remain in the pre-update backup and Git history.
- **Scope:** Retire the source deviation. Keep Elliot's independent workspace
  85% compression preference unchanged. No model configuration or session rewrite.
