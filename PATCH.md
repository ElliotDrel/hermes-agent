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

### HERMES-FORK-001: Resumable PatchMD fork updates

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
  command; `--abort` restores the recorded backup without deleting it.
- **Touchpoints:** `hermes_cli/fork_update.py`, `hermes_cli/update_cmd.py`,
  `hermes_cli/subcommands/update.py`, `hermes_cli/config_defaults.py`,
  `gateway/control_socket.py`, `gateway/run.py`, `gateway/run_shutdown.py`,
  `gateway/run_notifications.py`, and the focused updater, control-socket, and gateway-notification tests.
- **Verification:** Isolated repositories exercise stable-tag selection while
  ignoring a newer untagged upstream commit, shallow-checkout history repair,
  clean rebase, conflict pause, resolved continuation, abort restoration,
  ancestry checks, explicit lease push, and the exit-`3` boundary before
  post-update work. Gateway notification regressions cover both live streaming
  and restart-recovery delivery of the PATCH.md audit handoff.
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
- **Verification:** Focused tests cover YAML propagation, initial sync, missing
  application IDs, converged reconnect suppression, resumable incomplete
  reconciliation, the short outer timeout, and the fresh-process boundary.
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
- **Upstream disposition:** Candidate for upstreaming as richer output-hook
  metadata. Keep active while the workspace footer consumes these fields.

### HERMES-FORK-006: Windows gateway venv dependencies

- **Intent:** Let the Windows gateway import dependencies installed in the
  Hermes project virtual environment, including local speech transcription.
- **Behavior:** The generated VBS launcher places both the source root and the
  venv `Lib/site-packages` directory on `PYTHONPATH` before starting the uv
  base interpreter.
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

### HERMES-FORK-012: Semantic session titles and manual rename

- **Intent:** Produce concise T3-style conversation titles and let a Discord
  user deliberately regenerate or replace the active session/thread title.
- **Behavior:** Title generation follows the customized semantic prompt;
  `/rename [title]` regenerates or sets the title, updates session metadata,
  renames the Discord thread, works during an active run, and surfaces native
  rename failures instead of silently hiding them.
- **Touchpoints:** Title generation, gateway slash and mid-run dispatch,
  command metadata, the Discord adapter, and focused title/rename tests.
- **Verification:** Focused regressions cover generated and explicit titles,
  active-run dispatch, native command registration, metadata persistence,
  thread rename, and diagnostic error propagation. The split command architecture
  and current upstream title-generator API are revalidated whenever this maintained
  workflow is restored after an upstream update.
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

- [`hermes-fork-change`](https://github.com/ElliotDrel/Hermes-Workspace/blob/main/skills/software-development/hermes-fork-change/SKILL.md) defines the source-fork commit and `PATCH.md` contract.
- [`hermes-fork-update`](https://github.com/ElliotDrel/Hermes-Workspace/blob/main/skills/software-development/hermes-fork-update/SKILL.md) resumes and audits a maintained-fork update.
- [`local-hermes-source-modification`](https://github.com/ElliotDrel/Hermes-Workspace/blob/main/skills/autonomous-ai-agents/local-hermes-source-modification/SKILL.md) explains how to diagnose, change, and verify installed source.
- [`local-hermes-issue-verification`](https://github.com/ElliotDrel/Hermes-Workspace/blob/main/skills/autonomous-ai-agents/local-hermes-issue-verification/SKILL.md) defines the evidence record that belongs in the relevant `PATCH.md` entry.

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

### Update-audit evidence lifecycle

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
- **Discord completion cleanup:** Deferred feature. Any future cleanup must
  retain final responses and user messages, and prove it cannot delete an error
  or final answer.
- **Discord active-thread archive backfill:** Deferred administration. A future
  implementation must modify only Elliot-owned active threads and read each
  resulting archive duration back.

## Retired patches

None.
