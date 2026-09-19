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
  ledger. Conflicts remain as an active Git rebase and resume through
  `hermes update --continue`; `--abort` restores the recorded backup without
  deleting it.
- **Touchpoints:** `hermes_cli/fork_update.py`, `hermes_cli/update_cmd.py`,
  `hermes_cli/subcommands/update.py`, `hermes_cli/config_defaults.py`, and the
  focused updater tests.
- **Verification:** Isolated repositories exercise stable-tag selection while
  ignoring a newer untagged upstream commit, shallow-checkout history repair,
  clean rebase, conflict pause, resolved continuation, abort restoration,
  ancestry checks, explicit lease push, and the exit-`3` boundary before
  post-update work.
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
- **Behavior:** `discord.command_sync_policy: startup` permits one slash-command
  synchronization per Discord application in each gateway process, then skips
  reconnect-time syncs until the gateway restarts.
- **Touchpoints:** `plugins/platforms/discord/adapter.py`, focused Discord
  connection tests, and Discord configuration documentation.
- **Verification:** Focused tests cover initial sync, reconnect suppression,
  adapter replacement, and the fresh-process boundary.
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

## Retired patches

None.
