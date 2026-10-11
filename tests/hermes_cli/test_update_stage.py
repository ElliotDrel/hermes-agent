"""Real disposable Git + uv staging/relocation; never operate on a live install."""
import json
from pathlib import Path
import subprocess
import sys
import shutil
import io
import zipfile

import pytest

from hermes_cli import update_stage as staging


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def launcher_template(root, format="distlib"):
    """An offline native installer-style launcher, never a production file."""
    if format == "uv":
        wheel = root / "venv/launcher_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as bundle:
            bundle.writestr("launcher_fixture.py", "def main():\n    print('template')\n")
            bundle.writestr("launcher_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: launcher-fixture\nVersion: 1.0\n")
            bundle.writestr("launcher_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: offline-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            bundle.writestr("launcher_fixture-1.0.dist-info/entry_points.txt", "[console_scripts]\nhermes = launcher_fixture:main\n")
            bundle.writestr("launcher_fixture-1.0.dist-info/RECORD", "")
        subprocess.run(["uv", "pip", "install", "--python", str(staging.venv_python_path(root / "venv")), "--no-deps", str(wheel)], check=True)
        return
    import ensurepip
    wheel = next((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
    with zipfile.ZipFile(wheel) as bundle:
        stub = bundle.read("pip/_vendor/distlib/t64.exe")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("__main__.py", "print('template')\n")
    (root / "venv/Scripts/hermes.exe").write_bytes(stub + b"#!python.exe\n" + archive.getvalue())


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
    (origin / "setup.py").write_text(
        'from setuptools import setup\n'
        'from setuptools.command.bdist_wheel import bdist_wheel\n'
        'class Guard(bdist_wheel):\n'
        '    def run(self):\n'
        '        raise RuntimeError("Source-only fixture forbids wheels")\n'
        'setup(cmdclass={"bdist_wheel": Guard})\n')
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
    (package / "main.py").write_text("VALUE = 'candidate'\ndef check():\n    print(VALUE)\nif __name__ == '__main__':\n    check()\n")
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
        if any('_verify_started_gateway' in str(arg) for arg in cmd):
            launches.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("hermes_cli.gateway_windows._wait_for_gateway_ready", lambda **kw: True)
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda: {"code_sha": sha})
    staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert launches[0][0][0] == str(staging.venv_python_path(active / "venv"))
    assert "PYTHONPATH" not in launches[0][1]["env"]
    assert "-I" in launches[0][0] and "-I" in launches[1][0]
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
    assert not (active / "venv" / "Scripts" / "stage-check.exe").exists()
    output = subprocess.check_output(
        [str(staging.venv_python_path(active / "venv")), "-I", "-m", "hermes_cli.main", "--help"],
        cwd=active.parent, text=True)
    assert output.strip() == "candidate"


@pytest.mark.windows_only
@pytest.mark.parametrize("launcher_format", ["uv", "distlib"])
def test_activation_from_executing_candidate_interpreter(tmp_path, monkeypatch, launcher_format):
    """Whole Hermes checkout, candidate-owned imports, real Windows promotion.

    Only transport start/discovery/status are intercepted, in fresh promoted
    subprocesses; no development source prepend or parent readiness preimports.
    """
    source = Path(staging.__file__).resolve().parent.parent
    active = tmp_path / "installed"
    subprocess.run(["git", "-c", "core.longpaths=true", "clone", "--local", str(source), str(active)], check=True)
    git(active, "config", "core.longpaths", "true")
    # Test the working implementation before the source intent is committed.
    shutil.copyfile(source / "hermes_cli/update_stage.py", active / "hermes_cli/update_stage.py")
    git(active, "config", "user.email", "offline@example.invalid")
    git(active, "config", "user.name", "Offline test")
    git(active, "add", "hermes_cli/update_stage.py")
    git(active, "commit", "--allow-empty", "-m", "disposable tested candidate")
    sha = old_sha = git(active, "rev-parse", "HEAD")
    token = "0123456789abcdef0123456789abcdef"
    candidate = tmp_path / f".installed.candidate-{token}"
    subprocess.run(["git", "-c", "core.longpaths=true", "clone", "--local", str(active), str(candidate)], check=True)
    git(candidate, "config", "core.longpaths", "true")
    shutil.copytree(Path(sys.prefix), candidate / "venv")
    subprocess.run(["uv", "venv", str(active / "venv"), "--python", sys.executable, "--relocatable"], check=True)
    launcher_template(active, launcher_format)
    staging._preserve_console_launchers(candidate, active)
    site = candidate / "venv/Lib/site-packages"
    # Remove any development editable/source bindings in the copied dependencies.
    for pth in site.glob("*.pth"):
        if "editable" in pth.name or str(source).replace("\\", "/") in pth.read_text().replace("\\", "/"):
            pth.unlink()
    (site / "hermes-checkout.pth").write_text("import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent))\n")
    data = dict(schema=1, token=token, sha=sha, previous_sha=old_sha,
                root=str(active.resolve()), candidate=str(candidate.resolve()), home=str(staging.get_hermes_home().resolve()))
    staging.pending_path(active).write_text(json.dumps(data))
    script = f'''
import json, subprocess, sys
from pathlib import Path
from argparse import Namespace
assert Path(sys.prefix) == Path({str(candidate / 'venv')!r})
assert Path(sys.executable) == Path({str(staging.venv_python_path(candidate / 'venv'))!r})
from hermes_cli import update_stage as staging
assert Path(staging.__file__).parent.parent == Path({str(candidate)!r})
assert 'hermes_cli.gateway_windows' not in sys.modules
real_run = subprocess.run
launches = []
def run(cmd, **kwargs):
    if 'gateway' in cmd and 'start' in cmd:
        launches.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    if any('_verify_started_gateway' in str(arg) for arg in cmd):
        # Execute the REAL promoted verification in its fresh interpreter. Stub
        # only transport discovery/state, simulating stale status then the pin.
        cmd = list(cmd)
        cmd[-1] = "from hermes_cli import gateway_windows as g; from gateway import status; g._wait_for_gateway_ready=lambda **kw: [123]; g._live_gateway_pids=lambda *a: [123]; rows=iter([{{'pid':123,'code_sha':'old'}},{{'pid':123,'code_sha':{sha!r}}}]); status.read_runtime_status=lambda: next(rows); " + cmd[-1]
    return real_run(cmd, **kwargs)
subprocess.run = run
staging.run_stage_command(Namespace(stage=None, activate_stage={data['token']!r}), Path({str(active)!r}))
assert len(launches) == 1
print(json.dumps({{'executing_python': sys.executable, 'promoted_sha': staging._git(Path({str(active)!r}), 'rev-parse', 'HEAD')}}))
'''
    env = staging._candidate_env(candidate)
    env["HERMES_HOME"] = str(staging.get_hermes_home())
    result = subprocess.run([str(staging.venv_python_path(candidate / "venv")), "-I", "-c", script],
                            cwd=active.parent, env=env, text=True, capture_output=True, timeout=180)
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
    assert f"previous_sha={old_sha}; target_sha={sha}" in receipt["steps"][0]["detail"]
    for entry in ("hermes", "hermes-acp", "hermes-agent"):
        result = subprocess.run([str(active / f"venv/Scripts/{entry}.exe"), "--help"],
                                cwd=active.parent, env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "usage:" in result.stdout
    result = subprocess.run([str(active / "venv/Scripts/hermes-acp.exe"), "--check"],
                            cwd=active.parent, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Hermes ACP check OK" in result.stdout


def test_readiness_polls_sha_and_requires_matching_live_pid(monkeypatch):
    from hermes_cli import gateway_windows
    from gateway import status
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **kw: [123])
    monkeypatch.setattr(gateway_windows, "_live_gateway_pids", lambda *a: [123])
    rows = iter([{"pid": 123, "code_sha": "old"}, {"pid": 999, "code_sha": "pin"},
                 {"pid": 123, "code_sha": "pin"}])
    monkeypatch.setattr(status, "read_runtime_status", lambda: next(rows))
    monkeypatch.setattr(staging.time, "sleep", lambda _: None)
    staging._verify_started_gateway("pin")
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **kw: [])
    with pytest.raises(ValueError, match="did not become ready"):
        staging._verify_started_gateway("pin", timeout_s=0)


@pytest.mark.windows_only
def test_start_failure_never_renames_or_retries_booting_generation(install, monkeypatch):
    active, _, old_sha, sha = install
    data = staging.stage(active, sha)
    from argparse import Namespace
    real_run = subprocess.run
    starts = []
    def run(cmd, **kwargs):
        if "gateway" in cmd and "start" in cmd:
            starts.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        if any('_verify_started_gateway' in str(arg) for arg in cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert len(starts) == 1
    assert git(active, "rev-parse", "HEAD") == sha
    assert git(active.parent / f".{active.name}.previous-{data['token']}", "rev-parse", "HEAD") == old_sha
    assert not staging.pending_path(active).exists()
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "failed"


@pytest.mark.windows_only
def test_failures_never_change_active_and_swap_failure_restores_it(install, monkeypatch):
    active, origin, old_sha, sha = install
    real_prepare = staging._prepare_environment
    def fail(root, installed=None):
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
