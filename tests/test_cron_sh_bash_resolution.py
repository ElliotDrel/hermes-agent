"""Regression: cron .sh scripts must not be launched with the WSL bash launcher.

HERMES-LOCAL-011.

Windows ships ``C:\\Windows\\System32\\bash.exe`` (the WSL launcher) whenever WSL
is installed, and it precedes Git for Windows on PATH. ``shutil.which("bash")``
therefore returns a *Linux* bash, which cannot interpret the native Windows path
that cron passes as ``argv[1]``. The MSYS/WSL argv layer strips the backslashes
and the fire dies with exit 127 and the misleading message::

    /bin/bash: C:Users2supeHermes-Workspacescriptsgbrain-migration-status.sh:
    No such file or directory

The script exists; only the interpreter is wrong. ``_run_cron_script`` must
resolve bash via ``tools.environments.local._find_bash``, which prefers Hermes'
portable Git and the known Git-for-Windows locations before any PATH lookup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cron import scheduler  # noqa: E402


WSL_LAUNCHER = r"C:\WINDOWS\system32\bash.EXE"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


class _CapturedPopen:
    """Minimal Popen stand-in that records argv and exits immediately."""

    captured_argv: list[str] | None = None

    def __init__(self, argv, **kwargs):
        type(self).captured_argv = list(argv)
        self.args = argv
        self.returncode = 0
        self.pid = 4242
        self.stdout = None
        self.stderr = None

    def poll(self):
        return 0

    def communicate(self, timeout=None):
        return ("", "")

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None

    def terminate(self):
        return None


@pytest.fixture
def cron_script(tmp_path, monkeypatch):
    """Point the scheduler's scripts dir at a tmp dir holding one .sh script."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "probe.sh"
    script.write_text("#!/usr/bin/env bash\necho ok\n", newline="\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return script


def _resolve_bash_via_scheduler(monkeypatch, which_result, find_bash_result):
    """Run the scheduler's interpreter-selection branch and return argv[0]."""
    monkeypatch.setattr(scheduler.shutil, "which", lambda name: which_result)

    import tools.environments.local as local_env

    monkeypatch.setattr(local_env, "_find_bash", lambda: find_bash_result)

    _CapturedPopen.captured_argv = None
    monkeypatch.setattr(scheduler.subprocess, "Popen", _CapturedPopen)
    return _CapturedPopen


@pytest.mark.skipif(sys.platform != "win32", reason="WSL/Git Bash collision is Windows-only")
def test_cron_sh_does_not_use_wsl_bash_launcher(cron_script, monkeypatch):
    """PATH returning the WSL launcher must not win over Git for Windows."""
    if not os.path.isfile(GIT_BASH):
        pytest.skip("Git for Windows not installed on this host")

    popen = _resolve_bash_via_scheduler(
        monkeypatch, which_result=WSL_LAUNCHER, find_bash_result=GIT_BASH
    )

    scheduler._run_job_script(str(cron_script), workdir=str(cron_script.parent))

    argv = popen.captured_argv
    assert argv is not None, "cron never launched the script"
    chosen = argv[0]
    parent = os.path.basename(os.path.dirname(chosen)).lower()
    assert parent not in {"system32", "sysnative"}, (
        f"cron selected the WSL bash launcher ({chosen!r}); native-path .sh "
        "scripts fail with exit 127 under it"
    )
    assert chosen == GIT_BASH


@pytest.mark.skipif(sys.platform != "win32", reason="WSL/Git Bash collision is Windows-only")
def test_cron_sh_falls_back_to_path_when_resolver_yields_nothing(cron_script, monkeypatch):
    """With no Git Bash available the historical PATH lookup still applies."""
    popen = _resolve_bash_via_scheduler(
        monkeypatch, which_result=GIT_BASH, find_bash_result=None
    )

    scheduler._run_job_script(str(cron_script), workdir=str(cron_script.parent))

    argv = popen.captured_argv
    assert argv is not None
    assert argv[0] == GIT_BASH
