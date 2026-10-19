"""Owner-bound candidate preparation/promotion with real Git and PM generations."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pm.candidates import prepare_candidate, promote_candidate, validate_candidate
from pm.environments import install_state_dir, runtime_facts_path
from pm.lock import Facts
from pm.packages import Venv


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


@pytest.fixture
def candidate_owner(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "pm").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[2] / "pm/lock.json", owner / "pm/lock.json")
    (owner / "pyproject.toml").write_text(
        '[project]\nname="pinned-fixture"\nversion="1.0"\nrequires-python=">=3.14"\n'
        '[tool.uv]\npackage=false\nno-index=true\n', encoding="utf-8")
    (owner / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n'
        '[[package]]\nname = "pinned-fixture"\nversion = "1.0"\nsource = { virtual = "." }\n', encoding="utf-8")
    git(owner, "init", "-b", "main")
    git(owner, "config", "user.email", "test@example.test")
    git(owner, "config", "user.name", "Candidate test")
    git(owner, "add", ".")
    git(owner, "commit", "-m", "base")
    uv = shutil.which("uv")
    assert uv, "Real PM candidate test requires uv"
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))
    from pm import paths
    monkeypatch.setattr(paths, "repo_root", lambda: owner)
    initial = Venv(owner).apply([], plugin_dirs=[], explicit=True)
    Facts(runtime_facts_path(owner)).record_state(
        "venv", Venv(owner).expected_stamp([], plugin_dirs=[]), [], **initial)
    base = git(owner, "rev-parse", "HEAD")
    # Publish the candidate to a genuine disposable remote, then restore the owner
    # by a normal branch switch (no resets or mutation of another worktree).
    git(owner, "checkout", "-b", "published")
    (owner / "payload.py").write_text("VALUE = 'prepared target'\n", encoding="utf-8")
    git(owner, "add", ".")
    git(owner, "commit", "-m", "target")
    target = git(owner, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(owner, "clone", "--bare", str(owner), str(remote))
    git(owner, "remote", "add", "upstream", str(remote))
    git(owner, "checkout", "main")
    source = install_state_dir(owner) / "candidates" / "fixture" / "source"
    shutil.copytree(owner, source, ignore=shutil.ignore_patterns(".git"))
    (source / "payload.py").write_text("VALUE = 'prepared target'\n", encoding="utf-8")
    return owner, source, base, target, initial


def test_prepare_is_inert_and_promotion_selects_prebuilt_generation(candidate_owner, monkeypatch):
    owner, source, base, target, initial = candidate_owner
    before = runtime_facts_path(owner).read_bytes()
    manifest = prepare_candidate(owner, source, target, [], {})
    assert git(owner, "rev-parse", "HEAD") == base
    assert runtime_facts_path(owner).read_bytes() == before
    assert Path(initial["environment"]).is_dir()
    assert Path(manifest["environment"]).is_dir()
    assert str(install_state_dir(owner)) in manifest["environment"]
    # The real generated interpreter can load target bytes, from its own snapshot.
    python = Path(manifest["environment"]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    output = subprocess.run([str(python), "-I", "-c",
                             "import sys; sys.path.insert(0,sys.argv[1]); import payload; print(payload.VALUE)",
                             str(Path(manifest["resolved_lock"]).parent)],
                            capture_output=True, text=True, check=True, timeout=30)
    assert output.stdout.strip() == "prepared target"
    git(owner, "merge", "--ff-only", target)
    def no_build(*args, **kwargs):
        pytest.fail("Promotion must never rebuild the prepared generation")
    monkeypatch.setattr(Venv, "apply", no_build)
    promote_candidate(owner, manifest)
    selected = Facts(runtime_facts_path(owner), strict=True).get("venv")
    assert selected["environment"] == manifest["environment"]
    assert Path(initial["environment"]).is_dir(), "The previous generation remains available"


@pytest.mark.parametrize("tamper", ["lock", "source", "environment", "owner", "head", "path"])
def test_tamper_or_owner_change_cannot_publish(candidate_owner, tamper):
    owner, source, _base, target, initial = candidate_owner
    manifest = prepare_candidate(owner, source, target, [], {})
    before = runtime_facts_path(owner).read_bytes()
    mutations = {
        "lock": lambda: Path(manifest["resolved_lock"]).write_text("changed"),
        "source": lambda: (source / "payload.py").write_text("changed"),
        "environment": lambda: (Path(manifest["environment"]) / "injected.txt").write_text("changed"),
        "owner": lambda: manifest.update(install_key="foreign"),
        "head": lambda: git(owner, "merge", "--ff-only", target),
        "path": lambda: manifest.update(environment=str(owner / "foreign" / "venv")),
    }
    mutations[tamper]()
    with pytest.raises(ValueError):
        validate_candidate(owner, manifest)
    assert runtime_facts_path(owner).read_bytes() == before
    assert Path(initial["environment"]).is_dir()
