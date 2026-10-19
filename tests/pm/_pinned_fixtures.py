"""Real disposable Git owners and PM toolchain fixtures for pinned-update tests."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

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
    (owner / "hermes_cli").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[2] / "hermes_cli/update_completion.py",
                 owner / "hermes_cli/update_completion.py")
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
