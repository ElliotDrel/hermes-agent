"""Pinned, inert preparation and explicit Windows detached-gateway activation.

Candidates and retained previous installations are siblings of the install. No
live source, dependency, configuration or gateway is changed during preparation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from hermes_constants import get_hermes_home, venv_python_path
from utils import atomic_json_write


def pending_path(root: Path) -> Path:
    return root.parent / f".{root.name}.staged-update.json"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=True,
                            timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    return result.stdout.strip()


def _clean(root: Path) -> None:
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"Staging/activation requires a clean checkout: {root}")


def _candidate_env(root: Path) -> dict:
    from hermes_cli.managed_uv import managed_python_env
    env = managed_python_env(root)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "UV_NO_CONFIG", "_HERMES_GATEWAY"):
        env.pop(name, None)
    env.pop("UV_MANAGED_PYTHON", None)
    for name in ("UV_CONSTRAINT", "UV_BUILD_CONSTRAINT", "UV_OVERRIDE", "PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT"):
        env.pop(name, None)
    env.update(VIRTUAL_ENV=str(root / "venv"), UV_PROJECT_ENVIRONMENT=str(root / "venv"),
               UV_PYTHON_DOWNLOADS="never", PYTHONDONTWRITEBYTECODE="1")
    return env


def _prepare_environment(candidate: Path) -> None:
    # Reuse the native managed uv resolver and streamed locked installation.
    # Non-editable + relocatable is essential: this entire tree is renamed at activation.
    from hermes_cli.managed_uv import resolve_uv, _stream_sync
    uv = resolve_uv()
    if not uv:
        raise ValueError("Staging requires the native managed uv installer.")
    env = _candidate_env(candidate)
    subprocess.run([uv, "venv", str(candidate / "venv"), "--python", sys.executable,
                    "--no-python-downloads", "--relocatable", "--no-config"],
                   cwd=candidate, env=env, check=True)
    status, reason = _stream_sync(
        [uv, "sync", "--extra", "all", "--locked", "--no-editable",
         "--python", str(venv_python_path(candidate / "venv"))], cwd=candidate, env=env)
    if status:
        raise ValueError(f"Candidate dependency installation failed: {reason}")
    # Console scripts must resolve the SOURCE checkout, not the wheel's
    # site-packages copy (main.PROJECT_ROOT owns updates/gateway launchers).
    # A relative-to-sys.prefix pth survives whole-tree relocation; editable
    # setuptools finders and console-script absolute paths do not.
    site = candidate / "venv" / "Lib" / "site-packages"
    (site / "hermes-checkout.pth").write_text(
        "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent))\n",
        encoding="utf-8")
    _restore_declared_dependencies(candidate, uv, env)
    _probe(candidate)


def _restore_declared_dependencies(candidate: Path, uv: str, env: dict) -> None:
    # Reuse native declarations, never disable plugins or write profile config.
    from hermes_cli.plugin_python_deps import enabled_declarations, union_specs, core_constraints, unsupported_specs
    from hermes_cli.tools_config import active_restorable_python_tool_dependencies, restorable_python_tool_dependency
    from tools.lazy_deps import active_features, LAZY_DEPS
    declarations = enabled_declarations(get_hermes_home())
    if any(unsupported_specs(d.specs) for d in declarations):
        raise ValueError("Staging cannot restore direct-URL plugin dependencies; active installation unchanged.")
    specs = union_specs(declarations)
    for feature in active_features():
        specs.extend(LAZY_DEPS[feature])
    for name in active_restorable_python_tool_dependencies():
        dependency = restorable_python_tool_dependency(name)
        if dependency:
            specs.extend(dependency[1])
    if not specs:
        return
    constraints = candidate / "venv" / "stage-core-constraints.txt"
    constraints.write_text("\n".join(core_constraints(candidate)) + "\n", encoding="utf-8")
    # Only the separate candidate interpreter is a target; native uv policy and
    # constraints protect the locked core. A conflict aborts staging, not a plugin.
    subprocess.run([uv, "pip", "install", "--python", str(venv_python_path(candidate / "venv")),
                    "--constraint", "venv/stage-core-constraints.txt", *dict.fromkeys(specs)],
                   cwd=candidate, env=env, check=True, timeout=600)


def _probe(candidate: Path) -> None:
    # Import actual candidate code in a fresh interpreter; never bless active imports.
    env = _candidate_env(candidate)
    env["HERMES_HOME"] = str(candidate / "venv" / ".stage-probe-home")
    env["PYTHONPATH"] = str(candidate)
    subprocess.run([str(venv_python_path(candidate / "venv")), "-c",
                    "import hermes_cli.main, run_agent, model_tools, toolsets"],
                   cwd=candidate, env=env, check=True, timeout=90)


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
    from gateway.restart import is_gateway_supervisor_process, is_container_restart_context
    from gateway.status import read_runtime_status
    status = read_runtime_status() or {}
    if (is_gateway_supervisor_process() or is_container_restart_context()
            or _desktop_owns_gateway_lifecycle(project_root=root)):
        raise ValueError("Staging supports a detached gateway only; service/Desktop owners are unsupported.")
    from hermes_cli.profiles import profiles_to_serve
    from hermes_cli.gateway_supervised_restart import gateway_declares_external_supervisor
    from gateway.status import get_running_pid
    pid = get_running_pid()
    if pid and gateway_declares_external_supervisor(pid):
        raise ValueError("Externally supervised gateways cannot use staged detached activation.")
    homes = status.get("served_profiles") or []
    if len(homes) > 1 or len(profiles_to_serve(multiplex=True)) > 1:
        raise ValueError("Staging does not activate multi-profile fleets.")


def stage(root: Path, sha: str, branch: str = "live") -> dict:
    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("--stage requires the exact lowercase 40-character published commit SHA.")
    if not branch or branch.startswith("-"):
        raise ValueError("Invalid published branch.")
    _require_owner(root)
    _clean(root)
    if pending_path(root).exists():
        raise ValueError("A candidate is already staged; activate it or explicitly move its pending marker aside.")
    token = uuid4().hex
    candidate = root.parent / f".{root.name}.candidate-{token}"
    origin = _git(root, "remote", "get-url", "origin")
    # Fetch in the candidate only. The installed repo's HEAD, index, refs and venv stay untouched.
    subprocess.run(["git", "clone", "--no-checkout", "--single-branch", "--branch", branch,
                    "--", origin, str(candidate)], check=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}, timeout=300)
    tip = _git(candidate, "rev-parse", f"refs/remotes/origin/{branch}")
    # Exact pin must be published on this branch; later branch movement is harmless.
    _git(candidate, "merge-base", "--is-ancestor", sha, tip)
    _git(candidate, "checkout", "-B", branch, sha)
    if not (candidate / "hermes_cli" / "update_stage.py").is_file():
        raise ValueError("Published candidate predates staged activation support; publish a compatible candidate first.")
    _prepare_environment(candidate)
    _clean(candidate)
    if _git(candidate, "rev-parse", "HEAD") != sha:
        raise ValueError("Candidate revision changed during preparation.")
    data = {"schema": 1, "token": token, "sha": sha, "branch": branch,
            "root": str(root), "candidate": str(candidate), "home": str(get_hermes_home().resolve()),
            "previous_sha": _git(root, "rev-parse", "HEAD")}
    atomic_json_write(pending_path(root), data)
    return data


def load_pending(root: Path, token: str | None = None) -> dict | None:
    path = pending_path(root)
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
    if data:
        _require_owner(root)
        _validate(root, data)
    return data


def _validate(root: Path, data: dict) -> None:
    _clean(root)
    candidate = Path(data["candidate"])
    _clean(candidate)
    if (_git(root, "rev-parse", "HEAD") != data["previous_sha"]
            or _git(candidate, "rev-parse", "HEAD") != data["sha"]):
        raise ValueError("Installed or staged revision changed; refusing activation.")
    if not venv_python_path(candidate / "venv").is_file():
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
        raise ValueError("Gateway is still running; only explicit /restart may drain it before activation.")
    from hermes_cli.update_cmd_windows import _detect_venv_python_processes
    if _detect_venv_python_processes(project_root=root):
        raise ValueError("The installed environment still has live holders; close them and retry. Never use --force-venv.")
    candidate = Path(data["candidate"])
    previous = root.parent / f".{root.name}.previous-{token}"
    if previous.exists():
        raise ValueError(f"Retained previous installation already exists: {previous}")
    # Leave the current directory outside BOTH rename targets on Windows.
    os.chdir(root.parent)
    from hermes_cli.managed_uv import _rename_with_retry
    _rename_with_retry(root, previous)
    try:
        _rename_with_retry(candidate, root)
    except OSError:
        _rename_with_retry(previous, root)
        raise
    data["previous"] = str(previous)
    try:
        _probe(root)
    except BaseException:
        _rename_with_retry(root, candidate)
        _rename_with_retry(previous, root)
        raise
    return data


def run_stage_command(args, root: Path) -> None:
    from hermes_cli.update_lock import UpdateLock
    from hermes_cli.update_receipt import begin_update_receipt, record_step, finalize_update_receipt
    lock = UpdateLock()
    if not lock.acquire():
        raise ValueError("Another native update is running.")
    begin_update_receipt()
    try:
        if args.stage:
            data = stage(root, args.stage, args.branch or "live")
            record_step("stage", True, f"{data['sha']} at {data['candidate']}; active installation unchanged")
            print(f"Staged {data['sha']}. Active gateway unchanged. Send /restart yourself to activate.")
        else:
            data = activate(root, args.activate_stage)
            record_step("activate", True, f"{data['sha']}; previous installation retained at {data['previous']}")
            # Fresh code/environment owns native launch, never the pre-swap interpreter.
            env = _candidate_env(root)
            env["PYTHONPATH"] = str(root)
            # A failed native launch restores the complete old installation, not a
            # half-updated venv. A successfully running candidate is never renamed.
            try:
                subprocess.run([str(venv_python_path(root / "venv")), "-m", "hermes_cli.main",
                                "gateway", "start"], cwd=root.parent, env=env, check=True)
                from hermes_cli.gateway_windows import _wait_for_gateway_ready
                from gateway.status import read_runtime_status
                if not _wait_for_gateway_ready(timeout_s=15.0):
                    raise ValueError("Native gateway start did not become ready.")
                status = read_runtime_status() or {}
                if status.get("code_sha") != data["sha"]:
                    raise ValueError("Native start did not attest the pinned candidate SHA; inspect gateway status.")
            except BaseException:
                from gateway.status import get_running_pid
                if not get_running_pid():
                    from hermes_cli.managed_uv import _rename_with_retry
                    _rename_with_retry(root, Path(data["candidate"]))
                    _rename_with_retry(Path(data["previous"]), root)
                raise
            pending_path(root).unlink()
        finalize_update_receipt("success")
    except BaseException as exc:
        record_step("stage" if args.stage else "activate", False, str(exc))
        # Preparation never touches the gateway. Activation was user-requested;
        # if cutover refused/rolled back, recover the untouched old install using
        # its native start rather than strand the gateway after the manual drain.
        if not args.stage:
            from gateway.status import get_running_pid
            if not get_running_pid() and root.exists():
                env = _candidate_env(root)
                env["PYTHONPATH"] = str(root)
                try:
                    recovered = subprocess.run([str(venv_python_path(root / "venv")), "-m",
                        "hermes_cli.main", "gateway", "start"], cwd=root.parent, env=env)
                    record_step("activation_recovery_start", recovered.returncode == 0,
                                "native start attempted; inspect gateway status")
                except OSError as recovery_error:
                    record_step("activation_recovery_start", False, str(recovery_error))
        finalize_update_receipt("failed")
        raise
    finally:
        lock.release()


if __name__ == "__main__":
    # Bootstrap: explicit user activation from the prepared interpreter after a native stop.
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--activate-stage")
    mode.add_argument("--stage", metavar="SHA")
    parser.add_argument("--branch", default="live")
    args = parser.parse_args()
    run_stage_command(args, args.root)
