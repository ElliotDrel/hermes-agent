"""Pinned source update consumer: real advertised Git pins and PM selection IO."""
import json
from pathlib import Path
import subprocess

import pytest

from hermes_cli import staged_update as staged
from pm.environments import runtime_facts_path
from pm.lock import Facts
from tests.pm._pinned_fixtures import git

pytest_plugins = ["tests.pm._pinned_fixtures"]


@pytest.fixture
def prepared(candidate_owner, monkeypatch):
    owner, _source, base, target, initial = candidate_owner
    # Interpreter ownership is tested separately. Exercise the production clean-tree
    # admission and all receipt/Git/PM IO in these temporary source installs.
    def admission(root):
        staged._selection(root)
        staged._clean(root)
        return Path(root).resolve()
    monkeypatch.setattr(staged, "admit_owner", admission)
    def probe(root, manifest):
        # A real fresh interpreter in the candidate generation, no active runtime.
        from pm.environments import venv_command
        subprocess.run([*venv_command(root, Path(manifest["environment"]), ("-I",)), "-c", "import sqlite3"],
                       cwd=Path(manifest["source"]), check=True, timeout=30)
    monkeypatch.setattr(staged, "_preflight", probe)
    before = runtime_facts_path(owner).read_bytes()
    receipt_path = staged.prepare(owner, target)
    assert runtime_facts_path(owner).read_bytes() == before
    assert git(owner, "rev-parse", "HEAD") == base
    return owner, base, target, initial, receipt_path


def test_published_pin_and_dirty_source_refuse_before_candidate_creation(candidate_owner, monkeypatch):
    owner, _source, base, target, _initial = candidate_owner
    monkeypatch.setattr(staged, "admit_owner", lambda root: (staged._clean(root), root)[1])
    with pytest.raises(ValueError, match="advertised"):
        staged.prepare(owner, "0" * 40)
    (owner / "uncommitted.py").write_text("local edit")
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(ValueError, match="dirty"):
        staged.prepare(owner, target)
    assert runtime_facts_path(owner).read_bytes() == before
    assert git(owner, "rev-parse", "HEAD") == base


def test_activation_rechecks_selection_after_preflight(prepared, monkeypatch):
    owner, base, _target, initial, path = prepared
    def changed(root, manifest):
        Facts(runtime_facts_path(root)).record_state("venv", "changed", [], **initial)
    monkeypatch.setattr(staged, "_preflight", changed)
    monkeypatch.setattr(staged, "_drain", lambda: pytest.fail("Admission must run again before draining"))
    with pytest.raises(ValueError, match="selection changed"):
        staged.activate(owner, path)
    assert git(owner, "rev-parse", "HEAD") == base


@pytest.mark.parametrize("failure", ["before-commit", "promotion", "launch", "none"])
def test_activation_commit_boundaries_preserve_truthful_state(prepared, monkeypatch, failure):
    owner, base, target, initial, path = prepared
    before = runtime_facts_path(owner).read_bytes()
    monkeypatch.setattr(staged, "_activation_snapshot", lambda: (None, []))
    events = []
    token = {"resume_needed": True}
    monkeypatch.setattr(staged, "_drain", lambda: (events.append("drain"), token)[1])
    from hermes_cli import update_cmd_commit as commit
    # Native commit marker machinery has its own IO suites; retain real Git/PM IO
    # here and inject failures exactly at the transaction boundaries.
    monkeypatch.setattr(commit, "begin_update_attempt", lambda: None)
    monkeypatch.setattr(commit, "record_run_start", lambda *args: None)
    def arm(*args, **kwargs):
        events.append("commit")
        return "commit refused" if failure == "before-commit" else None
    monkeypatch.setattr(commit, "arm_commit_point", arm)
    monkeypatch.setattr(commit, "disarm_commit_obligations", lambda: events.append("disarm"))
    from hermes_cli import update_completion
    real_promote = staged.promote_candidate
    def promote(root, manifest):
        events.append("promote")
        assert git(root, "rev-parse", "HEAD") == target
        if failure == "promotion":
            raise RuntimeError("promotion failed")
        real_promote(root, manifest)
    def continuation(request):
        promote(owner, request["prepared_candidate"]["manifest"])
        events.append("complete")
        events.append("launch")
        if failure == "launch":
            raise RuntimeError("launch failed")
        from pm.candidates import finish_candidate
        finish_candidate(owner, request["prepared_candidate"]["manifest"])
        final = request["prepared_candidate"]["receipt"]
        final["state"] = "activated"
        staged._write_receipt(path, final)
        return {"candidate_activated": True, "receipt": {"update_id": request["receipt"]["update_id"]}}
    monkeypatch.setattr(update_completion, "run_completion", continuation)
    def lost(request, reason):
        final = request["prepared_candidate"]["receipt"]
        final["state"] = "incomplete"
        staged._write_receipt(path, final)
        return {"candidate_activated": False, "receipt": {"update_id": request["receipt"]["update_id"]}}
    monkeypatch.setattr(update_completion, "settle_lost_completion", lost)
    monkeypatch.setattr(staged, "_resume", lambda token: events.append("launch"))
    if failure == "before-commit":
        with pytest.raises(RuntimeError):
            staged.activate(owner, path)
    else:
        assert staged.activate(owner, path) is (failure == "none")
    final = json.loads(path.read_text(encoding="utf-8"))
    if failure == "before-commit":
        assert git(owner, "rev-parse", "HEAD") == base
        assert runtime_facts_path(owner).read_bytes() == before
        assert final["state"] == "prepared"
        assert events == ["drain", "commit", "disarm", "launch"]
    else:
        assert git(owner, "rev-parse", "HEAD") == target
        assert final["state"] == ("activated" if failure == "none" else "incomplete")
        assert "disarm" not in events
        assert events[:3] == ["drain", "commit", "promote"]
        if failure == "promotion":
            assert runtime_facts_path(owner).read_bytes() == before
        else:
            assert Facts(runtime_facts_path(owner)).get("venv")["environment"] == final["manifest"]["environment"]
    assert Path(initial["environment"]).is_dir()


def test_foreign_interpreter_refusal_has_no_filesystem_effects(candidate_owner):
    owner, _source, _base, _target, _initial = candidate_owner
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(ValueError, match="Foreign interpreter"):
        staged.admit_owner(owner)
    assert runtime_facts_path(owner).read_bytes() == before


@pytest.mark.parametrize("healthy", [True, False])
def test_preflight_uses_fresh_built_interpreter_and_disposable_state(candidate_owner, healthy):
    owner, source, _base, target, _initial = candidate_owner
    (source / "pyproject.toml").write_text(
        '[project]\nname="pinned-app"\nversion="1.0"\nrequires-python=">=3.14"\n'
        '[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n'
        '[tool.uv]\nno-index=true\n', encoding="utf-8")
    (source / "uv.lock").write_text(
        'version=1\nrevision=3\nrequires-python=">=3.14"\n'
        '[[package]]\nname="pinned-app"\nversion="1.0"\nsource={editable="."}\n', encoding="utf-8")
    body = "import os; from pathlib import Path; Path(os.environ['HERMES_HOME']).mkdir(parents=True)"
    if not healthy:
        body += "; raise RuntimeError('fresh interpreter failure')"
    (source / "backend.py").write_text(
        "from pathlib import Path\nfrom zipfile import ZipFile\n"
        "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
        "    name='pinned_app-1.0-py3-none-any.whl'\n"
        "    dist='pinned_app-1.0.dist-info'\n"
        f"    entries={{'hermes_cli/__init__.py':'', 'hermes_cli/main.py':{body!r}, 'gateway/__init__.py':'', 'gateway/run.py':''}}\n"
        "    entries[dist+'/METADATA']='Metadata-Version: 2.1\\nName: pinned-app\\nVersion: 1.0\\n'\n"
        "    entries[dist+'/WHEEL']='Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n'\n"
        "    entries[dist+'/RECORD']=''.join(path+',,\\n' for path in entries)\n"
        "    with ZipFile(Path(wheel_directory)/name,'w') as archive:\n"
        "        for path, data in entries.items(): archive.writestr(path,data)\n"
        "    return name\nbuild_editable=build_wheel\n", encoding="utf-8")
    before = runtime_facts_path(owner).read_bytes()
    manifest = staged.prepare_candidate(owner, source, target, [], {})
    if healthy:
        staged._preflight(owner, manifest)
    else:
        with pytest.raises(RuntimeError, match="fresh interpreter failure"):
            staged._preflight(owner, manifest)
    assert runtime_facts_path(owner).read_bytes() == before
    assert not list(source.parent.glob("preflight-*"))


def test_plugin_snapshots_keep_identity_currency_and_refuse_live_drift(candidate_owner, monkeypatch):
    owner, _source, _base, target, _initial = candidate_owner
    from pm.environments import dependency_home_root
    home = dependency_home_root()
    home.mkdir(parents=True, exist_ok=True)
    plugin = home / "plugins" / "attached"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: attached\nversion: 1.0.0\n", encoding="utf-8")
    (plugin / "pyproject.toml").write_text(
        '[project]\nname="attached"\nversion="1.0"\nrequires-python=">=3.14"\n'
        '[tool.uv]\npackage=false\n', encoding="utf-8")
    config = home / "config.yaml"
    config.write_text("plugins:\n  enabled: [attached]\n", encoding="utf-8")
    profile = home / "profiles" / "beta"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: fixture\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (config, profile / "config.yaml")}
    names = list((home / "profiles").iterdir())
    monkeypatch.setattr(staged, "admit_owner", lambda root: root)
    monkeypatch.setattr(staged, "_preflight", lambda *args: None)
    path = staged.prepare(owner, target)
    receipt = json.loads(path.read_text())
    assert list(receipt["manifest"]["plugins"]) == [str(plugin.resolve())]
    assert {path: path.read_bytes() for path in before} == before
    assert list((home / "profiles").iterdir()) == names
    config.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    monkeypatch.setattr(staged, "_drain", lambda: pytest.fail("Changed plugin graph must refuse before drain"))
    with pytest.raises(ValueError, match="plugin selection"):
        staged.activate(owner, path)
    config.write_bytes(before[config])
    git(owner, "merge", "--ff-only", target)
    staged.promote_candidate(owner, receipt["manifest"])
    from pm.install import venv_is_current
    assert venv_is_current(project_root=owner), "Native launch must accept the prepared graph without rebuilding"


def test_native_drain_refusal_resumes_its_recorded_token(monkeypatch):
    token, resumed = {"resume_needed": True}, []
    monkeypatch.setattr("hermes_cli.update_cmd._pause_gateways_for_update", lambda opts: token)
    monkeypatch.setattr("hermes_cli.update_cmd_posix_pause.pause_at_commit_point", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_windows.list_venv_holders", lambda: [{"pid": 42}])
    monkeypatch.setattr(staged, "_resume", lambda value: resumed.append(value))
    with pytest.raises(RuntimeError, match="runtime holders"):
        staged._drain()
    assert resumed == [token]


def test_native_deferred_resume_is_not_activation_success(monkeypatch):
    token = {"resume_needed": True}
    def deferred(value):
        value.update(resume_needed=False, resume_deferred="dependencies not ready")
    monkeypatch.setattr("hermes_cli.update_cmd_windows._resume_windows_gateways_after_update", deferred)
    with pytest.raises(RuntimeError, match="readiness"):
        staged._resume(token)


def test_update_lock_excludes_another_actual_process(candidate_owner):
    owner, _source, base, _target, _initial = candidate_owner
    import socket
    import sys
    repository = str(Path(__file__).resolve().parents[2])
    # A direct child legitimately joins its updater parent's native custody. Let
    # a short-lived launcher exit, then release its orphan through a socket gate:
    # this is a separate contender, not an authorized updater child.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(30)
        contender = (
            "import sys,socket;sys.path.insert(0,sys.argv[1]);from pathlib import Path;"
            "from hermes_cli.update_lock import UpdateLock;"
            "s=socket.create_connection(('127.0.0.1',int(sys.argv[3])),timeout=30);s.recv(1);"
            "lock=UpdateLock(install_root=Path(sys.argv[2]));ok=lock.acquire();"
            "lock.release();s.sendall(b'1' if ok else b'0');s.close()")
        launcher = ("import subprocess,sys;subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],*sys.argv[2:]],"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)")
        before = runtime_facts_path(owner).read_bytes()
        with staged._locked(owner):
            subprocess.run([sys.executable, "-I", "-c", launcher, contender, repository,
                            str(owner), str(server.getsockname()[1])], check=True, timeout=30)
            connection, _address = server.accept()
            with connection:
                connection.settimeout(30)
                connection.sendall(b"g")
                assert connection.recv(1) == b"0"
    assert git(owner, "rev-parse", "HEAD") == base
    assert runtime_facts_path(owner).read_bytes() == before


def test_real_native_commit_preserves_debt_after_source_completion_failure(prepared, monkeypatch, tmp_path):
    owner, _base, target, _initial, path = prepared
    from hermes_cli import main, update_host_obligation
    monkeypatch.setattr(main, "PROJECT_ROOT", owner)
    debt = tmp_path / "host-debt.json"
    monkeypatch.setattr(update_host_obligation, "host_obligation_path", lambda: debt)
    inventory = [{"kind": "gateway", "profile": "default", "pid": 12345, "supervisor": "manual"}]
    monkeypatch.setattr(staged, "_activation_snapshot", lambda: (None, inventory))
    monkeypatch.setattr(staged, "_drain", lambda: None)
    monkeypatch.setattr(staged, "_resume", lambda token: pytest.fail("Post-commit failure must retain restart debt"))
    def fail_completion(*args):
        raise RuntimeError("completion failure")
    monkeypatch.setattr("hermes_cli.update_completion.run_completion", fail_completion)
    assert staged.activate(owner, path) is False
    assert git(owner, "rev-parse", "HEAD") == target
    assert debt.is_file()
    assert json.loads(debt.read_text())["expected_sha"] == target
    from hermes_cli.venv_sync import completion_pending_path
    assert completion_pending_path(owner).is_file()
    assert json.loads(path.read_text())["state"] == "prepared"  # lost child leaves recovery evidence intact


def test_cli_retains_native_postcommit_zero_exit_convention(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr(staged, "activate", lambda root, path: False)
    result = staged.command(SimpleNamespace(activate="receipt.json"), tmp_path)
    assert result is None
    assert "needs follow-up" in capsys.readouterr().out


def test_cli_precommit_refusal_exits_nonzero(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    def refuse(*args):
        raise ValueError("source changed")
    monkeypatch.setattr(staged, "activate", refuse)
    with pytest.raises(SystemExit) as exc:
        staged.command(SimpleNamespace(activate="receipt.json"), tmp_path)
    assert exc.value.code == 2
    assert "source changed" in capsys.readouterr().err


def test_failed_prepare_releases_only_its_pin_and_keeps_evidence(candidate_owner, monkeypatch):
    owner, _source, base, target, initial = candidate_owner
    monkeypatch.setattr(staged, "admit_owner", lambda root: root)
    manifests = []
    real_prepare = staged.prepare_candidate
    def capture(*args):
        manifest = real_prepare(*args)
        manifests.append(manifest)
        return manifest
    monkeypatch.setattr(staged, "prepare_candidate", capture)
    def fail_preflight(*args):
        raise RuntimeError("broken candidate interpreter")
    monkeypatch.setattr(staged, "_preflight", fail_preflight)
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(RuntimeError, match="broken candidate"):
        staged.prepare(owner, target)
    manifest = manifests[0]
    assert (Path(manifest["source"]).parent / "receipt.json").is_file()
    assert not (Path(manifest["environment"]).parent / ".update-retained").exists()
    assert git(owner, "rev-parse", "HEAD") == base
    assert runtime_facts_path(owner).read_bytes() == before
    from hermes_cli.runtime_state import collect_generations
    assert collect_generations(owner, min_age_seconds=0) == [Path(manifest["environment"]).parent]
    assert Path(initial["environment"]).is_dir()


def test_persistent_candidate_receipt_io_failure_stays_native_partial(prepared, monkeypatch, tmp_path):
    owner, _base, target, _initial, path = prepared
    from hermes_cli import main, update_host_obligation, update_receipt
    monkeypatch.setattr(main, "PROJECT_ROOT", owner)
    debt = tmp_path / "host-debt.json"
    monkeypatch.setattr(update_host_obligation, "host_obligation_path", lambda: debt)
    inventory = [{"kind": "gateway", "profile": "default", "pid": 12345, "supervisor": "manual"}]
    monkeypatch.setattr(staged, "_activation_snapshot", lambda: (None, inventory))
    monkeypatch.setattr(staged, "_drain", lambda: None)
    def no_receipt(*args):
        raise OSError("candidate receipt filesystem unavailable")
    monkeypatch.setattr(staged, "_write_receipt", no_receipt)
    def fail_sidecar(request):
        import contextvars
        from hermes_cli.update_completion import _finish_candidate, _resume_receipt
        def child():
            _resume_receipt(request["receipt"])
            update_receipt.finalize_update_receipt("partial")
            terminal = update_receipt.read_latest_receipt()
            assert not _finish_candidate(request, terminal)
            return {"receipt": update_receipt.read_latest_receipt(), "candidate_activated": False}
        return contextvars.Context().run(child)
    monkeypatch.setattr("hermes_cli.update_completion.run_completion", fail_sidecar)
    before = runtime_facts_path(owner).read_bytes()
    assert staged.activate(owner, path) is False
    assert git(owner, "rev-parse", "HEAD") == target
    assert runtime_facts_path(owner).read_bytes() == before
    assert debt.is_file()
    terminal = update_receipt.read_latest_receipt()
    assert terminal["outcome"] == "partial"
    assert {row["step"] for row in terminal["followups"]} >= {"pinned_activation"}


@pytest.mark.parametrize("declaration", [None, "PINNED_CANDIDATE_PROTOCOL = 2", "PINNED_CANDIDATE_PROTOCOL = True",
    "PINNED_CANDIDATE_PROTOCOL = 1\nPINNED_CANDIDATE_PROTOCOL = int('2')",
    "PINNED_CANDIDATE_PROTOCOL = 1\nPINNED_CANDIDATE_PROTOCOL: int = 2",
    "PINNED_CANDIDATE_PROTOCOL = 1\ndel PINNED_CANDIDATE_PROTOCOL"])
def test_unsupported_target_protocol_refuses_before_build(candidate_owner, monkeypatch, declaration):
    owner, _source, base, target, _initial = candidate_owner
    monkeypatch.setattr(staged, "admit_owner", lambda root: root)
    original_snapshot = staged._snapshot
    def snapshot(*args):
        original_snapshot(*args)
        (args[2] / "hermes_cli/update_completion.py").write_text(declaration or "# absent protocol")
    monkeypatch.setattr(staged, "_snapshot", snapshot)
    monkeypatch.setattr(staged, "prepare_candidate", lambda *args: pytest.fail("Unsupported target must not build"))
    monkeypatch.setattr(staged, "_drain", lambda: pytest.fail("Unsupported target must not drain"))
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(ValueError, match="protocol"):
        staged.prepare(owner, target)
    assert runtime_facts_path(owner).read_bytes() == before
    assert git(owner, "rev-parse", "HEAD") == base


@pytest.mark.parametrize("failure", ["bootstrap", "lost-result", "spawn"])
@pytest.mark.parametrize("recovery_fails", [False, True])
def test_failed_bootstrap_resumes_drained_token_once(prepared, monkeypatch, failure, recovery_fails):
    owner, _base, target, _initial, path = prepared
    from hermes_cli import main, update_receipt
    monkeypatch.setattr(main, "PROJECT_ROOT", owner)
    monkeypatch.setattr(staged, "_activation_snapshot", lambda: (None, []))
    token, resumed = {"resume_needed": True}, []
    monkeypatch.setattr(staged, "_drain", lambda: token)
    def bootstrap(request):
        if request.get("resume_only"):
            resumed.append(request)
            assert request["prepared_candidate"]["manifest"]["target_sha"] == target
            if recovery_fails:
                raise OSError("fresh recovery could not spawn")
            return {"receipt": update_receipt.read_latest_receipt(), "windows_resume": {"resume_needed": False}}
        if failure == "spawn":
            raise OSError("completion could not spawn")
        if failure == "lost-result":
            return {"receipt": None, "exit_code": 23, "error": "completion died without a result"}
        update_receipt.finalize_update_receipt("partial")
        return {"receipt": update_receipt.read_latest_receipt(), "windows_resume": None}
    monkeypatch.setattr("hermes_cli.update_completion.run_completion", bootstrap)
    assert not staged.activate(owner, path)
    assert git(owner, "rev-parse", "HEAD") == target
    assert len(resumed) == 1
    assert token["resume_needed"] is recovery_fails
    manifest = json.loads(path.read_text())["manifest"]
    assert (Path(manifest["environment"]).parent / ".update-retained").exists()
    if recovery_fails:
        receipt = update_receipt.read_latest_receipt()
        assert any(row["step"] == "windows_resume" for row in receipt["followups"])
