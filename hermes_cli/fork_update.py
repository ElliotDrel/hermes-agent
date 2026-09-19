"""Deterministic, resumable updates for a maintained Hermes fork."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORK_UPDATE_STATE_FILE = "fork-update-state.json"
_STABLE_RELEASE_TAG = re.compile(
    r"^v(?P<year>\d{4})\.(?P<month>\d{1,2})\.(?P<day>\d{1,2})(?:\.(?P<patch>\d+))?$"
)


class ForkUpdateError(RuntimeError):
    """Raised when the fork workflow cannot continue without operator action."""


@dataclass(frozen=True)
class ForkSyncResult:
    """Outcome returned to the main updater."""

    verified: bool
    changed: bool = False
    paused: bool = False
    needs_audit: bool = False
    pre_update_head: str | None = None
    target_ref: str | None = None
    conflicts: tuple[str, ...] = ()
    detail: str = ""

    def __bool__(self) -> bool:
        return self.verified


def default_state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / FORK_UPDATE_STATE_FILE


def _run(
    git_cmd: list[str],
    cwd: Path,
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        git_cmd + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise ForkUpdateError(detail)
    return result


def _rev_parse(git_cmd: list[str], cwd: Path, ref: str) -> str:
    result = _run(git_cmd, cwd, "rev-parse", "--verify", ref, check=True)
    return result.stdout.strip()


def _current_branch(git_cmd: list[str], cwd: Path) -> str:
    return _run(git_cmd, cwd, "branch", "--show-current", check=True).stdout.strip()


def _rebase_in_progress(git_cmd: list[str], cwd: Path) -> bool:
    git_dir = Path(
        _run(git_cmd, cwd, "rev-parse", "--absolute-git-dir", check=True).stdout.strip()
    )
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _is_ancestor(git_cmd: list[str], cwd: Path, ancestor: str, descendant: str) -> bool:
    return (
        _run(
            git_cmd, cwd, "merge-base", "--is-ancestor", ancestor, descendant
        ).returncode
        == 0
    )


def _stable_tag_key(tag: str) -> tuple[int, int, int, int] | None:
    match = _STABLE_RELEASE_TAG.fullmatch(tag)
    if match is None:
        return None
    return (
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("patch") or 0),
    )


def _current_stable_tag(cwd: Path) -> str:
    version_file = cwd / "hermes_cli" / "__init__.py"
    try:
        tree = ast.parse(version_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise ForkUpdateError(
            f"Could not read the installed Hermes release date from {version_file}: {exc}"
        ) from exc
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__release_date__"
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            tag = f"v{value}"
            if _stable_tag_key(tag) is not None:
                return tag
    raise ForkUpdateError(
        f"Could not determine a stable release tag from {version_file}."
    )


def _latest_stable_release_target(
    git_cmd: list[str], cwd: Path
) -> tuple[str, str, str] | None:
    """Return ``(current_tag, target_tag, target_sha)`` for stable updates.

    Stable means an official calendar-version tag. The newest matching tag is
    selected, while ``upstream/main`` and non-release tags are deliberately
    ignored.
    """

    current_tag = _current_stable_tag(cwd)
    current_key = _stable_tag_key(current_tag)
    assert current_key is not None
    listed = _run(
        git_cmd,
        cwd,
        "ls-remote",
        "--tags",
        "--refs",
        "upstream",
        "refs/tags/v*",
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "git ls-remote failed").strip()
        raise ForkUpdateError(f"Could not list official stable releases: {detail}")

    tags: dict[str, tuple[int, int, int, int]] = {}
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith("refs/tags/"):
            continue
        tag = fields[1].removeprefix("refs/tags/")
        key = _stable_tag_key(tag)
        if key is not None:
            tags[tag] = key
    if current_tag not in tags:
        raise ForkUpdateError(
            f"Installed release {current_tag} is not present in the official stable tag set."
        )

    newer = [tag for tag, key in tags.items() if key > current_key]
    if not newer:
        return None
    target_tag = max(newer, key=lambda tag: tags[tag])
    refspec = f"+refs/tags/{target_tag}:refs/tags/{target_tag}"
    fetched = _run(git_cmd, cwd, "fetch", "upstream", refspec, "--quiet")
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout or "git fetch failed").strip()
        raise ForkUpdateError(f"Could not fetch stable release {target_tag}: {detail}")
    target_sha = _rev_parse(git_cmd, cwd, f"{target_tag}^{{commit}}")
    return current_tag, target_tag, target_sha


def _merge_base(
    git_cmd: list[str],
    cwd: Path,
    left: str,
    right: str,
    *,
    target_tag: str,
) -> str:
    result = _run(git_cmd, cwd, "merge-base", left, right)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    shallow = _run(git_cmd, cwd, "rev-parse", "--is-shallow-repository")
    if shallow.returncode == 0 and shallow.stdout.strip() == "true":
        deepen = _run(
            git_cmd,
            cwd,
            "fetch",
            "--unshallow",
            "origin",
            "main",
            "--quiet",
        )
        if deepen.returncode == 0:
            result = _run(git_cmd, cwd, "merge-base", left, right)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

    raise ForkUpdateError(
        f"Could not find a common ancestor for origin/main and {target_tag}. "
        "The updater did not start a rebase."
    )


def load_fork_update_state(state_path: Path | None = None) -> dict[str, Any] | None:
    path = state_path or default_state_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForkUpdateError(
            f"Could not read fork update state at {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ForkUpdateError(f"Fork update state at {path} is not a JSON object")
    return value


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def fork_audit_pending(state_path: Path | None = None) -> bool:
    state = load_fork_update_state(state_path)
    return bool(state and state.get("audit_pending"))


def complete_fork_audit(
    state_path: Path | None = None,
    *,
    summary: str = "PATCH.md audit completed",
) -> None:
    path = state_path or default_state_path()
    state = load_fork_update_state(path)
    if state is None:
        raise ForkUpdateError(f"No fork update state exists at {path}")
    if state.get("status") != "post_update":
        raise ForkUpdateError(
            f"Fork update state is {state.get('status')!r}; the code update is not complete"
        )
    state["audit_pending"] = False
    state["status"] = "audited"
    state["audit_summary"] = summary
    state["audited_at"] = datetime.now(timezone.utc).isoformat()
    _write_state(path, state)


def _unique_backup_ref(git_cmd: list[str], cwd: Path) -> str:
    stem = f"backup/pre-update-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    candidate = stem
    suffix = 2
    while (
        _run(
            git_cmd, cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"
        ).returncode
        == 0
    ):
        candidate = f"{stem}-{suffix}"
        suffix += 1
    return candidate


def _conflicted_paths(git_cmd: list[str], cwd: Path) -> tuple[str, ...]:
    result = _run(git_cmd, cwd, "diff", "--name-only", "--diff-filter=U")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _push_rebased_main(
    git_cmd: list[str], cwd: Path, *, expected_origin_sha: str
) -> subprocess.CompletedProcess[str]:
    return _run(
        git_cmd,
        cwd,
        "push",
        "origin",
        "HEAD:refs/heads/main",
        f"--force-with-lease=refs/heads/main:{expected_origin_sha}",
    )


def start_fork_rebase(
    git_cmd: list[str],
    cwd: Path,
    *,
    state_path: Path | None = None,
    channel: str = "stable",
) -> ForkSyncResult:
    """Rebase local ``main`` onto the channel's release and lease-push it."""

    path = state_path or default_state_path()
    existing = load_fork_update_state(path)
    if existing and existing.get("audit_pending"):
        raise ForkUpdateError(
            "A prior fork update still requires the hermes-fork-update audit."
        )
    if existing and not existing.get("audit_pending"):
        path.unlink(missing_ok=True)

    branch = _current_branch(git_cmd, cwd)
    if branch != "main":
        raise ForkUpdateError(
            f"Fork rebase strategy requires the main branch; checkout is on {branch or 'detached HEAD'}."
        )
    if _rebase_in_progress(git_cmd, cwd):
        raise ForkUpdateError(
            "A Git rebase is already in progress without matching fork update state."
        )
    if channel.lower() != "stable":
        raise ForkUpdateError(
            f"Maintained-fork updates currently support only the stable channel, not {channel!r}."
        )

    pre_update_head = _rev_parse(git_cmd, cwd, "HEAD")
    origin_sha = _rev_parse(git_cmd, cwd, "origin/main")
    if pre_update_head != origin_sha:
        raise ForkUpdateError(
            "Local main does not match origin/main. Push or reconcile it before running hermes update."
        )
    target = _latest_stable_release_target(git_cmd, cwd)
    if target is None:
        return ForkSyncResult(
            verified=True,
            target_ref=_current_stable_tag(cwd),
        )
    source_release, target_release, upstream_sha = target
    if _is_ancestor(git_cmd, cwd, upstream_sha, pre_update_head):
        return ForkSyncResult(verified=True, target_ref=target_release)

    old_base = _merge_base(
        git_cmd,
        cwd,
        origin_sha,
        upstream_sha,
        target_tag=target_release,
    )
    backup_ref = _unique_backup_ref(git_cmd, cwd)
    _run(git_cmd, cwd, "branch", backup_ref, pre_update_head, check=True)
    state: dict[str, Any] = {
        "version": 2,
        "branch": "main",
        "channel": "stable",
        "source_release": source_release,
        "target_release": target_release,
        "upstream_ref": f"refs/tags/{target_release}",
        "old_base": old_base,
        "pre_update_head": pre_update_head,
        "upstream_sha": upstream_sha,
        "backup_ref": backup_ref,
        "audit_pending": True,
        "status": "rebasing",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_state(path, state)

    rebased = _run(git_cmd, cwd, "rebase", upstream_sha)
    if rebased.returncode != 0:
        if _rebase_in_progress(git_cmd, cwd):
            conflicts = _conflicted_paths(git_cmd, cwd)
            state["status"] = "conflict"
            state["conflicts"] = list(conflicts)
            _write_state(path, state)
            return ForkSyncResult(
                verified=True,
                paused=True,
                needs_audit=True,
                pre_update_head=pre_update_head,
                target_ref=target_release,
                conflicts=conflicts,
                detail="Resolve the rebase, run git rebase --continue, then run hermes update --continue.",
            )
        state["status"] = "failed"
        state["error"] = (rebased.stderr or rebased.stdout).strip()
        _write_state(path, state)
        raise ForkUpdateError(
            state["error"] or "Git rebase failed before it could pause"
        )

    new_head = _rev_parse(git_cmd, cwd, "HEAD")
    if not _is_ancestor(git_cmd, cwd, upstream_sha, new_head):
        state["status"] = "failed"
        state["error"] = "Rebased HEAD does not descend from the recorded upstream SHA"
        _write_state(path, state)
        raise ForkUpdateError(state["error"])

    pushed = _push_rebased_main(git_cmd, cwd, expected_origin_sha=pre_update_head)
    if pushed.returncode != 0:
        state["status"] = "push_failed"
        state["rebased_head"] = new_head
        state["error"] = (pushed.stderr or pushed.stdout).strip()
        _write_state(path, state)
        return ForkSyncResult(
            verified=True,
            changed=True,
            paused=True,
            needs_audit=True,
            pre_update_head=pre_update_head,
            target_ref=target_release,
            detail="The rebase completed, but the lease-protected push failed. Run hermes update --continue after checking origin/main.",
        )

    state["status"] = "post_update"
    state["rebased_head"] = new_head
    state["pushed_at"] = datetime.now(timezone.utc).isoformat()
    _write_state(path, state)
    return ForkSyncResult(
        verified=True,
        changed=True,
        needs_audit=True,
        pre_update_head=pre_update_head,
        target_ref=target_release,
    )


def continue_fork_rebase(
    git_cmd: list[str],
    cwd: Path,
    *,
    state_path: Path | None = None,
) -> ForkSyncResult:
    """Finish a rebase the audit agent already resolved with Git."""

    path = state_path or default_state_path()
    state = load_fork_update_state(path)
    if state is None:
        raise ForkUpdateError("No paused fork update exists to continue.")
    if _rebase_in_progress(git_cmd, cwd):
        conflicts = _conflicted_paths(git_cmd, cwd)
        return ForkSyncResult(
            verified=True,
            paused=True,
            needs_audit=True,
            pre_update_head=state.get("pre_update_head"),
            target_ref=state.get("target_release"),
            conflicts=conflicts,
            detail="Git rebase is still active. Resolve conflicts and run git rebase --continue first.",
        )
    if state.get("status") == "post_update":
        return ForkSyncResult(
            verified=True,
            changed=True,
            needs_audit=True,
            pre_update_head=state.get("pre_update_head"),
            target_ref=state.get("target_release"),
        )
    if state.get("status") not in {"conflict", "push_failed", "rebasing"}:
        raise ForkUpdateError(
            f"Fork update state cannot be continued from status {state.get('status')!r}."
        )
    if _current_branch(git_cmd, cwd) != state.get("branch", "main"):
        raise ForkUpdateError(
            "The checkout is not on the branch recorded by the paused update."
        )
    if _run(git_cmd, cwd, "status", "--porcelain", "--untracked-files=all").stdout:
        raise ForkUpdateError(
            "The rebase result has uncommitted changes; refusing to push."
        )

    upstream_sha = str(state["upstream_sha"])
    new_head = _rev_parse(git_cmd, cwd, "HEAD")
    if not _is_ancestor(git_cmd, cwd, upstream_sha, new_head):
        raise ForkUpdateError(
            "HEAD does not descend from the upstream SHA recorded before the rebase."
        )
    pushed = _push_rebased_main(
        git_cmd,
        cwd,
        expected_origin_sha=str(state["pre_update_head"]),
    )
    if pushed.returncode != 0:
        state["status"] = "push_failed"
        state["error"] = (pushed.stderr or pushed.stdout).strip()
        _write_state(path, state)
        return ForkSyncResult(
            verified=True,
            changed=True,
            paused=True,
            needs_audit=True,
            pre_update_head=state.get("pre_update_head"),
            target_ref=state.get("target_release"),
            detail="The lease-protected push still fails. Inspect origin/main before retrying.",
        )

    state["status"] = "post_update"
    state["rebased_head"] = new_head
    state["pushed_at"] = datetime.now(timezone.utc).isoformat()
    state.pop("conflicts", None)
    state.pop("error", None)
    _write_state(path, state)
    return ForkSyncResult(
        verified=True,
        changed=True,
        needs_audit=True,
        pre_update_head=state.get("pre_update_head"),
        target_ref=state.get("target_release"),
    )


def abort_fork_rebase(
    git_cmd: list[str],
    cwd: Path,
    *,
    state_path: Path | None = None,
) -> str:
    """Abort a paused rebase and restore its verified backup ref."""

    path = state_path or default_state_path()
    state = load_fork_update_state(path)
    if state is None:
        raise ForkUpdateError("No paused fork update exists to abort.")
    if state.get("status") == "post_update":
        raise ForkUpdateError(
            "The rebased main branch was already pushed. Run the audit instead of aborting it."
        )
    backup_ref = str(state["backup_ref"])
    backup_sha = _rev_parse(git_cmd, cwd, backup_ref)
    expected_sha = str(state["pre_update_head"])
    if backup_sha != expected_sha:
        raise ForkUpdateError(
            "The recorded backup ref no longer matches the pre-update HEAD."
        )

    if _rebase_in_progress(git_cmd, cwd):
        _run(git_cmd, cwd, "rebase", "--abort", check=True)
    current_sha = _rev_parse(git_cmd, cwd, "HEAD")
    if current_sha != backup_sha:
        if _run(git_cmd, cwd, "status", "--porcelain", "--untracked-files=all").stdout:
            raise ForkUpdateError(
                "The worktree is dirty; refusing to restore the backup ref."
            )
        branch = str(state.get("branch", "main"))
        _run(git_cmd, cwd, "switch", "--detach", backup_ref, check=True)
        _run(git_cmd, cwd, "branch", "-f", branch, backup_ref, check=True)
        _run(git_cmd, cwd, "switch", branch, check=True)

    restored_sha = _rev_parse(git_cmd, cwd, "HEAD")
    if restored_sha != backup_sha:
        raise ForkUpdateError("Git did not restore the recorded backup ref.")
    path.unlink(missing_ok=True)
    return restored_sha
