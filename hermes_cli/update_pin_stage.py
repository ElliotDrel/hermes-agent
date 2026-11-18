"""Pinned, inert preparation and explicit Windows detached-gateway activation (HERMES-FORK-028).

Candidates and retained previous installations are siblings of the install. No
live source, dependency, configuration or gateway is changed during preparation.
Not ``update_stage`` (upstream's desktop progress helper): never shadow that name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from hermes_cli.update_pin_stage_env import (
    SYNC_COMMITTED_EXIT, build_candidate_environment, candidate_python, clean_env, probe,
    rename_with_retry, run_complete_install, run_resync_previous,
)
from hermes_cli.update_pin_stage_recovery import (
    GATEWAY_EXIT_WAIT_S, GatewayStillRunning, consume_marker, recover_gateway,
)
from hermes_constants import get_hermes_home
from utils import atomic_json_write

MODULE = "hermes_cli.update_pin_stage"


def pending_path(root: Path) -> Path:
    return root.parent / f".{root.name}.pin-stage.json"


def _legacy_pending_path(root: Path) -> Path:
    # The pre-v0.23.2 fork wrote this name; its loaded /restart handler would run
    # ``-m hermes_cli.update_stage`` (upstream's helper has no entrypoint) and strand
    # the gateway, so new markers never use it. Old-staged pins are still activatable.
    return root.parent / f".{root.name}.staged-update.json"


def _marker(root: Path) -> Path:
    return next((path for path in (pending_path(root), _legacy_pending_path(root)) if path.exists()),
                pending_path(root))


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True, timeout=300, env=clean_env())
    return result.stdout.strip()


def _clean(root: Path) -> None:
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"Staging/activation requires a clean checkout: {root}")


def _require_owner(root: Path) -> None:
    if sys.platform != "win32":
        raise ValueError("Staged activation currently supports native Windows detached gateways only.")
    from hermes_cli.config import is_managed
    if is_managed():
        raise ValueError("Managed installations must use their package manager.")
    if not (root / ".git").is_dir():
        raise ValueError("Staging requires a standalone Git clone, not a linked worktree.")
    # Do not silently leave services, another profile or Desktop on an old generation.
    from hermes_cli.update_cmd_windows import _desktop_owns_gateway_lifecycle
    from gateway.restart import is_container_restart_context, is_gateway_supervisor_process
    from gateway.status import get_running_pid, read_runtime_status
    status = read_runtime_status() or {}
    if (is_gateway_supervisor_process() or is_container_restart_context()
            or _desktop_owns_gateway_lifecycle(project_root=root)):
        raise ValueError("Staging supports a detached gateway only; service/Desktop owners are unsupported.")
    from hermes_cli.gateway_supervised_restart import gateway_declares_external_supervisor
    from hermes_cli.profiles import profiles_to_serve
    pid = get_running_pid()
    if pid and gateway_declares_external_supervisor(pid):
        raise ValueError("Externally supervised gateways cannot use staged detached activation.")
    homes = status.get("served_profiles") or []
    if len(homes) > 1 or len(profiles_to_serve(multiplex=True)) > 1:
        raise ValueError("Staging does not activate multi-profile fleets.")


def _carry_remotes(root: Path, candidate: Path) -> None:
    """Re-declare the installed tree's other remotes (e.g. ``upstream``) in the candidate; read-only on root."""
    for name in _git(root, "remote").split():
        if name == "origin":
            continue
        url = _git(root, "remote", "get-url", name)
        _git(candidate, "remote", "add", name, url)
        push = _git(root, "remote", "get-url", "--push", name)
        if push != url:
            _git(candidate, "remote", "set-url", "--push", name, push)


def stage(root: Path, sha: str, branch: str = "live") -> dict:
    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("--stage requires the exact lowercase 40-character published commit SHA.")
    if not branch or branch.startswith("-"):
        raise ValueError("Invalid published branch.")
    _require_owner(root)
    _clean(root)
    if pending_path(root).exists() or _legacy_pending_path(root).exists():
        raise ValueError("A candidate is already staged; activate it or explicitly move its pending marker aside.")
    token = uuid4().hex
    candidate = root.parent / f".{root.name}.candidate-{token}"
    origin = _git(root, "remote", "get-url", "origin")
    # Fetch in the candidate only. The installed repo's HEAD, index, refs and venv stay untouched.
    subprocess.run(["git", "clone", "--no-checkout", "--single-branch", "--branch", branch,
                    "--", origin, str(candidate)], check=True, env=clean_env(), timeout=600)
    _carry_remotes(root, candidate)
    tip = _git(candidate, "rev-parse", f"refs/remotes/origin/{branch}")
    # Exact pin must be published on this branch; later branch movement is harmless.
    _git(candidate, "merge-base", "--is-ancestor", sha, tip)
    _git(candidate, "checkout", "-B", branch, sha)
    if not (candidate / "hermes_cli" / "update_pin_stage.py").is_file():
        raise ValueError("Published candidate predates staged activation support; publish a compatible candidate first.")
    build_candidate_environment(candidate, ["all"])
    probe(candidate)
    _clean(candidate)
    if _git(candidate, "rev-parse", "HEAD") != sha:
        raise ValueError("Candidate revision changed during preparation.")
    data = {"schema": 1, "token": token, "sha": sha, "branch": branch,
            "root": str(root), "candidate": str(candidate), "home": str(get_hermes_home().resolve()),
            "previous_sha": _git(root, "rev-parse", "HEAD")}
    atomic_json_write(pending_path(root), data)
    return data


def load_pending(root: Path, token: str | None = None) -> dict | None:
    path = _marker(root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) for key in
            ("token", "sha", "root", "candidate", "home", "previous_sha")):
        raise ValueError("Malformed staged candidate marker; gateway unchanged.")
    expected = root.parent / f".{root.name}.candidate-{data['token']}"
    if (data.get("schema") != 1 or data.get("root") != str(root.resolve())
            or data.get("candidate") != str(expected.resolve())
            or not re.fullmatch(r"[0-9a-f]{32}", data["token"])
            or not re.fullmatch(r"[0-9a-f]{40}", data["sha"])
            or data.get("home") != str(get_hermes_home().resolve())
            or (token is not None and token != data["token"])):
        raise ValueError("Staged candidate does not belong to this installation/profile/token.")
    return data


def restart_candidate(root: Path) -> dict | None:
    data = load_pending(root)
    if data and not Path(data["candidate"]).is_dir():
        # Promoted (marker removal failed) or deleted: nothing can ever activate from it.
        consume_marker(_marker(root))
        raise ValueError("Stale staged-candidate marker removed (its candidate directory is gone); "
                         "run /restart again for an ordinary restart.")
    if data:
        _require_owner(root)
        _validate(root, data)
    return data


def activation_command(root: Path, data: dict) -> list[str]:
    """The explicit activation command, run from the candidate's own interpreter."""
    return [str(candidate_python(Path(data["candidate"]))), "-I", "-m", MODULE,
            "--root", str(root), "--activate-stage", data["token"]]


def _validate(root: Path, data: dict) -> None:
    _clean(root)
    candidate = Path(data["candidate"])
    _clean(candidate)
    if (_git(root, "rev-parse", "HEAD") != data["previous_sha"]
            or _git(candidate, "rev-parse", "HEAD") != data["sha"]):
        raise ValueError("Installed or staged revision changed; refusing activation.")
    if not candidate_python(candidate).is_file():
        raise ValueError("Staged dependency environment is missing.")


def activate(root: Path, token: str) -> dict:
    root = root.resolve()
    _require_owner(root)
    data = load_pending(root, token)
    if data is None:
        raise ValueError("No staged candidate.")
    _validate(root, data)
    from gateway.status import get_running_pid
    if get_running_pid():
        raise GatewayStillRunning("Gateway is still running; only explicit /restart may drain it before activation.")
    from hermes_cli.update_cmd_windows import _detect_venv_python_processes
    if _detect_venv_python_processes(project_root=root):
        raise ValueError("The installed environment still has live holders; close them and retry. Never use --force-venv.")
    candidate = Path(data["candidate"])
    previous = root.parent / f".{root.name}.previous-{token}"
    if previous.exists():
        raise ValueError(f"Retained previous installation already exists: {previous}")
    # Leave the current directory outside BOTH rename targets on Windows.
    os.chdir(root.parent)
    rename_with_retry(root, previous)
    try:
        rename_with_retry(candidate, root)
    except OSError:
        rename_with_retry(previous, root)
        raise
    data["previous"] = str(previous)
    try:
        probe(root)
        # Dependencies are published only after the cheap relocation probe. A sync that
        # fails commits nothing; one that commits and then fails (launcher publication)
        # leaves PM on the new lock, which the rollback below must undo.
        run_complete_install(root, previous)
    except BaseException as exc:
        rename_with_retry(root, candidate)
        rename_with_retry(previous, root)
        if isinstance(exc, subprocess.CalledProcessError) and exc.returncode == SYNC_COMMITTED_EXIT:
            _restore_pm_state(root, candidate, exc)
        raise
    return data


def _restore_pm_state(root: Path, candidate: Path, cause: BaseException) -> None:
    """Re-sync PM to the restored tree after a publication that committed and then failed."""
    from hermes_cli.update_receipt import record_step
    try:
        run_resync_previous(root, candidate)
    except BaseException as resync_error:
        record_step("pm_state_restore", False, str(resync_error))
        raise RuntimeError(
            "Dependency publication failed after PM committed the candidate's lock; the previous "
            "tree is restored but PM state could not be re-synced to it. Run `hermes update` "
            f"before relying on the installed environment ({resync_error})") from cause
    record_step("pm_state_restore", True, "PM re-synced to the restored previous tree")


def run_stage_command(args, root: Path) -> None:
    from hermes_cli.update_lock import UpdateLock
    from hermes_cli.update_receipt import begin_update_receipt, finalize_update_receipt, record_step
    # The executing candidate's package paths disappear on promotion. Receipt
    # finalization must not depend on a first import from those vanished paths.
    from hermes_cli import (process_identity, runtime_state, update_receipt,  # noqa: F401
                            update_serve_obligations, version_info)
    promoted = False
    lock = UpdateLock()
    if not lock.acquire():
        if not args.stage:
            # /restart already drained the gateway; the old install is untouched, so bring it back.
            recover_gateway(root, wait_s=GATEWAY_EXIT_WAIT_S)
        raise ValueError("Another native update is running.")
    begin_update_receipt()
    try:
        if args.stage:
            data = stage(root, args.stage, args.branch or "live")
            record_step("stage", True, f"previous_sha={data['previous_sha']}; target_sha={data['sha']}; candidate={data['candidate']}; active installation unchanged")
            print(f"Staged {data['sha']}. Active gateway unchanged. /restart activates only once the staged handler is installed and loaded.")
            print("First deployment: explicitly stop the native gateway, then run from outside both trees:")
            print(subprocess.list2cmdline(activation_command(root, data)))
        else:
            data = activate(root, args.activate_stage)
            record_step("activate", True, f"previous_sha={data['previous_sha']}; target_sha={data['sha']}; previous={data['previous']}")
            # Fresh code/environment owns native launch, never the pre-swap interpreter.
            env = clean_env()
            # From here the install is the promoted tree: never recover the old one.
            promoted = True
            # Consume the marker once promotion/import checks succeed. A failed
            # launch retains BOTH generations; a marker that cannot be removed
            # (locked on Windows) is cleared later by restart_candidate.
            if not consume_marker(_marker(root)):
                record_step("consume_marker", False, "marker still present; cleared on the next /restart")
            # Never rename after a spawn attempt: even a nonzero CLI result or a
            # missing pid file cannot exclude a child still booting from root.
            python = str(candidate_python(root))
            subprocess.run([python, "-I", "-m", "hermes_cli.main", "gateway", "start"],
                           cwd=root.parent, env=env, check=True)
            subprocess.run([python, "-I", "-c",
                            f"from {MODULE} import _verify_started_gateway; "
                            f"_verify_started_gateway({data['sha']!r})"],
                           cwd=root.parent, env=env, check=True, timeout=40)
        finalize_update_receipt("success")
    except BaseException as exc:
        record_step("stage" if args.stage else "activate", False, str(exc))
        # Preparation never touches the gateway. Activation was user-requested;
        # if cutover refused/rolled back, recover the untouched old install using
        # its native start rather than strand the gateway after the manual drain.
        if not args.stage and not promoted:
            # The watcher fires at its drain deadline even if the gateway is still exiting.
            recover_gateway(root, wait_s=GATEWAY_EXIT_WAIT_S if isinstance(exc, GatewayStillRunning) else 0.0)
        finalize_update_receipt("failed")
        raise
    finally:
        lock.release()


def _verify_started_gateway(sha: str, timeout_s: float = 15.0) -> None:
    """Fresh promoted interpreter: stable process discovery AND matching live SHA.

    Native start may return zero after a printed failure. Old status can also
    survive startup, so poll identity instead of treating one stale read as final.
    """
    from hermes_cli.gateway_windows import _live_gateway_pids, _wait_for_gateway_ready
    from gateway.status import read_runtime_status
    if not _wait_for_gateway_ready(timeout_s=timeout_s):
        raise ValueError("Native gateway start did not become ready.")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = read_runtime_status() or {}
        if status.get("code_sha") == sha and status.get("pid") in _live_gateway_pids(False):
            return
        time.sleep(0.2)
    raise ValueError("Native start did not attest the pinned candidate SHA from a live gateway; inspect gateway status.")


if __name__ == "__main__":
    # Bootstrap: explicit user activation from the prepared interpreter after a native stop.
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--activate-stage")
    mode.add_argument("--stage", metavar="SHA")
    parser.add_argument("--branch", default="live")
    options = parser.parse_args()
    run_stage_command(options, options.root)
