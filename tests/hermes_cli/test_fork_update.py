"""Behavior tests for the maintained-fork rebase update workflow."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.subcommands.update import build_update_parser


GIT_CMD = ["git", "-c", "core.longpaths=true"]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-c", "core.longpaths=true", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"git {' '.join(args)} failed in {cwd}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _configure_author(repo: Path) -> None:
    _git(repo, "config", "user.name", "Hermes Fork Test")
    _git(repo, "config", "user.email", "hermes-fork-test@example.invalid")


def _build_diverged_repositories(
    tmp_path: Path, *, conflict: bool, shallow: bool = False
) -> dict[str, Path | str]:
    upstream = tmp_path / "upstream.git"
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    upstream_work = tmp_path / "upstream-work"
    fork_work = tmp_path / "fork-work"
    checkout = tmp_path / "checkout"

    _git(tmp_path, "init", "--bare", str(upstream))
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _configure_author(seed)
    (seed / "hermes_cli").mkdir()
    (seed / "hermes_cli" / "__init__.py").write_text(
        '__version__ = "0.20.6"\n__release_date__ = "2026.8.27"\n',
        encoding="utf-8",
    )
    (seed / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "hermes_cli/__init__.py", "shared.txt")
    _git(seed, "commit", "-m", "baseline")
    _git(seed, "tag", "-a", "v2026.8.27", "-m", "stable 0.20.6")
    base_sha = _git(seed, "rev-parse", "HEAD").stdout.strip()
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "remote", "add", "upstream", str(upstream))
    _git(seed, "push", "origin", "main")
    _git(seed, "push", "upstream", "main")
    _git(seed, "push", "upstream", "refs/tags/v2026.8.27")

    _git(tmp_path, "clone", "-b", "main", str(origin), str(fork_work))
    _configure_author(fork_work)
    if conflict:
        (fork_work / "shared.txt").write_text("fork\n", encoding="utf-8")
        _git(fork_work, "add", "shared.txt")
    else:
        (fork_work / "fork.txt").write_text("fork behavior\n", encoding="utf-8")
        _git(fork_work, "add", "fork.txt")
    _git(fork_work, "commit", "-m", "fork behavior")
    _git(fork_work, "push", "origin", "main")
    fork_sha = _git(fork_work, "rev-parse", "HEAD").stdout.strip()

    _git(tmp_path, "clone", "-b", "main", str(upstream), str(upstream_work))
    _configure_author(upstream_work)
    if conflict:
        (upstream_work / "shared.txt").write_text("upstream\n", encoding="utf-8")
        _git(upstream_work, "add", "shared.txt")
    else:
        (upstream_work / "upstream.txt").write_text(
            "upstream change\n", encoding="utf-8"
        )
        _git(upstream_work, "add", "upstream.txt")
    (upstream_work / "hermes_cli" / "__init__.py").write_text(
        '__version__ = "0.21.0"\n__release_date__ = "2026.8.31"\n',
        encoding="utf-8",
    )
    _git(upstream_work, "add", "hermes_cli/__init__.py")
    _git(upstream_work, "commit", "-m", "upstream change")
    stable_sha = _git(upstream_work, "rev-parse", "HEAD").stdout.strip()
    _git(upstream_work, "tag", "-a", "v2026.8.31", "-m", "stable 0.21.0")
    _git(upstream_work, "push", "origin", "refs/tags/v2026.8.31")
    (upstream_work / "unreleased.txt").write_text(
        "must not land on the stable channel\n", encoding="utf-8"
    )
    _git(upstream_work, "add", "unreleased.txt")
    _git(upstream_work, "commit", "-m", "unreleased main change")
    _git(upstream_work, "push", "origin", "main")
    upstream_tip = _git(upstream_work, "rev-parse", "HEAD").stdout.strip()

    checkout_source = origin.as_uri() if shallow else str(origin)
    clone_args = ["clone", "-b", "main"]
    if shallow:
        clone_args.extend(["--depth", "1"])
    _git(tmp_path, *clone_args, checkout_source, str(checkout))
    _configure_author(checkout)
    _git(checkout, "remote", "add", "upstream", str(upstream))
    _git(checkout, "fetch", "origin", "main")
    _git(checkout, "fetch", "upstream", "main")
    return {
        "checkout": checkout,
        "origin": origin,
        "base_sha": base_sha,
        "fork_sha": fork_sha,
        "stable_sha": stable_sha,
        "upstream_tip": upstream_tip,
    }


def _rebase_is_active(repo: Path) -> bool:
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def test_fork_strategy_defaults_to_skip() -> None:
    assert DEFAULT_CONFIG["updates"]["channel"] == "stable"
    assert DEFAULT_CONFIG["updates"]["fork_strategy"] == "skip"


def test_update_parser_accepts_mutually_exclusive_continue_and_abort() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda _args: None)

    continued = parser.parse_args(["update", "--continue"])
    aborted = parser.parse_args(["update", "--abort"])

    assert continued.continue_fork_update is True
    assert continued.abort_fork_update is False
    assert aborted.abort_fork_update is True
    assert aborted.continue_fork_update is False
    with pytest.raises(SystemExit):
        parser.parse_args(["update", "--continue", "--abort"])


def test_clean_rebase_creates_recovery_state_and_lease_pushes(tmp_path: Path) -> None:
    from hermes_cli.fork_update import start_fork_rebase

    repos = _build_diverged_repositories(tmp_path, conflict=False)
    checkout = repos["checkout"]
    assert isinstance(checkout, Path)
    state_path = tmp_path / "hermes-home" / "fork-update-state.json"

    result = start_fork_rebase(GIT_CMD, checkout, state_path=state_path)

    new_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    remote_head = _git(repos["origin"], "rev-parse", "refs/heads/main").stdout.strip()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.changed is True
    assert result.paused is False
    assert result.needs_audit is True
    assert result.pre_update_head == repos["fork_sha"]
    assert new_head == remote_head
    assert new_head != repos["fork_sha"]
    assert state["old_base"] == repos["base_sha"]
    assert state["pre_update_head"] == repos["fork_sha"]
    assert state["channel"] == "stable"
    assert state["source_release"] == "v2026.8.27"
    assert state["target_release"] == "v2026.8.31"
    assert state["upstream_ref"] == "refs/tags/v2026.8.31"
    assert state["upstream_sha"] == repos["stable_sha"]
    assert state["audit_pending"] is True
    assert state["status"] == "post_update"
    assert "recovery_worktree" not in state
    assert (
        _git(
            checkout, "show-ref", "--verify", f"refs/heads/{state['backup_ref']}"
        ).returncode
        == 0
    )
    assert (
        _git(
            checkout, "merge-base", "--is-ancestor", state["upstream_sha"], "HEAD"
        ).returncode
        == 0
    )
    assert (
        _git(
            checkout,
            "merge-base",
            "--is-ancestor",
            repos["upstream_tip"],
            "HEAD",
            check=False,
        ).returncode
        == 1
    )
    assert (checkout / "unreleased.txt").exists() is False


def test_shallow_fork_unshallows_from_origin_before_stable_rebase(
    tmp_path: Path,
) -> None:
    from hermes_cli.fork_update import start_fork_rebase

    repos = _build_diverged_repositories(tmp_path, conflict=False, shallow=True)
    checkout = repos["checkout"]
    assert isinstance(checkout, Path)
    state_path = tmp_path / "hermes-home" / "fork-update-state.json"
    assert (
        _git(checkout, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    )

    result = start_fork_rebase(GIT_CMD, checkout, state_path=state_path)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.changed is True
    assert state["target_release"] == "v2026.8.31"
    assert (
        _git(checkout, "rev-parse", "--is-shallow-repository").stdout.strip() == "false"
    )
    assert (
        _git(
            checkout, "merge-base", "--is-ancestor", state["upstream_sha"], "HEAD"
        ).returncode
        == 0
    )
    assert (checkout / "unreleased.txt").exists() is False


def test_conflict_pauses_without_push_then_continue_finishes(tmp_path: Path) -> None:
    from hermes_cli.fork_update import continue_fork_rebase, start_fork_rebase

    repos = _build_diverged_repositories(tmp_path, conflict=True)
    checkout = repos["checkout"]
    assert isinstance(checkout, Path)
    state_path = tmp_path / "hermes-home" / "fork-update-state.json"

    paused = start_fork_rebase(GIT_CMD, checkout, state_path=state_path)

    assert paused.paused is True
    assert paused.conflicts == ("shared.txt",)
    assert _rebase_is_active(checkout)
    assert (
        _git(repos["origin"], "rev-parse", "refs/heads/main").stdout.strip()
        == repos["fork_sha"]
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "conflict"
    assert state["audit_pending"] is True
    recovery_root = Path(state["recovery_worktree"])
    assert paused.recovery_worktree == str(recovery_root)
    assert recovery_root.is_dir()
    assert (recovery_root / "shared.txt").read_text(encoding="utf-8") == "fork\n"

    (checkout / "shared.txt").write_text("upstream\nfork\n", encoding="utf-8")
    _git(checkout, "add", "shared.txt")
    _git(checkout, "-c", "core.editor=true", "rebase", "--continue")
    finished = continue_fork_rebase(GIT_CMD, checkout, state_path=state_path)

    final_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    assert finished.changed is True
    assert finished.paused is False
    assert finished.needs_audit is True
    assert (
        _git(repos["origin"], "rev-parse", "refs/heads/main").stdout.strip()
        == final_head
    )
    assert (
        _git(
            checkout, "merge-base", "--is-ancestor", state["upstream_sha"], "HEAD"
        ).returncode
        == 0
    )
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["status"] == "post_update"
    assert "recovery_worktree" not in final_state
    assert recovery_root.exists() is False


def test_abort_restores_backup_and_keeps_remote_unchanged(tmp_path: Path) -> None:
    from hermes_cli.fork_update import abort_fork_rebase, start_fork_rebase

    repos = _build_diverged_repositories(tmp_path, conflict=True)
    checkout = repos["checkout"]
    assert isinstance(checkout, Path)
    state_path = tmp_path / "hermes-home" / "fork-update-state.json"

    paused = start_fork_rebase(GIT_CMD, checkout, state_path=state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert paused.paused is True
    recovery_root = Path(state["recovery_worktree"])
    assert recovery_root.is_dir()

    aborted = abort_fork_rebase(GIT_CMD, checkout, state_path=state_path)

    assert aborted == repos["fork_sha"]
    assert _rebase_is_active(checkout) is False
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == repos["fork_sha"]
    assert _git(checkout, "branch", "--show-current").stdout.strip() == "main"
    assert _git(checkout, "status", "--porcelain").stdout == ""
    assert (
        _git(repos["origin"], "rev-parse", "refs/heads/main").stdout.strip()
        == repos["fork_sha"]
    )
    assert (
        _git(
            checkout, "show-ref", "--verify", f"refs/heads/{state['backup_ref']}"
        ).returncode
        == 0
    )
    assert state_path.exists() is False
    assert recovery_root.exists() is False
