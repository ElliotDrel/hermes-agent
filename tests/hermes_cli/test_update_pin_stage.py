"""Real disposable Git + uv staging/relocation; never operate on a live install.

Only PM's builder (``pm.build_environment``, which needs its pinned toolchain
store) and its dependency publication are replaced by real-uv / recording
stand-ins; Git, relocation, promotion, rollback and receipts are real.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from hermes_cli import update_pin_stage as staging
from hermes_cli import update_pin_stage_env as stage_env


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def fake_pm_build(*, source, out, extras=(), no_install_project=False, frozen=True, explicit=False, **_):
    """PM's builder contract (relocatable, dependency-only) realised with plain uv."""
    assert no_install_project and frozen and explicit
    subprocess.run(["uv", "venv", str(out), "--python", sys.executable, "--relocatable", "--no-project"],
                   check=True)
    subprocess.run(["uv", "sync", "--frozen", "--no-install-project", "--python", str(stage_env.candidate_python(out.parent)),
                    *[arg for extra in extras for arg in ("--extra", extra)]],
                   cwd=source, check=True, env={**stage_env.clean_env(), "UV_PROJECT_ENVIRONMENT": str(out)})
    return stage_env.candidate_python(out.parent)


def _rmtree_readonly(func, path, *_):
    os.chmod(path, stat.S_IWRITE)  # git object files are read-only; Windows refuses to unlink them
    func(path)


@pytest.fixture
def short_tmp(tmp_path):
    """A scratch root short enough for the staged tree to stay under Windows MAX_PATH.

    The tests clone a whole checkout beside the install and build a venv in it (~170 chars of
    fixed names below the root), while the runner nests ``tmp_path`` under TMPDIR, the run dir
    and the test name. Past ~90 chars git cannot even set its cwd, so the root moves to the
    drive root when ``tmp_path`` is too deep."""
    if len(str(tmp_path)) <= 60:
        yield tmp_path
        return
    root = Path(tempfile.mkdtemp(prefix="hp", dir=tmp_path.anchor))
    try:
        yield root
    finally:
        # A test may have chdir'ed into the root (monkeypatch restores it only after this teardown).
        if Path.cwd().is_relative_to(root):
            os.chdir(root.parent)
        shutil.rmtree(root, **({"onexc": _rmtree_readonly} if sys.version_info >= (3, 12)
                               else {"onerror": _rmtree_readonly}))


@pytest.fixture
def install(short_tmp, monkeypatch):
    tmp_path = short_tmp
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "live")
    git(origin, "config", "user.email", "offline@example.invalid")
    git(origin, "config", "user.name", "Offline test")
    (origin / ".gitignore").write_text("venv/\n.venv/\n__pycache__/\n*.egg-info/\nbuild/\n")
    (origin / "pyproject.toml").write_text(
        '[project]\nname="stage-fixture"\nversion="1.0"\nrequires-python=">=3.11"\n'
        'dependencies=["six==1.17.0"]\n[project.optional-dependencies]\nall=[]\n')
    package = origin / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text("VALUE = 'old'\n")
    (package / "update_pin_stage.py").write_text("# Fixture activation-capability marker\n")
    for module in ("run_agent", "model_tools", "toolsets"):
        (origin / f"{module}.py").write_text("VALUE = 'fixture'\n")
    subprocess.run(["uv", "lock"], cwd=origin, check=True)
    git(origin, "add", ".")
    git(origin, "commit", "-m", "old")
    old_sha = git(origin, "rev-parse", "HEAD")
    active = tmp_path / "installed"
    subprocess.run(["git", "clone", str(origin), str(active)], check=True)
    subprocess.run(["uv", "venv", str(active / "venv"), "--python", sys.executable, "--relocatable"], check=True)
    (active / "venv" / "active-sentinel").write_text("untouched")
    (package / "main.py").write_text("VALUE = 'candidate'\ndef check():\n    print(VALUE)\nif __name__ == '__main__':\n    check()\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "published")
    sha = git(origin, "rev-parse", "HEAD")
    staging.get_hermes_home().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(staging, "_require_owner", lambda root: None)
    monkeypatch.setattr("pm.build_environment", fake_pm_build)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", lambda **kw: [])
    published = []
    monkeypatch.setattr(staging, "run_complete_install", lambda root, previous: published.append((root, previous)))
    return active, origin, old_sha, sha, published


@pytest.mark.platforms("windows")
def test_published_pin_stage_and_activation_keep_active_untouched(install, monkeypatch):
    active, origin, old_sha, sha, published = install
    before = (active / ".git" / "HEAD").read_bytes()
    data = staging.stage(active, sha)
    candidate = Path(data["candidate"])
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert (active / ".git" / "HEAD").read_bytes() == before
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    assert git(candidate, "rev-parse", "HEAD") == sha
    assert not published, "staging must not publish dependencies into the install's PM state"
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
        if any("_verify_started_gateway" in str(arg) for arg in cmd):
            launches.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert launches[0][0][0] == str(stage_env.candidate_python(active))
    assert "PYTHONPATH" not in launches[0][1]["env"]
    assert "-I" in launches[0][0] and "-I" in launches[1][0]
    assert not staging.pending_path(active).exists()
    previous = active.parent / f".{active.name}.previous-{data['token']}"
    assert published == [(active.resolve(), active.resolve().parent / previous.name)],"dependencies publish once, at the final path, before launch"
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"
    assert git(active, "rev-parse", "HEAD") == sha
    assert git(previous, "rev-parse", "HEAD") == old_sha
    assert (previous / "venv" / "active-sentinel").read_text() == "untouched"
    output = real_run([str(stage_env.candidate_python(active)), "-I", "-c",
        "from hermes_cli.main import VALUE; import hermes_cli.main, six, sys; from pathlib import Path; "
        "assert Path(hermes_cli.main.__file__).parent.parent == Path(sys.prefix).parent; print(VALUE)"],
        cwd=active.parent, text=True, capture_output=True, check=True).stdout
    assert output.strip() == "candidate", "Relocated non-editable environment must import candidate, not old checkout"
    output = real_run([str(stage_env.candidate_python(active)), "-I", "-m", "hermes_cli.main", "--help"],
                      cwd=active.parent, text=True, capture_output=True, check=True).stdout
    assert output.strip() == "candidate"


@pytest.mark.platforms("windows")
def test_marker_is_not_the_name_a_loaded_pre_0232_restart_handler_reads(install):
    """Old loaded /restart would run ``-m hermes_cli.update_stage`` (no entrypoint upstream) and strand
    the gateway; a new pin must be invisible to it, while an old-staged pin stays activatable."""
    active, _, old_sha, sha, _ = install
    data = staging.stage(active, sha)
    assert staging.pending_path(active).name == f".{active.name}.pin-stage.json"
    assert not (active.parent / f".{active.name}.staged-update.json").exists()
    legacy = active.parent / f".{active.name}.staged-update.json"
    shutil.move(staging.pending_path(active), legacy)
    assert staging.load_pending(active, data["token"])["sha"] == sha
    with pytest.raises(ValueError, match="already staged"):
        staging.stage(active, sha)


@pytest.mark.platforms("windows")
def test_activation_from_executing_candidate_interpreter(short_tmp):
    """Whole Hermes checkout, candidate-owned imports, real Windows promotion.

    Only transport start/discovery/status and PM dependency publication are
    intercepted, in the fresh promoted subprocess; no development source prepend.
    """
    tmp_path = short_tmp
    source = Path(staging.__file__).resolve().parent.parent
    active = tmp_path / "installed"
    subprocess.run(["git", "-c", "core.longpaths=true", "clone", "--local", str(source), str(active)], check=True)
    git(active, "config", "core.longpaths", "true")
    # Test the working implementation before the source intent is committed.
    for name in ("update_pin_stage.py", "update_pin_stage_env.py", "update_pin_stage_recovery.py",
                 "update_cmd_windows.py",
                 "gateway_supervised_restart.py"):
        shutil.copyfile(source / "hermes_cli" / name, active / "hermes_cli" / name)
    git(active, "config", "user.email", "offline@example.invalid")
    git(active, "config", "user.name", "Offline test")
    git(active, "add", "hermes_cli")
    git(active, "commit", "--allow-empty", "-m", "disposable tested candidate")
    sha = old_sha = git(active, "rev-parse", "HEAD")
    token = "0123456789abcdef0123456789abcdef"
    candidate = tmp_path / f".installed.candidate-{token}"
    subprocess.run(["git", "-c", "core.longpaths=true", "clone", "--local", str(active), str(candidate)], check=True)
    git(candidate, "config", "core.longpaths", "true")
    shutil.copytree(Path(sys.prefix), candidate / "venv")
    subprocess.run(["uv", "venv", str(active / "venv"), "--python", sys.executable, "--relocatable"], check=True)
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
from hermes_cli import update_pin_stage as staging
assert Path(staging.__file__).parent.parent == Path({str(candidate)!r})
assert 'hermes_cli.gateway_windows' not in sys.modules
published = []
staging.run_complete_install = lambda root, previous: published.append((str(root), str(previous)))
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
assert len(launches) == 1 and len(published) == 1
print(json.dumps({{'executing_python': sys.executable, 'promoted_sha': staging._git(Path({str(active)!r}), 'rev-parse', 'HEAD')}}))
'''
    env = stage_env.clean_env(HERMES_HOME=str(staging.get_hermes_home()))
    result = subprocess.run([str(stage_env.candidate_python(candidate)), "-I", "-c", script],
                            cwd=active.parent, env=env, text=True, capture_output=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["promoted_sha"] == sha
    assert evidence["executing_python"] == str(stage_env.candidate_python(candidate))
    assert not candidate.exists()
    assert not staging.pending_path(active).exists()
    previous = active.parent / f".{active.name}.previous-{data['token']}"
    assert git(previous, "rev-parse", "HEAD") == old_sha
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"
    assert f"previous_sha={old_sha}; target_sha={sha}" in receipt["steps"][0]["detail"]


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


@pytest.mark.platforms("windows")
def test_start_failure_never_renames_or_retries_booting_generation(install, monkeypatch):
    active, _, old_sha, sha, _ = install
    data = staging.stage(active, sha)
    from argparse import Namespace
    real_run = subprocess.run
    starts = []
    def run(cmd, **kwargs):
        if "gateway" in cmd and "start" in cmd:
            starts.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        if any("_verify_started_gateway" in str(arg) for arg in cmd):
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


@pytest.mark.platforms("windows")
def test_failures_never_change_active_and_swap_or_publication_failure_restores_it(install, monkeypatch):
    active, origin, old_sha, sha, published = install
    real_build = stage_env.build_candidate_environment
    def fail(candidate, extras):
        raise ValueError("dependency failure")
    monkeypatch.setattr(staging, "build_candidate_environment", fail)
    with pytest.raises(ValueError, match="dependency failure"):
        staging.stage(active, sha)
    assert not staging.pending_path(active).exists()
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    monkeypatch.setattr(staging, "build_candidate_environment", real_build)
    data = staging.stage(active, sha)
    from argparse import Namespace
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 123)
    with pytest.raises(ValueError, match="still running"):
        staging.activate(active, data["token"])
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    with pytest.raises(ValueError, match="token"):
        staging.activate(active, "0" * 32)
    rename = stage_env.rename_with_retry
    def fail_promotion(source, destination):
        if source == Path(data["candidate"]):
            raise OSError("locked candidate")
        rename(source, destination)
    monkeypatch.setattr(staging, "rename_with_retry", fail_promotion)
    monkeypatch.chdir(active.parent)
    with pytest.raises(OSError, match="locked candidate"):
        staging.activate(active, data["token"])
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    monkeypatch.setattr(staging, "rename_with_retry", rename)
    candidate_main = Path(data["candidate"]) / "hermes_cli" / "main.py"
    candidate_main.write_text("VALUE = 'tampered'\n")
    with pytest.raises(ValueError, match="clean checkout"):
        staging.activate(active, data["token"])
    assert git(active, "rev-parse", "HEAD") == old_sha
    git(Path(data["candidate"]), "checkout", "--", "hermes_cli/main.py")

    def failed_publication(root, previous):
        raise subprocess.CalledProcessError(1, "pm sync")
    monkeypatch.setattr(staging, "run_complete_install", failed_publication)
    with pytest.raises(subprocess.CalledProcessError):
        staging.activate(active, data["token"])
    assert git(active, "rev-parse", "HEAD") == old_sha, "a failed dependency publication rolls the tree back"
    assert (active / "venv" / "active-sentinel").read_text() == "untouched"
    assert Path(data["candidate"]).is_dir()
    monkeypatch.setattr(staging, "run_complete_install", lambda root, previous: published.append(1))
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
    monkeypatch.setattr(staging, "rename_with_retry", fail_promotion)
    with pytest.raises(OSError, match="locked candidate"):
        staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert len(launches) == 1 and launches[0][0] == str(stage_env.candidate_python(active))
    receipt = json.loads((staging.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "failed"
    assert git(active, "rev-parse", "HEAD") == old_sha
    assert json.loads(staging.pending_path(active).read_text())["sha"] == sha


def test_complete_install_carries_pre_pm_extras_once_and_defers_to_recorded_selection(monkeypatch, tmp_path):
    """First PM generation carries the retained old venv's extras; afterwards PM's ledger owns them."""
    import pm
    import pm.extras
    from hermes_cli import venv_sync
    calls = []
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(venv_sync, "collect_superseded_generations", lambda root: calls.append("gc"))
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda root: calls.append("launchers"))
    monkeypatch.setattr(pm.extras, "legacy_selection", lambda previous: ["all", "telegram"])
    monkeypatch.setattr(pm.extras, "first_generation_selection", lambda root: ["all"])
    stage_env.complete_install(tmp_path / "root", tmp_path / "previous")
    assert calls[0] == ((["all", "telegram"],), {"explicit": True, "project_root": tmp_path / "root",
                                                  "carried": ["telegram"]})
    assert calls[1:] == ["gc", "launchers"]
    calls.clear()
    monkeypatch.setattr(pm.extras, "first_generation_selection", lambda root: None)
    stage_env.complete_install(tmp_path / "root", tmp_path / "previous")
    assert calls[0] == ((None,), {"explicit": True, "project_root": tmp_path / "root", "carried": None})


def _recording_run(monkeypatch, *, start_rc=0):
    """Intercept only native gateway start/verify; everything else (git, uv) stays real."""
    real_run = subprocess.run
    starts = []

    def run(cmd, **kwargs):
        if "gateway" in cmd and "start" in cmd:
            starts.append(cmd)
            return subprocess.CompletedProcess(cmd, start_rc)
        if any("_verify_started_gateway" in str(arg) for arg in cmd):
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    return starts


@pytest.mark.platforms("windows")
def test_lock_contention_after_drain_restarts_the_untouched_gateway(install, monkeypatch):
    from argparse import Namespace
    from hermes_cli.update_lock import UpdateLock
    active, _, old_sha, sha, _ = install
    data = staging.stage(active, sha)
    monkeypatch.setattr(UpdateLock, "acquire", lambda self: False)
    starts = _recording_run(monkeypatch)
    with pytest.raises(ValueError, match="Another native update"):
        staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert len(starts) == 1 and starts[0][0] == str(stage_env.candidate_python(active))
    assert git(active, "rev-parse", "HEAD") == old_sha


@pytest.mark.platforms("windows")
def test_refusal_while_drained_gateway_still_exits_ends_with_a_running_gateway(install, monkeypatch):
    """The watcher fires at its drain deadline; a gateway still exiting must not strand the install."""
    from argparse import Namespace
    from hermes_cli import update_pin_stage_recovery as recovery
    active, _, old_sha, sha, _ = install
    data = staging.stage(active, sha)
    polls = iter([123, 123, 123, None, None])
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: next(polls, None))
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    starts = _recording_run(monkeypatch)
    with pytest.raises(ValueError, match="still running"):
        staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert len(starts) == 1 and git(active, "rev-parse", "HEAD") == old_sha


@pytest.mark.platforms("windows")
def test_unremovable_marker_neither_recovers_old_tree_nor_wedges_restart(install, monkeypatch):
    from argparse import Namespace
    from hermes_cli import update_pin_stage_recovery as recovery
    active, _, old_sha, sha, _ = install
    data = staging.stage(active, sha)
    real_unlink = Path.unlink

    def locked_unlink(self, *args, **kwargs):
        if self.name.endswith(".pin-stage.json"):
            raise PermissionError("locked")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked_unlink)
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    starts = _recording_run(monkeypatch)
    monkeypatch.chdir(active.parent)
    staging.run_stage_command(Namespace(stage=None, activate_stage=data["token"]), active)
    assert len(starts) == 1 and git(active, "rev-parse", "HEAD") == sha, "promoted tree launched exactly once"
    assert staging.pending_path(active).exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    with pytest.raises(ValueError, match="Stale"):
        staging.restart_candidate(active)
    assert staging.restart_candidate(active) is None, "a stale marker is cleared, never a permanent refusal"


@pytest.mark.platforms("windows")
def test_rollback_after_committed_dependency_sync_restores_pm_state(install, monkeypatch):
    active, _, old_sha, sha, _ = install
    data = staging.stage(active, sha)
    monkeypatch.chdir(active.parent)
    resyncs = []

    def committed_then_failed(root, previous):
        raise subprocess.CalledProcessError(stage_env.SYNC_COMMITTED_EXIT, "publish")
    monkeypatch.setattr(staging, "run_complete_install", committed_then_failed)
    monkeypatch.setattr(staging, "run_resync_previous",
                        lambda root, candidate: resyncs.append(git(root, "rev-parse", "HEAD")))
    with pytest.raises(subprocess.CalledProcessError):
        staging.activate(active, data["token"])
    assert resyncs == [old_sha], "PM is re-synced only after the previous tree is back in place"

    def failing_resync(root, candidate):
        raise subprocess.CalledProcessError(1, "resync")
    monkeypatch.setattr(staging, "run_resync_previous", failing_resync)
    with pytest.raises(RuntimeError, match="PM committed the candidate's lock"):
        staging.activate(active, data["token"])


def test_failure_after_committed_sync_is_distinguishable_from_a_clean_sync_failure(monkeypatch, tmp_path):
    import pm
    import pm.extras
    from hermes_cli import venv_sync
    monkeypatch.setattr(pm.extras, "first_generation_selection", lambda root: None)
    monkeypatch.setattr(venv_sync, "collect_superseded_generations", lambda root: None)

    def broken_launchers(root):
        raise OSError("launcher locked")
    monkeypatch.setattr(venv_sync, "publish_launchers", broken_launchers)
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: None)
    with pytest.raises(stage_env.PublicationFailedAfterSync):
        stage_env.complete_install(tmp_path / "root", tmp_path / "previous")

    def failed_sync(*a, **kw):
        raise RuntimeError("lock conflict")
    monkeypatch.setattr(pm, "sync_venv", failed_sync)
    with pytest.raises(RuntimeError, match="lock conflict") as caught:
        stage_env.complete_install(tmp_path / "root", tmp_path / "previous")
    assert not isinstance(caught.value, stage_env.PublicationFailedAfterSync)


@pytest.mark.platforms("windows")
def test_staging_carries_other_remotes_without_touching_the_install(install):
    active, origin, old_sha, sha, _ = install
    git(active, "remote", "add", "upstream", "https://example.invalid/upstream.git")
    before = (active / ".git" / "config").read_bytes()
    data = staging.stage(active, sha)
    assert git(Path(data["candidate"]), "remote", "get-url", "upstream") == "https://example.invalid/upstream.git"
    assert git(Path(data["candidate"]), "remote", "get-url", "origin") == git(active, "remote", "get-url", "origin")
    assert (active / ".git" / "config").read_bytes() == before
