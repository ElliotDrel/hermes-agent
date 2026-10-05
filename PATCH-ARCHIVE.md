# Retired Hermes customizations

These are design notes, not active requirements. Elliot selected these retirements
during the October 2026 stable-based cleanup. Do not restore them automatically.
Active behavior belongs in PATCH.md. This file contains no code, patch payloads or
recovery-commit inventory; reconsider the intent against current Hermes before
implementing anything again. Existing historical backups are left untouched.

## 001 / 016: Previous updater implementations

**Intent:** Preserve custom features across stable updates and report results in Discord.
**Previous design:** Custom update hooks and workspace installation helpers coordinated
candidate checkouts, dependency snapshots, gateway shutdown, recovery, audit checkpoints
and publication. Earlier notification routing could retain a previous update thread.
**Retired because:** Multiple owners of installation and recovery made updates harder
to understand and maintain. Old CLI interception also displaced native updater behavior.
**Replacement:** A two-branch Git preparation workflow, review in the current Discord
thread, and native Hermes installation/drain/restart. Current replacements retain IDs
001 and 016 in PATCH.md; their behavior is not retired.

## 003: Previous Discord command-sync policy implementation

**Intent:** Avoid repeated command registration on reconnect and let legitimate paced
reconciliation finish without exhausting command-management limits.
**Previous design:** Startup-only policy, application-scoped convergence state, resumed
incomplete reconciliation and per-request deadlines around command fetch/mutations.
**Retired because:** Stable already implements much of the convergence and rate-limit
handling. Reimporting the full older mechanism would duplicate it.
**Replacement:** Diagnose the remaining timeout on stable and retain only a demonstrated
correction under active 003. This archives the former implementation, not its intent.

## 006: Windows venv path overlay

**Intent:** Make gateway dependencies importable on Windows.
**Previous design:** Added the venv site-packages directory to the generated VBS
launcher's PYTHONPATH, alongside the native virtual-environment interpreter.
**Retired because:** Native Hermes already selects the venv interpreter, and the current
gateway loads its dependencies and MCP tools. Reconsider only if an actual supported
launcher path reproduces missing imports; do not add an overlay preemptively.

## 008: Restricted Discord cron continuation targets

**Intent:** Prevent a continuable cron job from unexpectedly opening its own thread.
**Previous design:** Additional create/update validation and tool guidance required an
explicit numeric Discord thread destination for session attachment. Ordinary channel
delivery remained non-continuable. Numeric validation did not prove a target's type.
**Retired because:** Elliot chose native cron targeting behavior. Reconsider if implicit
continuation threads again conflict with the intended workflow.

## 009: Strict auxiliary discovery override

**Intent:** Keep auxiliary model work on the selected provider chain.
**Previous design:** An additional configuration switch stopped credential discovery
after configured auxiliary/main/fallback routes were exhausted.
**Retired because:** Stable already prevents unrelated discovery when a main provider
is explicitly selected, as in this installation. Reconsider the uncovered auto/unselected
provider case only if this installation starts using it.

## 010: Separate cron runtime directory

**Intent:** Separate authored cron definitions from mutable scheduler data.
**Previous design:** Redirected jobs, ledgers, output, SQLite stores, locks and telemetry
under cron/runtime, with migration and backup/restore adaptations across scheduler modules.
**Retired because:** The folder split required substantial storage and migration changes.
Native cron layout is simpler and was deliberately restored. Do not move existing stores
back merely because these notes describe the old organization.

## 011: Special Bash selection for Windows cron

**Intent:** Avoid launching WSL Bash with native Windows script paths.
**Previous design:** Shell-script cron reused the local terminal's verified Git Bash
resolver and rejected System32/Sysnative launchers.
**Retired because:** Elliot chose not to carry it; no current failing shell-script job
was established during review. Reconsider on a reproduced cron interpreter mismatch.

## 014: Additional Chrome real-profile attachment behavior

**Intent:** Safely reuse browser profiles and recover from wrapper attachment failure.
**Previous design:** The remaining custom difference allowed visible headed launches
and used a verified direct-CDP connection when wrapper attachment failed. Profile-copy
isolation and endpoint verification already exist in stable.
**Retired because:** Elliot now uses a different Chrome extension and its customizations.
Do not restore the older browser path without confirming it is still the desired workflow.

## 019: Codex model catalog and context fidelity

**Intent:** Use account catalog context limits, with current-client discovery and a
conservative fallback when discovery fails.
**Previous design:** Adjusted catalog request version preference and model metadata
fallbacks, letting live catalog information take precedence.
**Retired because:** Selected stable already contains equivalent behavior. No local
implementation remains necessary; independent compression preferences are unaffected.

## 020: Heartbeat time windows and timestamps

**Intent:** Run conversation heartbeats only within selected daily local-time windows.
**Previous design:** Extended native heartbeat parsing and persisted state with windows,
IANA timezone and optional current-time text. CLI/gateway admission rechecked the window
before execution, with existing scheduling and queue priority preserved.
**Retired because:** Elliot chose native heartbeat behavior for this setup. Reconsider
only when windowed conversation check-ins are wanted; no separate scheduler is needed.
