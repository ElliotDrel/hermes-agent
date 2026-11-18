"""Update progress and receipts are per run, survive delivery failure, and fall back to the main chat.

HERMES-FORK-016: a run's own chat (its thread) is tried first, then the profile's configured main
chat without any thread; a returned send failure counts; an undelivered receipt stays on disk; the
watcher's deadline never invents an installer result.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner

MAIN_CHAT = "900"
RUN_CHAT = "100"


def _runner(adapter, home_thread=None, *, has_adapter=True):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_configs = {}
    runner.config = SimpleNamespace(platforms={Platform.DISCORD: SimpleNamespace(
        home_channel=SimpleNamespace(chat_id=MAIN_CHAT, thread_id=home_thread))})
    runner._authorization_adapter = lambda *a: adapter if has_adapter else None
    runner._peek_session_state = lambda *a: None
    return runner


def _adapter(*results):
    return SimpleNamespace(send=AsyncMock(side_effect=list(results)))


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["refused", "exception", "missing"])
async def test_update_receipt_falls_back_to_main(tmp_path, primary):
    first = {"refused": SendResult(success=False, error="gone"), "exception": RuntimeError("gone")}
    adapter = _adapter(*([] if primary == "missing" else [first[primary]]), SendResult(success=True))
    runner = _runner(adapter, home_thread="stale-home-thread")
    chat_id = None if primary == "missing" else RUN_CHAT
    pending = _write(tmp_path, ".update_pending.json",
                     dict(platform="discord", chat_id=chat_id, thread_id=RUN_CHAT))
    _write(tmp_path, ".update_exit_code", "0")
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is True
    args = adapter.send.await_args
    assert args.args[0] == MAIN_CHAT
    assert args.kwargs["metadata"]["non_conversational"] is True
    assert not args.kwargs["metadata"].get("thread_id")  # never the old home thread
    assert not pending.exists()


@pytest.mark.asyncio
async def test_undelivered_update_receipt_survives_for_retry(tmp_path):
    runner = _runner(_adapter(*[SendResult(success=False, error="offline")] * 2))
    pending = _write(tmp_path, ".update_pending.json", dict(platform="discord", chat_id=RUN_CHAT, thread_id=RUN_CHAT))
    exit_path = _write(tmp_path, ".update_exit_code", "1")
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is False
    assert pending.exists() and exit_path.exists()


@pytest.mark.asyncio
async def test_watcher_preserves_failed_completion(tmp_path):
    runner = _runner(_adapter(*[SendResult(success=False, error="offline")] * 4))
    pending = _write(tmp_path, ".update_pending.json", dict(platform="discord", chat_id=RUN_CHAT, thread_id=RUN_CHAT))
    exit_path = _write(tmp_path, ".update_exit_code", "0")
    with patch("gateway.run._hermes_home", tmp_path), \
            patch("gateway.run_notifications_update.asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await runner._watch_update_progress()
    assert pending.exists() and exit_path.exists()


@pytest.mark.asyncio
async def test_same_parent_id_still_falls_back_out_of_thread():
    adapter = _adapter(SendResult(success=False, error="gone"), SendResult(success=True))
    runner = _runner(adapter, home_thread="old")
    delivered = await runner._deliver_update_completion(
        dict(chat_id=MAIN_CHAT, thread_id=RUN_CHAT), Platform.DISCORD, adapter, "Done")
    assert delivered and adapter.send.await_count == 2
    assert not adapter.send.await_args.kwargs["metadata"].get("thread_id")


@pytest.mark.asyncio
async def test_run_chat_equal_to_main_chat_is_tried_once():
    adapter = _adapter(SendResult(success=False, error="gone"))
    runner = _runner(adapter)
    assert not await runner._deliver_update_completion(
        dict(chat_id=MAIN_CHAT), Platform.DISCORD, adapter, "Done")
    assert adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_update_stream_falls_back_to_main(tmp_path):
    adapter = _adapter(SendResult(success=False, error="gone"), SendResult(success=True))
    runner = _runner(adapter)
    _write(tmp_path, ".update_pending.json", dict(platform="discord", chat_id=RUN_CHAT, thread_id=RUN_CHAT))
    with patch("gateway.run._hermes_home", tmp_path):
        target = runner._resolve_update_target(runner._update_paths())
        await runner._send_update_output(target, "Waiting for 2 active agents")
    assert adapter.send.await_args.args[0] == MAIN_CHAT
    assert adapter.send.await_args.kwargs["metadata"]["non_conversational"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("has_target", [True, False])
async def test_monitor_deadline_never_fabricates_install_exit(tmp_path, has_target):
    adapter = _adapter(SendResult(success=True))
    runner = _runner(adapter, has_adapter=has_target)
    pending = _write(tmp_path, ".update_pending.json", dict(platform="discord", chat_id=RUN_CHAT, thread_id=RUN_CHAT))
    with patch("gateway.run._hermes_home", tmp_path), \
            patch("hermes_cli.gateway._get_restart_exit_wait_budget", return_value=0):
        await runner._watch_update_progress(timeout=0)
    assert pending.exists()
    assert not (tmp_path / ".update_exit_code").exists()
    if has_target:
        assert "30 minutes" not in str(adapter.send.await_args_list)


@pytest.mark.asyncio
async def test_watcher_deadline_includes_restart_wait_budget(tmp_path):
    """A run that is still inside the restart wait when the base timeout passes is still followed."""
    adapter = _adapter(SendResult(success=True))
    runner = _runner(adapter)
    _write(tmp_path, ".update_pending.json", dict(platform="discord", chat_id=RUN_CHAT, thread_id=RUN_CHAT))
    exit_path = tmp_path / ".update_exit_code"

    async def finish_during_budget(_delay):
        exit_path.write_text("0")

    with patch("gateway.run._hermes_home", tmp_path), \
            patch("hermes_cli.gateway._get_restart_exit_wait_budget", return_value=600), \
            patch("gateway.run_notifications_update.asyncio.sleep", side_effect=finish_during_budget):
        await runner._watch_update_progress(timeout=0)
    assert adapter.send.await_count == 1
    assert not (tmp_path / ".update_pending.json").exists()


@pytest.mark.asyncio
async def test_one_run_never_splits_between_its_thread_and_the_main_chat():
    """After one message fell back to the main chat, the rest of that run follows it there."""
    adapter = _adapter(SendResult(success=False, error="thread gone"), SendResult(success=True),
                       SendResult(success=True), SendResult(success=True))
    runner = _runner(adapter)
    pending = dict(chat_id=RUN_CHAT, thread_id=RUN_CHAT, timestamp="2026-11-18T10:00:00")
    for text in ("progress", "more progress", "receipt"):
        assert await runner._deliver_update_completion(pending, Platform.DISCORD, adapter, text) is True
    assert [call.args[0] for call in adapter.send.await_args_list] == [RUN_CHAT, MAIN_CHAT, MAIN_CHAT, MAIN_CHAT]
    other_run = dict(pending, timestamp="2026-11-18T11:00:00")
    adapter.send.side_effect = [SendResult(success=True)]
    assert await runner._deliver_update_completion(other_run, Platform.DISCORD, adapter, "next") is True
    assert adapter.send.await_args.args[0] == RUN_CHAT, "a new run starts in its own thread again"
