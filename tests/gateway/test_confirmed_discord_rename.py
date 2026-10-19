"""Confirmed explicit names and conversation regeneration use the native title lane."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, GatewayConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state import SessionDB, AsyncSessionDB


@pytest.mark.asyncio
async def test_rename_ack_precedes_generation_and_newer_human_title_wins(tmp_path, monkeypatch):
    from agent import title_generator
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session", "discord", user_id="person", chat_id="123", chat_type="thread", thread_id="123")
    db.set_session_title("session", "Original title")
    db.append_message("session", "user", "Investigate connection pool exhaustion")
    db.append_message("session", "assistant", "The pool leaks connections after exceptions")
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123", chat_type="thread", user_id="person")
    entry = SimpleNamespace(session_id="session", session_key="discord:thread:123")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store, get_or_create_session=AsyncMock(return_value=entry))
    runner._session_key_for_source = lambda source: entry.session_key
    runner._thread_metadata_for_source = lambda source: {"thread_id": source.thread_id}
    runner._session_model_override = lambda key: {}
    runner._resolve_session_agent_runtime = lambda **kw: ("session-model", {"provider": "custom", "base_url": "http://127.0.0.1:1/v1"})
    trace = []
    async def acknowledge(*args, **kwargs):
        trace.append("ack")
        return SendResult(success=True, message_id="ack")
    async def confirmed(*args, **kwargs):
        trace.append("confirmed")
        return {"status": "confirmed", "name": kwargs["name"]}
    adapter = SimpleNamespace(read_thread_name=AsyncMock(return_value="Remote title"),
        send=AsyncMock(side_effect=acknowledge), rename_thread_confirmed=AsyncMock(side_effect=confirmed))
    runner._delivery_adapter_for = lambda source: adapter
    def auxiliary(**kwargs):
        assert trace[-1] == "ack"
        assert kwargs["task"] == "title_generation"
        assert kwargs["main_runtime"]["model"] == "session-model"
        assert "connection pool" in kwargs["messages"][-1]["content"]
        trace.append("generation")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"title":"Diagnose connection pool exhaustion"}'))])
    monkeypatch.setattr(title_generator, "call_llm", auxiliary)
    try:
        result = await runner._handle_rename_command(MessageEvent(text="/rename", source=source))
        assert trace == ["ack", "generation", "confirmed"]
        assert db.get_session_title("session") == "Diagnose connection pool exhaustion"
        assert "Diagnose connection pool exhaustion" in result
        trace.clear()
        async def racing(*args, **kwargs):
            db.set_session_title("session", "New human title")
            return {"status": "confirmed", "name": kwargs["name"]}
        adapter.rename_thread_confirmed.side_effect = racing
        result = await runner._handle_rename_command(MessageEvent(text="/rename Another name", source=source))
        assert db.get_session_title("session") == "New human title"
        assert "saved" in result.lower() and "kept" in result.lower()
        # A newer human choice is still newer when it returns to identical text (ABA).
        async def aba(*args, **kwargs):
            old = db.get_session_title("session")
            db.set_session_title("session", "Temporary human name")
            db.set_session_title("session", old)
            return {"status": "confirmed", "name": kwargs["name"]}
        adapter.rename_thread_confirmed.side_effect = aba
        result = await runner._handle_rename_command(MessageEvent(text="/rename Candidate", source=source))
        assert db.get_session_title("session") == "New human title"
        assert "saved" in result.lower() and "kept" in result.lower()
        from hermes_cli.commands import resolve_command, should_bypass_active_session
        definition = resolve_command("rename")
        assert should_bypass_active_session("rename")
        assert runner._busy_slash_route(definition) == ("plain", runner._handle_rename_command)
        # Refused acknowledgement stops even invalid-name validation and generation/transport.
        adapter.send.return_value = SendResult(success=False, error="refused")
        adapter.send.side_effect = None
        adapter.rename_thread_confirmed.reset_mock()
        assert "acknowledge" in (await runner._handle_rename_command(MessageEvent(text="/rename " + "x"*101, source=source))).lower()
        adapter.rename_thread_confirmed.assert_not_awaited()
        # Repeated invocations share one pending owner until its acknowledged work settles.
        adapter.send.side_effect = acknowledge
        adapter.rename_thread_confirmed.side_effect = confirmed
        for exact in ["x", "😀" * 100]:
            assert exact in await runner._handle_rename_command(MessageEvent(text="/rename " + exact, source=source))
            assert db.get_session_title("session") == exact
        before = db.get_session_title("session")
        assert "1–100" in await runner._handle_rename_command(MessageEvent(text="/rename " + "x"*101, source=source))
        assert db.get_session_title("session") == before
        import asyncio
        started, release = asyncio.Event(), asyncio.Event()
        original_set = runner._session_db.set_session_title_if_current
        async def settling(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_set(*args, **kwargs)
        runner._session_db.set_session_title_if_current = settling
        task = asyncio.create_task(runner._handle_rename_command(MessageEvent(text="/rename Settled name",source=source)))
        await asyncio.wait_for(started.wait(), 5)
        assert "pending" in await runner._handle_rename_command(MessageEvent(text="/rename Duplicate",source=source))
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.get_session_title("session") == "Settled name"
        assert not runner._pending_thread_renames
        # Native reconciliation upgrades a legacy database lacking the revision column.
        db.close()
        import sqlite3
        with sqlite3.connect(tmp_path / "state.db") as connection:
            connection.execute("DROP TRIGGER sessions_title_revision")
            connection.execute("ALTER TABLE sessions DROP COLUMN title_revision")
        db = SessionDB(tmp_path / "state.db")
        legacy = db.get_session("session")
        assert legacy["title_revision"] == 0
        db.set_session_title_source("session", "user")
        assert db.get_session("session")["title_revision"] > legacy["title_revision"]
        assert not db.set_session_title_if_current("session", "Late candidate", expected_title=legacy["title"],
            expected_source=legacy["title_source"], expected_revision=legacy["title_revision"])
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_confirmed_transport_uses_fresh_reads_and_reports_uncertainty(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter
    import aiohttp
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="isolated-test-token"))
    trace, responses = [], []
    class Response:
        headers = {}
        def __init__(self, status, body):
            self.status, self.body = status, body
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self):
            return self.body
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bot isolated-test-token"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            trace.append((method, kwargs.get("json")))
            return Response(*responses.pop(0))
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    for name in ["x", "😀" * 100]:
        trace.clear()
        responses[:] = [(200,{"type":11,"name":"Original"}), (200,{"name":name}), (200,{"type":11,"name":name})]
        result = await adapter.rename_thread_confirmed("123", name=name, expected_name="Original")
        assert result["status"] == "confirmed"
        assert [step[0] for step in trace] == ["GET","PATCH","GET"]
        assert trace[1][1] == {"name":name}
    trace.clear()
    responses[:] = [(200,{"type":11,"name":"Human renamed"})]
    assert (await adapter.rename_thread_confirmed("123",name="Candidate",expected_name="Original"))["status"] == "conflict"
    assert [step[0] for step in trace] == ["GET"]
    responses[:] = [(200,{"type":11,"name":"Original"}), (503,{})]
    assert (await adapter.rename_thread_confirmed("123",name="Candidate",expected_name="Original"))["status"] == "uncertain"
    responses[:] = [(200,{"type":11,"name":"Original"}), (429,{"retry_after":45})]
    assert (await adapter.rename_thread_confirmed("123",name="Candidate",expected_name="Original"))["status"] == "rate_limited"

