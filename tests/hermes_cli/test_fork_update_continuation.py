"""Exercise continuation through the real checkout planner, without live I/O."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from hermes_cli import update_cmd, fork_update, gitlock, update_handoff


@pytest.mark.parametrize("continued", [False, True])
def test_zero_origin_commits_continuation_does_not_start_another_rebase(
    monkeypatch, tmp_path, continued
):
    sync = Mock(return_value=True)
    if continued:
        sync.side_effect = AssertionError("Continuation started a second fork rebase")
    facade = SimpleNamespace(
        PROJECT_ROOT=tmp_path,
        _run_pre_update_backup=Mock(return_value=None),
        _pause_windows_gateways_for_update=Mock(return_value=None),
        _resume_windows_gateways_after_update=Mock(),
        _is_windows=lambda: False,
        _resolve_update_branch=lambda args: "main",
        _warn_orphaned_update_autostashes=Mock(),
        _stash_local_changes_if_needed=Mock(return_value=None),
        _sync_with_upstream_if_needed=sync,
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: facade)
    monkeypatch.setattr(update_cmd, "_fork_update_preflight", lambda args: continued)
    opts = SimpleNamespace(gw_input_fn=None, assume_yes=True, switch_branch=False,
                           active_lazy_features=[], active_tool_dependencies=[],
                           discard_local_changes=False, keep_stash=False,
                           no_gateway_restart=False)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *args: opts)
    monkeypatch.setattr(update_cmd, "_begin_update_receipt_and_plan", lambda args: None)
    monkeypatch.setattr(update_cmd, "_record_pre_update_backup_outcome", lambda *args: None)
    monkeypatch.setattr(update_handoff, "adopt_handed_off_gateway_resume", lambda: None)
    monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda path: False)
    monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda: (False, ["git"], True))
    monkeypatch.setattr(update_cmd, "_apply_parked_branch_guard", lambda *a, **kw: (False, False, None))
    monkeypatch.setattr(update_cmd, "_is_shallow_checkout", lambda *a: False)
    monkeypatch.setattr(update_cmd, "_current_branch_name", lambda *a, **kw: "main")
    monkeypatch.setattr(update_cmd, "_capture_head_sha", lambda *a: "rebased")
    monkeypatch.setattr(update_cmd, "_count_commits_between", lambda *a: 0)
    monkeypatch.setattr(update_cmd, "_git_run", lambda cmd, args, **kw:
                        subprocess.CompletedProcess(args, 0, stdout="0\n", stderr=""))
    for name in ("clear_stale_git_locks", "clear_stale_tmp_packs",
                 "repair_broken_shallow_boundaries", "prune_stale_shallow_grafts"):
        monkeypatch.setattr(gitlock, name, lambda *args: [])
    continuation = Mock(return_value=fork_update.ForkSyncResult(
        verified=True, changed=True, needs_audit=True, pre_update_head="before"))
    monkeypatch.setattr(fork_update, "continue_fork_rebase", continuation)
    finish_current = Mock()
    apply_pulled = Mock()
    pull = Mock(return_value="rebased")
    monkeypatch.setattr(update_cmd, "_finish_already_up_to_date", finish_current)
    monkeypatch.setattr(update_cmd, "_pull_updates", pull)
    monkeypatch.setattr(update_cmd, "_apply_pulled_update", apply_pulled)

    update_cmd._cmd_update_impl(SimpleNamespace(), gateway_mode=False)

    if continued:
        continuation.assert_called_once()
        sync.assert_not_called()
        finish_current.assert_not_called()
        pull.assert_called_once()
        apply_pulled.assert_called_once()
        args = apply_pulled.call_args.args
        assert args[2] == "before"
        assert args[3].fork_pre_update_sha == "before"
        assert args[3].commit_count >= 1
    else:
        continuation.assert_not_called()
        sync.assert_called_once()
        finish_current.assert_called_once()
        apply_pulled.assert_not_called()
    facade._pause_windows_gateways_for_update.assert_called_once()
