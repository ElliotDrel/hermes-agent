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


@pytest.mark.asyncio
async def test_restart_preflight_keeps_loop_responsive_and_refusal_unchanged(tmp_path, monkeypatch):
    import threading
    obj = runner()
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()

    def validate(root):
        assert threading.get_ident() != loop_thread
        entered.set()
        if not release.wait(2):
            raise AssertionError("validation blocked the event loop")
        raise ValueError("changed candidate")

    monkeypatch.setattr("hermes_cli.update_stage.restart_candidate", validate)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    event = MessageEvent(text="/restart", source=SessionSource(
        platform=Platform.DISCORD, user_id="owner", chat_id="thread", chat_type="group"))
    task = asyncio.create_task(obj._handle_restart_command(event))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        release.set()
        reply = await asyncio.wait_for(task, 3)
        assert "refused" in reply and "changed candidate" in reply
        obj.request_restart.assert_not_called()
        assert not (tmp_path / ".restart_notify.json").exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_real_windows_pipe_remains_live_during_owner_preflight(tmp_path, monkeypatch):
    import os
    from gateway.control_socket import GatewayControlServer, identify_gateway
    from hermes_cli.gateway_supervised_restart import gateway_declares_external_supervisor

    # No production home, credentials, gateway lifecycle or watchdog invocation.
    server = GatewayControlServer(tmp_path, verb_handlers={
        "identify": lambda: {"pid": os.getpid(), "supervisor": "manual"}})
    assert await server.start()
    try:
        monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda *a, **k:
            pytest.fail("current-process owner detection must not query its own pipe"))
        monkeypatch.setattr("gateway.control_socket._detect_supervisor", lambda: "manual")
        assert not gateway_declares_external_supervisor(os.getpid(), tmp_path)
        monkeypatch.setattr("gateway.control_socket._detect_supervisor", lambda: "external")
        assert gateway_declares_external_supervisor(os.getpid(), tmp_path)
        # Real named-pipe client/server round trip, with the loop free to answer.
        result = await asyncio.wait_for(asyncio.to_thread(identify_gateway, tmp_path), 5)
        assert result == {"pid": os.getpid(), "supervisor": "manual"}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_restart_rechecks_admission_after_yielded_preflight(tmp_path, monkeypatch):
    import threading
    obj = runner()
    entered = threading.Event()
    release = threading.Event()

    def validate(root):
        entered.set()
        assert release.wait(2)
        return {"token": "a" * 32}

    monkeypatch.setattr("hermes_cli.update_stage.restart_candidate", validate)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    event = MessageEvent(text="/restart", source=SessionSource(
        platform=Platform.DISCORD, user_id="owner", chat_id="thread", chat_type="group"))
    task = asyncio.create_task(obj._handle_restart_command(event))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        obj._restart_requested = True
        release.set()
        await asyncio.wait_for(task, 3)
        obj.request_restart.assert_not_called()
        assert not hasattr(obj, "_staged_restart")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
