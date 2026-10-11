# Coordinated fork integration

Integrate the completed feature pins without advancing official stable.

- Captured published live/delegation: `ef7f73edd8198ac79a2b18640af5089dace17150`.
- Discord input/cleanup ownership: `2d87fff9a769ceaa581ec5bc90973d9d695166cb` (merged).
- Confirmed rename transactions: `72900731c7d713d1ab5b89819c7d9dcb823c9d9d` (merged).
- Notification/staging documentation: `80eb6f490fd36640f3d0ddd0de653782dd8cb5e0` (docs-only cherry-pick; preserve later schema/delegation contracts).
- Schema and progress changes already belong to captured live ancestry; do not duplicate them.

Source integration target: `52312d15ef2b90fc51bdfcf2dbb4065f0a594175`. This record changes documentation only. No new source behavior, database migration, stable rebase or runtime repair.

## Independent integrated verification

Own worktree `.venv`, temporary homes and scratch paths, no runtime credentials or dependency mutation. Canonical per-file runner with serial workers and retries disabled:

- 46 files: 908 passes, zero assertion failures, three staging fixture setup errors; exit 1. Do not treat the runner's zero-failure headline as a successful run. Two explicitly named previously established Windows baseline exclusions: `test_tool_defs_cache_key_sees_config_replacement_with_pinned_mtime` and `test_windows_location_vars_passed_without_secrets`.
- 18 disjoint adjacent ownership/restart/delivery files: 281 passes, zero failures, exit 0.
- Staging file rerun: all six pass, exit 0, 104.41 seconds. Direct pytest is used for this environment-specific verification. Shell-scoped `UV_PYTHON` points to this worktree interpreter; unset `UV_MANAGED_PYTHON` and `UV_PYTHON_INSTALL_DIR`. The canonical runner sanitizes environment, so its fixture's bare `uv lock` otherwise discovers an inaccessible managed Python 3.14 path (`WinError 448`). No test/source changes or assertion exclusions for staging.
- Syntax parsed for all 25 changed Python files; scoped `git diff --check` passed.
- 64 distinct files covered; duplicate staging passes must not be added as distinct tests. Original error run remains diagnostic evidence, not a green claim.

Exact lists/commands are retained locally in `.scratch/test-files.json`, `.scratch/test-command.json`, and `.scratch/adjacent-command.json`; do not commit test scratch or production state.

## Activation boundary

Existing staged candidate `ac297a4c4c6ccd2b3e26a7076e67809fa46a7346` and marker are preserved. They do not contain this full integration. Reconcile that pin explicitly before staging the final published integration. Never activate the stale candidate by accident. Native stage accepts only an exact published SHA; agent preparation never grants drain/restart permission. Elliot alone issues a new `/restart`, after which installed/loaded SHA, native receipt, Discord reconnection and MCP schemas must be checked.

Reported accidental earlier runtime dependency sync is not repaired by this integration. Affected named distributions were observed present and old gateway connected; full before/after drift and restoration actor are unknown. The final candidate must have its own fresh locked dependency environment and declared optional dependencies/import probes.

Graceful restart drains accepted FIFO only within native budgets. Hard-interruption residual recovery depends on enabled/healthy Discord backfill and readable history, not unconditional crash persistence. Rename retains documented non-atomic Discord name races and explicit uncertainty reporting.
