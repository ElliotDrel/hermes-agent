"""Rename transaction invariants: real SQLite, mocked Discord I/O, no model calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource
from hermes_state import AsyncSessionDB, SessionDB


def runner_for(db):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(session_id="session-1")
    runner._async_session_store = AsyncSessionStore(runner.session_store)
    runner._session_db = AsyncSessionDB(db)
    adapter = SimpleNamespace(send=AsyncMock(), get_thread_name=AsyncMock(return_value="Visible old"), rename_thread=AsyncMock(return_value=True))
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter


def event(text="/rename New title"):
    return MessageEvent(text=text, source=SessionSource(platform=Platform.DISCORD, user_id="user", chat_id="999", thread_id="999", chat_type="thread"))


@pytest.mark.asyncio
async def test_pending_confirmation_and_manual_decisions(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="session-1", source="discord")
        db.set_session_title("session-1", "Internal old")
        db.append_message("session-1", "user", "Existing history")
        snapshot = db.get_messages("session-1")
        runner, adapter = runner_for(db)
        entered, release = asyncio.Event(), asyncio.Event()
        async def edit(*args, **kwargs):
            entered.set()
            await release.wait()
            return True
        adapter.rename_thread.side_effect = edit
        task = asyncio.create_task(runner._handle_rename_command(event()))
        await asyncio.wait_for(entered.wait(), 10)
        assert db.get_session_title("session-1") == "Internal old"
        for text in ("/rename New title", "/rename Other title", "/rename"):
            assert "pending" in await runner._handle_rename_command(event(text))
        assert adapter.rename_thread.await_count == 1
        release.set()
        assert "confirmed" in await task
        assert db.get_session_title("session-1") == "New title"
        assert db.get_session_title_source("session-1") == "user"
        assert db.get_messages("session-1") == snapshot
        # Saved-title equality does not bypass Discord, and a newer /title survives.
        adapter.rename_thread.reset_mock()
        async def manual(*args, **kwargs):
            db.set_session_title("session-1", "New manual decision")
            return True
        adapter.rename_thread.side_effect = manual
        assert "preserved" in await runner._handle_rename_command(event())
        assert adapter.rename_thread.await_count == 1
        assert db.get_session_title("session-1") == "New manual decision"
        # Cancellation must retain thread ownership until a DB worker settles.
        import threading
        started, finish = threading.Event(), threading.Event()
        original = db.set_session_title_if_current
        def blocking(*args, **kwargs):
            started.set()
            finish.wait(10)
            return original(*args, **kwargs)
        db.set_session_title_if_current = blocking
        adapter.rename_thread.side_effect = None
        task = asyncio.create_task(runner._handle_rename_command(event()))
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert "pending" in await runner._handle_rename_command(event())
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runner._pending_rename_commands
        db.set_session_title_if_current = original
        db.set_session_title("session-1", "New manual decision")
        # Session boundary is not a title transfer or route repair.
        async def reset(*args, **kwargs):
            runner.session_store.get_or_create_session.return_value = SimpleNamespace(session_id="session-2")
            return True
        adapter.rename_thread.side_effect = reset
        assert "active session changed" in await runner._handle_rename_command(event())
        assert db.get_session_title("session-1") == "New manual decision"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_validation_failure_cancellation_and_generation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="session-1", source="discord")
        db.set_session_title("session-1", "Old")
        runner, adapter = runner_for(db)
        for invalid in ("a" * 101, "Bad\nname", "Invisible\u200bname", "Two  spaces"):
            assert "not completed" in await runner._handle_rename_command(event("/rename " + invalid))
        adapter.rename_thread.assert_not_awaited()
        assert adapter.get_thread_name.await_count == 4
        assert adapter.send.await_count == 4
        for error in (discord.RateLimited(470.7), discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"message":"No permission", "code":50013}), discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), {"message":"Invalid", "code":50035})):
            adapter.rename_thread.side_effect = error
            reply = await runner._handle_rename_command(event())
            assert "saved title was not changed" in reply
            assert "no delayed" in reply
            if isinstance(error, discord.RateLimited):
                assert "471 seconds" in reply
            assert db.get_session_title("session-1") == "Old"
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        adapter.rename_thread.side_effect = wait
        task = asyncio.create_task(runner._handle_rename_command(event()))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runner._pending_rename_commands
        assert db.get_session_title("session-1") == "Old"
        adapter.rename_thread.side_effect = None
        db.append_message("session-1", "user", "Repair thread title.")
        with patch("agent.title_generator.generate_regenerated_title", return_value="Generated title"):
            assert "confirmed" in await runner._handle_rename_command(event("/rename"))
        assert db.get_session_title("session-1") == "Generated title"
        literal = "Research — alpha–beta"
        assert "confirmed" in await runner._handle_rename_command(event("/rename " + literal))
        assert db.get_session_title("session-1") == literal
        db.set_session_title("session-1", "Generated title")
        # Atomic title/provenance CAS protects a same-text provenance change too.
        db.set_session_title_source("session-1", "llm")
        assert not db.set_session_title_if_current("session-1", "Stale", expected_title="Generated title", expected_source="user")
        assert db.get_session_title("session-1") == "Generated title"
        db.end_session("session-1", end_reason="reset")
        assert not db.set_session_title_if_current("session-1", "Ended write", expected_title="Generated title", expected_source="llm")
    finally:
        db.close()
