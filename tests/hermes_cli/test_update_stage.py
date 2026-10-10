"""Real disposable Git + uv staging/relocation; never operate on a live install."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import update_stage as staging


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def install(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "live")
    git(origin, "config", "user.email", "offline@example.invalid")
    git(origin, "config", "user.name", "Offline test")
    (origin / ".gitignore").write_text("venv/\n.venv/\n__pycache__/\n*.egg-info/\nbuild/\n")
    (origin / "pyproject.toml").write_text(
        '[project]\nname="stage-fixture"\nversion="1.0"\nrequires-python=">=3.11"\n'
        'dependencies=[]\n[project.scripts]\nstage-check="hermes_cli.main:check"\n[project.optional-dependencies]\nall=[]\n'
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[tool.setuptools]\npy-modules=["run_agent", "model_tools", "toolsets"]\n'
        '[tool.setuptools.packages.find]\ninclude=["hermes_cli*"]\n')
    package = origin / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text("VALUE = 'old'\n")
    (package / "update_stage.py").write_text("# Fixture activation-capability marker\n")
    for module in ("run_agent", "model_tools", "toolsets"):
        (origin / f"{module}.py").write_text("VALUE = 'fixture'\n")
    subprocess.run(["uv", "lock"], cwd=origin, check=True)
    git(origin, "add", ".")
    git(origin, "commit", "-m", "old")
    old_sha = git(origin, "rev-parse", "HEAD")
    active = tmp_path / "installed"
    subprocess.run(["git", "clone", str(origin), str(active)], check=True)
    subprocess.run(["uv", "venv", str(active / "venv"), "--python", sys.executable,
                    "--relocatable"], check=True)
    (active / "venv" / "active-sentinel").write_text("untouched")
    (package / "main.py").write_text("VALUE = 'candidate'\ndef check():\n    print(VALUE)\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "published")
    sha = git(origin, "rev-parse", "HEAD")
    monkeypatch.setattr("tools.lazy_deps.active_features", lambda: [])
    monkeypatch.setattr("hermes_cli.tools_config.active_restorable_python_tool_dependencies", lambda: [])
    home = staging.get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [stage-plugin]\n")
    plugin = home / "plugins" / "stage-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: stage-plugin\npython_dependencies: ['six==1.17.0']\n")
    monkeypatch.setattr(staging, "_require_owner", lambda root: None)
    monkeypatch.setattr("hermes_cli.managed_uv.resolve_uv", lambda: "uv")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", lambda **kw: [])
    return active, origin, old_sha, sha


@pytest.mark.windows_only
def test_published_pin_stage_and_activation_keep_active_untouched(install, monkeypatch):
    active, origin, old_sha, sha = install
    before = (active / ".git" / "HEAD").read_bytes()
    data = staging.stage(active, sha)
    candidate = Path(data["candidate"])
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert (active / ".git" / "HEAD").read_bytes() == before
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    assert git(candidate, "rev-parse", "HEAD") == sha
    # Publication moves after staging. Activation must NOT fetch latest live.
    (origin / "hermes_cli" / "main.py").write_text("VALUE = 'later'\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "later live")
    monkeypatch.chdir(active.parent)
    from argparse import Namespace
    real_run = subprocess.run
    launches = []
    def run(cmd, **kwargs):
        if "gateway" in cmd and "start" in cmd:
            launches.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("hermes_cli.gateway_windows._wait_for_gateway_ready", lambda **kw: True)
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda: {"code_sha": sha})
    staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert launches[0][0][0] == str(staging.venv_python_path(active / "venv"))
    assert launches[0][1]["env"]["PYTHONPATH"] == str(active)
    assert not staging.pending_path(active).exists()
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"
    assert git(active, "rev-parse", "HEAD") == sha
    previous = active.parent / f".{active.name}.previous-{data['token']}"
    assert git(previous, "rev-parse", "HEAD") == old_sha
    assert (previous / "venv" / "active-sentinel").read_text() == "untouched"
    output = subprocess.check_output([str(staging.venv_python_path(active / "venv")), "-I", "-c",
        "from hermes_cli.main import VALUE; import hermes_cli.main, six, sys; from pathlib import Path; assert Path(hermes_cli.main.__file__).parent.parent == Path(sys.prefix).parent; print(VALUE)"], cwd=active.parent, text=True)
    assert output.strip() == "candidate", "Relocated non-editable environment must import candidate, not old checkout"
    script = active / "venv" / "Scripts" / "stage-check.exe"
    assert subprocess.check_output([str(script)], cwd=active.parent, text=True).strip() == "candidate"


@pytest.mark.windows_only
def test_activation_from_executing_candidate_interpreter(install):
    """Exercise actual Windows relocation with the helper's staged executable.

    The tiny fixture borrows development libraries to import the production helper;
    no filesystem, owner, holder, probe or process-lifecycle check is mocked.
    Only gateway launch/readiness/status attestation are intercepted.
    """
    active, _, old_sha, sha = install
    data = staging.stage(active, sha)
    candidate = Path(data["candidate"])
    source = Path(staging.__file__).resolve().parent.parent
    libraries = Path(sys.prefix) / "Lib" / "site-packages"
    script = f'''
import json, subprocess, sys
from pathlib import Path
from argparse import Namespace
sys.path[:0] = [{str(source)!r}, {str(libraries)!r}]
assert Path(sys.prefix) == Path({str(candidate / 'venv')!r})
assert Path(sys.executable) == Path({str(staging.venv_python_path(candidate / 'venv'))!r})
from hermes_cli import update_stage as staging
from hermes_cli import gateway_windows
from gateway import status
real_run = subprocess.run
launches = []
def run(cmd, **kwargs):
    if 'gateway' in cmd and 'start' in cmd:
        launches.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    return real_run(cmd, **kwargs)
subprocess.run = run
gateway_windows._wait_for_gateway_ready = lambda **kwargs: True
status.read_runtime_status = lambda: {{'code_sha': {sha!r}}}
staging.run_stage_command(Namespace(stage=None, activate_stage={data['token']!r}), Path({str(active)!r}))
assert len(launches) == 1
print(json.dumps({{'executing_python': sys.executable, 'promoted_sha': staging._git(Path({str(active)!r}), 'rev-parse', 'HEAD')}}))
'''
    env = staging._candidate_env(candidate)
    env["HERMES_HOME"] = str(staging.get_hermes_home())
    result = subprocess.run([str(staging.venv_python_path(candidate / "venv")), "-c", script],
                            cwd=active.parent, env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["promoted_sha"] == sha
    assert evidence["executing_python"] == str(staging.venv_python_path(candidate / "venv"))
    assert not candidate.exists()
    assert not staging.pending_path(active).exists()
    previous = active.parent / f".{active.name}.previous-{data['token']}"
    assert git(previous, "rev-parse", "HEAD") == old_sha
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"


@pytest.mark.windows_only
def test_failures_never_change_active_and_swap_failure_restores_it(install, monkeypatch):
    active, origin, old_sha, sha = install
    real_prepare = staging._prepare_environment
    def fail(root):
        raise ValueError("dependency failure")
    monkeypatch.setattr(staging, "_prepare_environment", fail)
    with pytest.raises(ValueError, match="dependency failure"):
        staging.stage(active, sha)
    assert not staging.pending_path(active).exists()
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    monkeypatch.setattr(staging, "_prepare_environment", real_prepare)
    data = staging.stage(active, sha)
    from argparse import Namespace
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 123)
    with pytest.raises(ValueError, match="still running"):
        staging.activate(active, data["token"])
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    with pytest.raises(ValueError, match="token"):
        staging.activate(active, "0" * 32)
    from hermes_cli import managed_uv
    rename = managed_uv._rename_with_retry
    def fail_promotion(source, destination):
        if source == Path(data["candidate"]):
            raise OSError("locked candidate")
        rename(source, destination)
    monkeypatch.setattr(managed_uv, "_rename_with_retry", fail_promotion)
    monkeypatch.chdir(active.parent)
    with pytest.raises(OSError, match="locked candidate"):
        staging.activate(active, data["token"])
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    monkeypatch.setattr(managed_uv, "_rename_with_retry", rename)
    candidate_main = Path(data["candidate"]) / "hermes_cli" / "main.py"
    candidate_main.write_text("VALUE = 'tampered'\n")
    with pytest.raises(ValueError, match="clean checkout"):
        staging.activate(active, data["token"])
    assert git(active, "rev-parse", "HEAD") == old_sha
    git(Path(data["candidate"]), "checkout", "--", "hermes_cli/main.py")
    # Exercise command-boundary recovery and the real failed receipt, mocking
    # only native gateway launch (test must never start a real gateway).
    launches = []
    real_run = subprocess.run
    def refuse_start(cmd, **kwargs):
        if "gateway" in cmd and "start" in cmd:
            launches.append(cmd)
            return subprocess.CompletedProcess(cmd, 1)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", refuse_start)
    monkeypatch.setattr(managed_uv, "_rename_with_retry", fail_promotion)
    with pytest.raises(OSError, match="locked candidate"):
        staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert len(launches) == 1 and launches[0][0] == str(staging.venv_python_path(active / "venv"))
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "failed"
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert json.loads(staging.pending_path(active).read_text())["sha"] == sha
