"""Deterministic combined ownership regressions; no transport/model traffic."""
import asyncio
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.run_turn_runner import TurnRunner
from tests.gateway.test_composed_progress_lifecycle import composed_config, install_configured_runner


@pytest.mark.asyncio
async def test_real_turn_waits_for_cancel_overflow_and_status_before_snapshot(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    entered = asyncio.Event()
    contexts = []

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []
        def run_conversation(self, message, conversation_history=None, **kwargs):
            return {"final_response": "done", "messages": [], "completed": True}

    runner, adapter = install_configured_runner(monkeypatch, tmp_path, composed_config(), Agent)
    runner._draining = False


    async def delayed_progress(turn):
        contexts.append(turn._ctx)
        pending_status = concurrent.futures.Future()
        turn._ctx._cleanup_status_futures = [pending_status]
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Real cancel-time progress send appends its physical ID after an await.
            await turn._send_progress_text(SimpleNamespace(adapter=adapter), "cancel overflow")
            async def finish_status():
                result = await adapter.send("thread", "pending status")
                turn._track_progress_result(result)
                pending_status.set_result(result)
            asyncio.create_task(finish_status())
            raise

    monkeypatch.setattr(TurnRunner, "send_progress_messages", delayed_progress)
    original_worker = GatewayRunner._run_agent_await_turn_worker
    async def wait_worker(self, *args):
        await entered.wait()
        return await original_worker(self, *args)
    monkeypatch.setattr(GatewayRunner, "_run_agent_await_turn_worker", wait_worker)
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread")
    adapter._active_sessions["key"] = asyncio.Event()
    adapter._active_sessions["key"]._hermes_run_generation = 1
    adapter._pending_messages["key"] = MessageEvent(text="next", source=source, message_id="next")
    runner._session_key_for_source = lambda source: "key"
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="next")
    runner._refresh_agent_cache_message_count = AsyncMock()
    async def handler(event):
        result = await runner._run_agent(message=event.text, context_prompt="stable", history=[], source=source,
                                        session_id="session", session_key="key", run_generation=1)
        return None if result.get("already_sent") else result["final_response"]
    adapter._message_handler = handler
    await adapter._process_message_background(MessageEvent(text="first", source=source), "key")
    overflow = [s["message_id"] for s in adapter.sent if s["content"] == "cancel overflow"]
    deleted = [d["message_id"] for d in adapter.deleted]
    assert len(contexts) == 2 and len(overflow) == 2
    assert all(mid in deleted for mid in overflow)
    status_ids = [s["message_id"] for s in adapter.sent if s["content"] == "pending status"]
    assert len(status_ids) == 2 and all(mid in deleted for mid in status_ids)
    assert all(ctx._cleanup_msg_ids == [] for ctx in contexts)
    # A successful sibling must not replay when chained cleanup remains unconfirmed.
    calls = []
    owner = object()
    failed = AsyncMock(return_value=False)
    adapter.register_post_delivery_callback("key", lambda: calls.append("released"), generation=1, owner=owner)
    adapter.register_post_delivery_callback("key", failed, generation=1, owner=owner)
    await adapter._fire_post_delivery_callback("key", 1, owner)
    await adapter._fire_post_delivery_callback("key", 1, owner)
    assert calls == ["released"] and failed.await_count == 2


@pytest.mark.asyncio
async def test_real_discord_busy_inline_and_debounce_receipts(monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_discord_free_response import FakeTextChannel, make_message
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="placeholder"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._text_batch_delay_seconds = 0
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 60
    adapter._busy_text_hard_cap_seconds = 120
    adapter._message_handler = AsyncMock(return_value=None)
    adapter._heal_stale_session_lock = lambda key: None
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="42")
    key = adapter._source_session_key(source)
    adapter._active_sessions[key] = asyncio.Event()
    channel = FakeTextChannel(channel_id=123)
    assert await adapter._handle_message(make_message(channel=channel, content="/status"), recovered=True) is True
    assert await adapter._handle_message(make_message(channel=channel, content="buffered"), recovered=True) is True
    assert adapter._text_debounce_store()[key].event.text == "buffered"
    adapter._discard_text_debounce(key)
    async def reject(event):
        event._gateway_rejected = True
        return None
    adapter._message_handler = reject
    assert await adapter._handle_message(make_message(channel=channel, content="/status"), recovered=True) is False
