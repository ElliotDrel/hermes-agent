"""Exact-pin, inert source-update preparation and explicit activation.

Receipts bind a PM generation to an advertised Git commit and one clean owner.
No code from that commit runs in the old interpreter. Post-commit failures keep
native completion/restart debt and all generations for forward recovery.
"""
from __future__ import annotations

from contextlib import contextmanager
import ast
import atexit
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

from pm.candidates import abandon_candidate, prepare_candidate, promote_candidate, reject_plugin_links, validate_candidate
from pm.environments import install_state_dir, runtime_facts_path, selected_venv, venv_command
from pm.filesystem import durable_write_bytes, file_digest
from pm.lock import Facts
from pm.workspace import enabled_member_dirs, members_stamp


def _git_command() -> list[str]:
    from hermes_cli.update_cmd import _base_git_cmd
    from hermes_platform.resolver import locate_command

    found = locate_command("git").command
    if not found:
        raise ValueError("Git is unavailable for this source install")
    return [*found, *_base_git_cmd()[1:]]


def _git(root: Path, *args: str) -> str:
    from hermes_cli.update_custody import run_git

    result = run_git(_git_command(), list(args), cwd=str(root), capture_output=True,
                     text=True, encoding="utf-8", errors="replace", timeout=120,
                     stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    result.check_returncode()
    return result.stdout.strip()


def _clean(root: Path) -> None:
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Source owner is dirty; commit or move local changes before preparing/activating")


def _selection(root: Path) -> dict:
    fact = Facts(runtime_facts_path(root), strict=True).get("venv")
    if (not isinstance(fact, dict) or not all(isinstance(fact.get(k), str) and fact[k]
            for k in ("environment", "resolved_lock", "stamp"))
            or not isinstance(fact.get("extras"), list)
            or any(not isinstance(extra, str) for extra in fact["extras"])):
        raise ValueError("Source owner has no complete PM-managed runtime selection")
    generation = Path(fact["environment"]).resolve().parent
    if (generation.parent != install_state_dir(root).resolve() / "environments"
            or Path(fact["resolved_lock"]).resolve() != generation / "workspace" / "uv.lock"):
        raise ValueError("Runtime selection does not belong to this source owner")
    return fact


def admit_owner(root: Path) -> Path:
    """Refuse unsupported installs and foreign interpreters before creating state."""
    from hermes_cli.update_owning_install import _pm_environment_owner
    from hermes_cli._launchers import _launcher_python
    from hermes_cli.venv_sync import refuse_foreign_owned_venv
    from hermes_cli.config import detect_install_method, get_managed_system
    from hermes_cli.image_provenance import read_image_provenance
    from hermes_cli.steward import is_bundled_payload
    from hermes_cli._early_recovery import interrupted_pull_marker

    root = Path(root).resolve()
    generation_owner = _pm_environment_owner(Path(sys.prefix).resolve())
    if generation_owner is not None:
        root = generation_owner.resolve()
    if getattr(sys, "frozen", False) or not (root / ".git").exists() or (root / "enabled-features.json").exists():
        raise ValueError("Pinned candidates require a writable Git-backed PM source install")
    if (detect_install_method(root) != "git" or get_managed_system() or is_bundled_payload(root)
            or read_image_provenance() is not None):
        raise ValueError("This install is owned by a package/image manager; use its native updater")
    if interrupted_pull_marker(root).exists():
        raise ValueError("Finish the interrupted source update before preparing another candidate")
    refuse_foreign_owned_venv(root)
    _selection(root)
    prefix = Path(sys.prefix).resolve()
    selected = selected_venv(root).resolve()
    launchers = root / ".hermes" / "bin"
    launcher_pythons = [_launcher_python(launchers / name) for name in ("hermes.exe", "hermes.cmd", "hermes")]
    store_launcher = any(p is not None and p.resolve() == Path(sys.executable).resolve()
                         for p in launcher_pythons)
    if prefix == selected and generation_owner != root:
        raise ValueError("Managed interpreter owner is missing its inputs/.project-root proof")
    if prefix != selected and not store_launcher:
        raise ValueError("Foreign interpreter: use this source install's hermes launcher")
    _clean(root)
    return root


def _advertised(root: Path, remote: str, sha: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Specify an exact lowercase 40-character published Git SHA")
    if remote not in _git(root, "remote").splitlines():
        raise ValueError("Recorded update remote is no longer configured")
    url = _git(root, "remote", "get-url", remote)
    tips = {line.split()[0] for line in _git(root, "ls-remote", remote).splitlines() if line.strip()}
    if sha not in tips:
        raise ValueError("Target SHA is no longer advertised by the recorded remote")
    return url


def _snapshot(root: Path, sha: str, destination: Path) -> None:
    from hermes_cli.update_custody import run_git

    result = run_git(_git_command(), ["archive", "--format=tar", sha], cwd=str(root),
                     capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
    result.check_returncode()
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(destination, filter="data")


def _plugins(destination: Path) -> dict:
    """Freeze native read-only member enumeration, retaining installed identities.

    PM's reader parses existing YAML directly; it does not initialize config,
    discover/execute plugin hooks, or create profile homes.
    """
    result = {}
    for source in enabled_member_dirs():
        source = Path(source).absolute()
        reject_plugin_links(source)
        target = destination / hashlib.sha256(str(source).encode()).hexdigest()[:16]
        shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv", "venv"))
        reject_plugin_links(target)
        result[str(source.resolve())] = str(target)
    return result


def _preflight(root: Path, manifest: dict) -> None:
    """Probe the fresh interpreter off-process, with disposable home/runtime/temp."""
    with tempfile.TemporaryDirectory(prefix="preflight-", dir=Path(manifest["source"]).parent) as scratch:
        isolated = Path(scratch)
        env = {**os.environ, "HERMES_HOME": str(isolated / "home"),
               "HERMES_RUNTIME_DIR": str(isolated / "runtime"), "PYTHONDONTWRITEBYTECODE": "1",
               "TEMP": scratch, "TMP": scratch, "TMPDIR": scratch, "HOME": scratch, "USERPROFILE": scratch}
        env.pop("PYTHONPATH", None)
        env.pop("VIRTUAL_ENV", None)
        command = venv_command(root, Path(manifest["environment"]), ("-I", "-B"))
        probe = subprocess.run([*command, "-c", "import hermes_cli.main; import gateway.run"],
                               cwd=isolated, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120, check=False)
        if probe.returncode:
            raise RuntimeError("Candidate interpreter preflight failed: " + probe.stderr[-4000:])


def _write_receipt(path: Path, receipt: dict) -> None:
    durable_write_bytes(path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())


@contextmanager
def _locked(root: Path):
    from hermes_cli.update_lock import UpdateLock, describe_holder

    lock = UpdateLock(install_root=root)
    if not lock.acquire():
        raise RuntimeError("Update lock busy: " + describe_holder(lock.holder))
    try:
        yield
    finally:
        lock.release()


def prepare(root: Path, sha: str, *, remote: str | None = None) -> Path:
    """Prepare and verify a published pin while the active source/selection stays fixed."""
    root = admit_owner(root)
    with _locked(root):
        root = admit_owner(root)
        remote = remote or ("upstream" if "upstream" in _git(root, "remote").splitlines() else "origin")
        url = _advertised(root, remote, sha)
        base, fact = _git(root, "rev-parse", "HEAD"), _selection(root)
        selection_hash = file_digest(runtime_facts_path(root))
        _git(root, "fetch", "--no-tags", remote, sha)
        if _git(root, "merge-base", base, sha) != base:
            raise ValueError("Pinned target must fast-forward the current owner")
        directory = install_state_dir(root) / "candidates" / uuid.uuid4().hex
        source = directory / "source"
        manifest = None
        path = directory / "receipt.json"
        try:
            _snapshot(root, sha, source)
            _candidate_protocol(source)
            plugins = _plugins(directory / "plugins")
            manifest = prepare_candidate(root, source, sha, fact["extras"], plugins)
            receipt = {"manifest": manifest, "manifest_hash": _manifest_hash(manifest),
                       "remote": remote, "remote_url": url, "selection_hash": selection_hash, "state": "preparing"}
            _write_receipt(path, receipt)
            _preflight(root, manifest)
            admit_owner(root)
            validate_candidate(root, manifest)
            if members_stamp(enabled_member_dirs()) != manifest["plugin_stamp"]:
                raise ValueError("Enabled plugin inputs changed during preparation; retry")
            if file_digest(runtime_facts_path(root)) != selection_hash:
                raise ValueError("Active dependency selection changed during preparation; retry")
            receipt["state"] = "prepared"
            _write_receipt(path, receipt)
            return path
        except BaseException as exc:
            if manifest is not None:
                try:
                    abandon_candidate(root, manifest)
                except (OSError, ValueError, RuntimeError) as cleanup:
                    print(f"Could not release failed preparation's retention: {cleanup}", file=sys.stderr)
                receipt["state"] = "preparation-failed"
                receipt["followup"] = str(exc)
                try:
                    _write_receipt(path, receipt)
                except OSError as persistence:
                    print(f"Failed preparation receipt could not be updated: {persistence}", file=sys.stderr)
                print(f"Failed candidate evidence: {path}; generation: {manifest['environment']}", file=sys.stderr)
            else:
                print(f"Failed candidate inputs: {directory}", file=sys.stderr)
            raise


def _candidate_protocol(source: Path) -> None:
    """Inspect target support without importing any target application code."""
    try:
        tree = ast.parse((source / "hermes_cli/update_completion.py").read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError) as exc:
        raise ValueError("Target lacks the native pinned-candidate continuation protocol") from exc
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(name, ast.Name) and name.id == "PINNED_CANDIDATE_PROTOCOL" for name in node.targets)
              and isinstance(node.value, ast.Constant)]
    bindings = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                and node.id == "PINNED_CANDIDATE_PROTOCOL" and isinstance(node.ctx, (ast.Store, ast.Del))]
    if len(bindings) != 1 or len(values) != 1 or type(values[0]) is not int or values[0] != 1:
        raise ValueError("Target has an incompatible native pinned-candidate continuation protocol")


def _manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _validate_receipt(root: Path, path: Path, receipt: dict) -> dict:
    required = ("remote", "remote_url", "manifest_hash", "selection_hash", "state")
    if (not isinstance(receipt, dict) or not isinstance(receipt.get("manifest"), dict)
            or any(not isinstance(receipt.get(key), str) for key in required)):
        raise ValueError("Candidate receipt is incomplete or malformed")
    manifest = receipt["manifest"]
    validate_candidate(root, manifest)
    _candidate_protocol(Path(manifest["source"]))
    if (path.resolve() != Path(manifest["source"]).parent / "receipt.json"
            or not path.resolve().is_relative_to(install_state_dir(root).resolve() / "candidates")
            or receipt["manifest_hash"] != _manifest_hash(manifest) or receipt["state"] != "prepared"):
        raise ValueError("Candidate receipt changed, escaped its owner, or was already activated")
    if members_stamp(enabled_member_dirs()) != manifest["plugin_stamp"]:
        raise ValueError("Enabled plugin selection or content changed since preparation")
    if file_digest(runtime_facts_path(root)) != receipt["selection_hash"]:
        raise ValueError("Active dependency selection changed since preparation")
    if _advertised(root, receipt["remote"], manifest["target_sha"]) != receipt["remote_url"]:
        raise ValueError("Recorded update remote changed")
    return manifest


def _drain():
    from types import SimpleNamespace
    from hermes_cli.update_cmd import _pause_gateways_for_update
    from hermes_cli import update_cmd_posix_pause as posix
    from hermes_cli.update_cmd_windows import list_venv_holders

    token = _pause_gateways_for_update(SimpleNamespace(no_gateway_restart=False))
    try:
        posix.pause_at_commit_point()
        holders = list_venv_holders()
        if os.name != "nt":
            # The ordinary POSIX updater deliberately continues on pause failure.
            # Explicit promotion requires a complete drain; repeat native discovery
            # without its best-effort exception suppression before the commit point.
            units, jobs = posix._discover_systemd(), posix._discover_launchd()
            bare, _notices = posix._discover_bare({u["pid"] for u in units} | {j["pid"] for j in jobs})
            holders.extend([*units, *jobs, *bare])
        if holders:
            raise RuntimeError("Source still has runtime holders after native drain: " + repr(holders))
    except BaseException:
        _resume(token)
        raise
    return token


def _resume(token) -> None:
    from hermes_cli.update_cmd_windows import _resume_windows_gateways_after_update

    _resume_windows_gateways_after_update(token)
    if token and (token.get("resume_deferred") or token.get("resume_needed")):
        raise RuntimeError("Native gateway resume/readiness remains incomplete")



def activate(root: Path, receipt_path: Path) -> bool:
    """Revalidate, drain, arm native debt, fast-forward, promote, finish and resume."""
    from hermes_cli import update_cmd_commit as commit
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.update_completion import run_completion, settle_lost_completion
    from hermes_cli.update_lock import checkout_lock_fds
    try:
        from hermes_cli.process_identity import _process_create_time
    except ImportError as exc:
        raise ValueError("Updater reporting dependencies are unavailable; repair this install before activating") from exc
    _process_create_time(os.getpid())  # receipt recovery must never cold-import target code
    from hermes_cli.venv_sync import arm_completion
    # Resolve recovery/transport entrypoints while the source is still old.
    recovery_arm = arm_completion
    transport_fds = checkout_lock_fds
    from pm.receipt import accept_worker_receipt
    from hermes_constants import get_hermes_home
    from hermes_cli import update_cmd_config
    from copy import deepcopy
    root = admit_owner(root)
    path = Path(receipt_path).resolve()
    receipt = json.loads(path.read_text(encoding="utf-8-sig"))
    with _locked(root):
        manifest = _validate_receipt(root, path, receipt)
        _preflight(root, manifest)
        admit_owner(root)
        manifest = _validate_receipt(root, path, receipt)
        from hermes_cli import update_receipt as reporting
        reporting.begin_update_receipt()
        reporting.record_stage("plan", "success", mode="pinned-candidate")
        token = None
        moved = False
        resume_settled = False
        recovery_request = None
        update_id = reporting._current.get().data["update_id"]
        def recover_paused():
            nonlocal resume_settled
            if not moved or not token or resume_settled or recovery_request is None:
                return
            resume_settled = True
            try:
                recovery = run_completion(recovery_request)
                if recovery.get("receipt"):
                    reporting.adopt_terminal_receipt(recovery["receipt"])
                if recovery.get("windows_resume") is not None:
                    resumed = dict(recovery["windows_resume"])
                    token.clear()
                    token.update(resumed)
                else:
                    reporting.owe_followup(update_id, "windows_resume", "fresh recovery returned no paused-fleet result")
            except Exception as exc:  # health: allow BLE001 -- durable pause/dependency debt survives transport failure
                reporting.owe_followup(update_id, "windows_resume", str(exc))
        commit.begin_update_attempt()
        commit.record_run_start(_git_command(), root)
        try:
            snapshot_id, plan = _activation_snapshot()
            inventory = plan["runtimes"] if isinstance(plan, dict) else plan
            token = _drain()
            admit_owner(root)
            _validate_receipt(root, path, receipt)
            if reason := commit.arm_commit_point(_git_command(), root, manifest["target_sha"],
                                                pre=manifest["base_head"], target=manifest["target_sha"],
                                                stash=None, runtimes=inventory):
                raise RuntimeError(reason)
            # Freeze a data-only request before source mutation. The old parent only
            # runs its preloaded stdlib transport; all target imports run in children.
            transport_fds(root)
            request = {
                "schema": 1, "source": str(root), "home": str(get_hermes_home()),
                "branch": _git(root, "branch", "--show-current"), "desktop": False,
                "assume_yes": True, "gateway_mode": False, "no_gateway_restart": False,
                "pre_update_version": None, "snapshot_id": snapshot_id,
                "sibling_snapshots": deepcopy(update_cmd_config._LAST_SIBLING_SNAPSHOTS), "plan": plan if isinstance(plan, dict) else None,
                "receipt": deepcopy(reporting._current.get().data), "windows_resume": token,
                "prepared_candidate": {"manifest": manifest, "path": str(path), "receipt": receipt},
            }
            recovery_request = {**deepcopy(request), "resume_only": True}
            if token:
                atexit.register(recover_paused)
            _git(root, "merge", "--ff-only", "--no-edit", manifest["target_sha"])
            moved = True
            if _git(root, "rev-parse", "HEAD") != manifest["target_sha"]:
                raise RuntimeError("Checkout did not land at the exact prepared pin")
            _clean(root)
            interrupted_pull_marker(root).unlink(missing_ok=True)
            reporting.record_stage("apply", "success", mode="pinned-candidate")
            request["receipt"] = deepcopy(reporting._current.get().data)
            try:
                result = run_completion(request)
            except Exception as exc:  # health: allow BLE001 -- committed code; native recovery owns lost children
                recovery_arm(root)
                result = settle_lost_completion(request, f"{type(exc).__name__}: {exc}")
            if result.get("receipt") is None:
                result = settle_lost_completion(request, result.get("error", "completion result lost"))
            accept_worker_receipt(result.get("pm_receipt"), request["receipt"]["update_id"])
            if token is not None and result.get("windows_resume") is not None:
                resumed = dict(result["windows_resume"])
                token.clear()
                token.update(resumed)
                resume_settled = True  # the native child attempted this token, even on a follow-up
            if result.get("receipt"):
                reporting.adopt_terminal_receipt(result["receipt"])
                current = reporting._current.get()
                if current is not None:
                    reporting._current.reset(current.current_token)
            return bool(result.get("candidate_activated"))
        except BaseException as exc:
            actual = _git(root, "rev-parse", "HEAD")
            torn = (interrupted_pull_marker(root).exists()
                    and not commit.tree_whole_at(_git_command(), root, manifest["base_head"]))
            if moved or actual != manifest["base_head"] or torn:
                moved = True
                receipt["state"] = "incomplete"
                receipt["actual_head"] = actual
                receipt["followup"] = str(exc)
                reporting.record_followup("pinned_activation", str(exc))
                try:
                    _write_receipt(path, receipt)
                except OSError as persistence:
                    reporting.record_followup("candidate_receipt", str(persistence))
                    print(f"Candidate receipt could not be updated ({persistence}); native recovery remains owed.", file=sys.stderr)
                reporting.finalize_update_receipt("partial")
                print("Pinned activation incomplete after source mutation; prior generations and native completion/restart debt retained.", file=sys.stderr)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                return False
            interrupted_pull_marker(root).unlink(missing_ok=True)
            commit.disarm_commit_obligations()
            try:
                _resume(token)
            finally:
                reporting.finalize_update_receipt("failed", stop_reason=str(exc))
            raise
        finally:
            try:
                recover_paused()
            finally:
                atexit.unregister(recover_paused)


def _activation_snapshot() -> tuple[str | None, dict]:
    from types import SimpleNamespace
    from hermes_cli.update_cmd import _m, _record_pre_update_backup_outcome, _record_snapshot_stage
    from hermes_cli.update_inventory import collect_runtime_inventory, record_plan_in_receipt

    args = SimpleNamespace(no_backup=False, backup=False)
    snapshot_id = _m()._run_pre_update_backup(args)
    _record_pre_update_backup_outcome(args, snapshot_id)
    _record_snapshot_stage(args, snapshot_id)
    plan = collect_runtime_inventory()
    record_plan_in_receipt(plan)
    return snapshot_id, plan.to_dict()


def command(args, root: Path) -> None:
    """Explicit CLI boundary; gateway callbacks never preflight their own process."""
    try:
        incompatible = ("gateway", "check", "plan", "list_venv_holders", "no_backup", "backup", "keep_stash",
                        "branch", "switch_branch", "force", "force_venv", "set_channel", "install_id", "channel", "no_gateway_restart")
        if any(getattr(args, name, False) for name in incompatible):
            raise ValueError("Prepare/activate must run alone from the owning source install's terminal")
        if getattr(args, "prepare", None):
            receipt = prepare(root, args.prepare)
            print(f"Prepared {args.prepare}; active install unchanged. Activate explicitly: hermes update --activate \"{receipt}\"")
        else:
            if activate(root, Path(args.activate)):
                print("Pinned source update activated.")
            else:
                print("Pinned source update needs follow-up; run hermes update to finish the retained obligations.")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Pinned update refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def handle_if_requested(args, root: Path) -> bool:
    """Retarget the owner before either update path's preflight/dispatch."""
    from hermes_cli.update_owning_install import retarget_to_owning_install

    retarget_to_owning_install(root)
    if not (getattr(args, "prepare", None) or getattr(args, "activate", None)):
        return False
    command(args, root)
    return True
