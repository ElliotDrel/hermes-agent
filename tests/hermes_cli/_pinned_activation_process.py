"""Whole native activation process fixture; only products/services are bounded stand-ins."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

from hermes_cli import update_completion


def assert_whole_activation(transition, candidate_owner, monkeypatch, mode, parent_origin):
    """Real owner/remote, PM generation and publication across the whole activation call."""
    from hermes_cli import staged_update as staged
    from pm.environments import runtime_facts_path
    from pm.lock import Facts
    from tests.pm._pinned_fixtures import git as owner_git

    target_fixture, _git, _old, _new, _request = transition
    owner, _source, _base, _pin, initial = candidate_owner
    repository = Path(update_completion.__file__).parents[1]
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
    # Ship the real PM/runtime/transport source closure. Only application product
    # building and gateway services use the bounded fixture's implementations.
    for package in ("pm", "hermes_platform", "hermes_cli"):
        shutil.copytree(repository / package, owner / package, dirs_exist_ok=True, ignore=ignored)
    shutil.copytree(target_fixture / "hermes_cli", owner / "hermes_cli", dirs_exist_ok=True, ignore=ignored)
    for module in ("staged_update.py", "update_completion.py", "runtime_state.py", "_early_recovery.py"):
        shutil.copy2(repository / "hermes_cli" / module, owner / "hermes_cli" / module)
    for module in ("hermes_constants.py", "hermes_dir_acl.py"):
        shutil.copy2(repository / module, owner / module)
    with (owner / "hermes_cli/venv_sync.py").open("a") as stream:
        stream.write("def completion_pending_path(root):\n    return Path(os.environ['HERMES_HOME'])/'completion-pending'\n")
    (owner / "hermes_cli/update_cmd_fleet.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def _pending_fleet_restart_needed(): return (Path(os.environ['HERMES_HOME'])/'host-debt.json').exists()\n")
    with (owner / "hermes_cli/update_cmd.py").open("a") as stream:
        stream.write(
            "\n_verify_native_fixture = _verify_fleet_after_update\n"
            "def _verify_fleet_after_update(out, **kw):\n"
            "    from hermes_cli import update_host_obligation as host\n"
            "    import os\n    from pathlib import Path\n"
            "    host.host_obligation_path=lambda:Path(os.environ['HERMES_HOME'])/'host-debt.json'\n"
            "    host.release_host_obligation('fixture-activation')\n"
            "    _verify_native_fixture(out, **kw)\n")
    # Any accidental rebuild in the fresh bootstrap is a real regression.
    with (owner / "pm/__init__.py").open("a") as stream:
        stream.write("\ndef sync_venv(*args, **kwargs): raise AssertionError('prepared candidate rebuilt')\n")
        if mode == "promotion-failure":
            stream.write("def promote_candidate(*args, **kwargs): raise OSError('injected native promotion refusal')\n")
    owner_git(owner, "add", ".")
    (owner / ".gitignore").write_text("events.jsonl\n__pycache__/\n*.pyc\n")
    owner_git(owner, "add", ".gitignore")
    owner_git(owner, "commit", "-m", "complete old native source fixture")
    base = owner_git(owner, "rev-parse", "HEAD")
    owner_git(owner, "checkout", "-b", "activation-target")
    (owner / "payload.py").write_text("VALUE = 'whole activation target'\n")
    owner_git(owner, "add", ".")
    owner_git(owner, "commit", "-m", "published complete target")
    target = owner_git(owner, "rev-parse", "HEAD")
    remote = owner.parent / "remote.git"
    owner_git(remote, "fetch", str(owner), "activation-target:activation-target")
    owner_git(owner, "checkout", "main")
    monkeypatch.setattr(staged, "admit_owner", lambda root: (staged._clean(root), root)[1])
    monkeypatch.setattr(staged, "_preflight", lambda *args: None)
    path = staged.prepare(owner, target)
    before = runtime_facts_path(owner).read_bytes()
    driver = (
        "import json,os,shutil,subprocess,sys\nfrom pathlib import Path\n"
        "repository,owner,receipt=map(Path,sys.argv[1:4])\n"
        "sys.path.insert(0,sys.argv[5])\n"
        "sys.path.insert(0,str(repository))\n"
        "from hermes_cli import staged_update as stage,update_cmd_commit as commit,update_receipt as reporting\n"
        "import hermes_cli,pm,hermes_platform\n"
        "hermes_cli.__path__=[str(owner/'hermes_cli')];pm.__path__=[str(owner/'pm')];hermes_platform.__path__=[str(owner/'hermes_platform')]\n"
        "sys.path.insert(0,str(owner))\n"
        "assert hermes_cli.__path__==[str(owner/'hermes_cli')]\n"
        "git=shutil.which('git');stage._git_command=lambda:[git]\n"
        "stage.admit_owner=lambda root:(stage._selection(root),stage._clean(root),root)[2]\n"
        "stage._preflight=lambda *args:None\n"
        "stage._activation_snapshot=lambda:(None,{'runtimes':[]})\n"
        "stage._drain=lambda:{'resume_needed':True}\n"
        "from hermes_cli import update_host_obligation as host,update_cmd_posix_pause as pause\n"
        "home=Path(os.environ['HERMES_HOME']);host.host_obligation_path=lambda:home/'host-debt.json'\n"
        "pause.pause_at_commit_point=lambda:None\n"
        "def obligations(root,sha,**kwargs):\n"
        "    from hermes_cli.venv_sync import arm_completion\n"
        "    arm_completion(root)\n"
        "    assert host.arm_host_obligation(home/'fleet-pending',expected_sha=sha,runtimes=[],owner='fixture-activation')\n"
        "commit.arm_commit_obligations=obligations\n"
        "from hermes_cli import update_completion as transport\n"
        "original=transport.run_completion\n"
        "calls=[]\n"
        "def run(request):\n"
        "    calls.append(bool(request.get('resume_only')))\n"
        "    response=original(request)\n"
        "    if sys.argv[4]=='lost-result' and not request.get('resume_only'):\n"
        "        return {'receipt':None,'exit_code':23,'error':'final response lost after verified child'}\n"
        "    return response\n"
        "transport.run_completion=run\n"
        "base=stage._git(owner,'rev-parse','HEAD')\n"
        "real_git=stage._git\n"
        "advanced=False\n"
        "def tracked_git(root,*args):\n"
        "    global advanced\n"
        "    result=real_git(root,*args)\n"
        "    if args and args[0]=='merge':advanced=True\n"
        "    return result\n"
        "stage._git=tracked_git\n"
        "prefix=os.path.normcase(str(owner.resolve()))+os.sep\n"
        "violations=[]\n"
        "def audit(event,args):\n"
        "    if advanced and event=='exec' and os.path.normcase(os.path.abspath(args[0].co_filename)).startswith(prefix):\n"
        "        violations.append(args[0].co_filename)\n"
        "        (owner.parent/'old-parent-target-exec.json').write_text(json.dumps(violations))\n"
        "        raise AssertionError('target app exec in old activation parent: '+args[0].co_filename)\n"
        "sys.addaudithook(audit)\n"
        "activated=stage.activate(owner,receipt)\n"
        "assert not violations,violations\n"
        "print('RESULT='+json.dumps({'activated':activated,'calls':calls,'pid':os.getpid()}))\n"
    )
    executable = sys.executable
    if parent_origin == "selected-generation":
        executable = str(Path(initial["environment"]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    response = subprocess.run([executable, "-c", driver, str(repository), str(owner), str(path), mode, sysconfig.get_path("purelib")],
                              cwd=owner, env={**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"]},
                              capture_output=True, text=True, timeout=120, check=False)
    assert response.returncode == 0, response.stdout + response.stderr
    assert not (owner.parent / "old-parent-target-exec.json").exists(), response.stdout + response.stderr
    result = json.loads(response.stdout.split("RESULT=")[1])
    assert owner_git(owner, "rev-parse", "HEAD") == target
    final = json.loads(path.read_text())
    selected = Facts(runtime_facts_path(owner)).get("venv")
    if mode == "promotion-failure":
        assert Path(selected["environment"]) == Path(initial["environment"]), response.stdout + response.stderr
        assert not result["activated"]
        assert final["state"] == "incomplete"
        assert (Path(final["manifest"]["environment"]).parent / ".update-retained").exists()
        home = Path(os.environ["HERMES_HOME"])
        assert json.loads((home / "host-debt.json").read_text())["expected_sha"] == target
        assert (home / "completion-pending").exists()
        return
    assert selected["environment"] == final["manifest"]["environment"], response.stdout + response.stderr
    assert runtime_facts_path(owner).read_bytes() != before
    assert Path(initial["environment"]).is_dir()
    events = [json.loads(row) for row in (owner / "events.jsonl").read_text().splitlines()]
    assert all(row["pid"] != result["pid"] for row in events)
    assert "build" in [row["name"] for row in events], response.stdout + response.stderr
    assert result["calls"] == ([False, True] if mode == "lost-result" else [False])
    assert final["state"] == "activated", response.stdout + response.stderr
    assert not (Path(final["manifest"]["environment"]).parent / ".update-retained").exists()
    if mode == "normal":
        assert result["activated"], response.stdout + response.stderr
    assert base != target
