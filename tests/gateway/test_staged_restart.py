"""Explicit restart uses the existing drain/watcher with a pinned candidate command."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.session import SessionSource
from gateway.config import Platform


def runner():
    obj = GatewayRunner.__new__(GatewayRunner)
    obj._restart_requested = False
    obj._draining = False
    obj._is_stale_restart_redelivery = lambda event: False
    obj._running_agent_count = lambda: 0
    obj.request_restart = MagicMock()
    return obj


@pytest.mark.asyncio
async def test_only_explicit_user_restart_binds_candidate_and_enters_native_drain(tmp_path, monkeypatch):
    obj = runner()
    data = {"token": "a" * 32, "root": str(tmp_path / "installed"),
            "candidate": str(tmp_path / "candidate"), "sha": "b" * 40}
    monkeypatch.setattr("hermes_cli.update_stage.restart_candidate", lambda root: data)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda: False)
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: False)
    event = MessageEvent(text="/restart", source=SessionSource(platform=Platform.DISCORD,
                         user_id="owner", chat_id="thread", chat_type="group"))
    event.internal = True
    reply = await obj._handle_restart_command(event)
    assert "explicit" in reply
    obj.request_restart.assert_not_called()
    event.internal = False
    await obj._handle_restart_command(event)
    assert obj._staged_restart is data
    obj.request_restart.assert_called_once_with(detached=True, via_service=False)
    assert (tmp_path / ".restart_notify.json").exists()


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_native_windows_watcher_launches_candidate_not_old_imports(tmp_path, monkeypatch):
    obj = runner()
    candidate = tmp_path / "candidate"
    obj._staged_restart = {"candidate": str(candidate), "root": str(tmp_path / "installed"), "token": "a" * 32}
    obj._detached_restart_helper_started = False
    obj._restart_drain_timeout = 0
    monkeypatch.setattr("gateway.run._resolve_hermes_bin", lambda: ["old-hermes.exe"])
    monkeypatch.setattr(GatewayShutdownMixin, "_restart_watcher_env", lambda: {"PYTHONPATH": "old-tree"})
    spawn = MagicMock()
    monkeypatch.setattr("subprocess.Popen", spawn)
    await obj._launch_detached_restart_command()
    argv = spawn.call_args.args[0]
    command = argv[5:]
    assert command == [str(candidate / "venv" / "Scripts" / "python.exe"), "-m",
        "hermes_cli.update_stage", "--root", str(tmp_path / "installed"), "--activate-stage", "a" * 32]
    assert spawn.call_args.kwargs["env"]["PYTHONPATH"] == str(candidate)
    assert spawn.call_args.kwargs["env"]["VIRTUAL_ENV"] == str(candidate / "venv")
    assert Path(spawn.call_args.kwargs["cwd"]) != Path(obj._staged_restart["root"])
