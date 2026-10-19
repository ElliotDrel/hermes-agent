"""Surfaced Discord 429s retain parent deadlines without cross-route policy mutation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import discord
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_sync_policy_and_parent_cooldowns_are_isolated():
    adapter=DiscordAdapter(PlatformConfig(enabled=True,token="test-token"))
    http=SimpleNamespace(max_ratelimit_timeout=None)
    adapter._client=SimpleNamespace(http=http,application_id=99)
    observed=[]
    async def sync():
        observed.append(http.max_ratelimit_timeout)
        return dict.fromkeys(["total","unchanged","updated","recreated","created","deleted"],0)
    adapter._get_discord_command_sync_policy=lambda:"safe"
    adapter._desired_command_sync_fingerprint=lambda:"current"
    adapter._command_sync_skip_reason=lambda *args:None
    adapter._record_command_sync_attempt=lambda *args:None
    adapter._record_command_sync_success=lambda *args:None
    adapter._safe_sync_slash_commands=sync
    await adapter._run_post_connect_initialization()
    assert observed == [None] and http.max_ratelimit_timeout is None
    started,release=asyncio.Event(),asyncio.Event()
    async def limited(**kwargs):
        started.set()
        await release.wait()
        raise discord.RateLimited(150)
    parent=SimpleNamespace(id=123,send=AsyncMock())
    message=SimpleNamespace(content="Work",channel=parent,author=SimpleNamespace(id=5,display_name="Example"),
                            create_thread=AsyncMock(side_effect=limited))
    first=asyncio.create_task(adapter._auto_create_thread(message))
    await asyncio.wait_for(started.wait(),5)
    second=asyncio.create_task(adapter._auto_create_thread(message))
    await asyncio.sleep(0)
    assert message.create_thread.await_count == 1
    release.set()
    errors=await asyncio.gather(first,second,return_exceptions=True)
    assert all(getattr(error,"retry_after_seconds",0)>0 for error in errors)
    assert message.create_thread.await_count == 1
    parent.send.assert_not_awaited()
    thread=SimpleNamespace(id=999,add_user=AsyncMock())
    other=SimpleNamespace(content="Other",channel=SimpleNamespace(id=456),author=message.author,
                          create_thread=AsyncMock(return_value=thread))
    assert await adapter._auto_create_thread(other) is thread
    thread.add_user.assert_awaited_with(message.author)
    adapter._auto_thread_cooldowns["123"]=time.monotonic()-1
    message.create_thread=AsyncMock(return_value=thread)
    assert await adapter._auto_create_thread(message) is thread
    assert http.max_ratelimit_timeout is None


@pytest.mark.asyncio
async def test_fallback_429_removes_its_seed_and_retains_authoritative_deadline():
    adapter=DiscordAdapter(PlatformConfig(enabled=True,token="test-token"))
    refusal=discord.Forbidden(SimpleNamespace(status=403,reason="Forbidden"), "Missing permissions")
    seed=SimpleNamespace(create_thread=AsyncMock(side_effect=discord.RateLimited(65.5)),delete=AsyncMock())
    parent=SimpleNamespace(id=123,send=AsyncMock(return_value=seed))
    message=SimpleNamespace(content="Work",channel=parent,author=SimpleNamespace(id=5,display_name="Example"),
                            create_thread=AsyncMock(side_effect=refusal))
    with pytest.raises(Exception) as raised:
        await adapter._auto_create_thread(message)
    assert raised.value.retry_after_seconds >= 65.5
    seed.delete.assert_awaited_once()
    message.create_thread.assert_awaited_once()
    seed.create_thread.assert_awaited_once()
    assert adapter._auto_thread_cooldowns["123"] > time.monotonic()

    # Ambiguous surfaced failures never add a second creation at our adapter layer.
    for failure in [TimeoutError(), ConnectionError(),
                    discord.HTTPException(SimpleNamespace(status=503,reason="Unavailable"), "server error"),
                    discord.HTTPException(SimpleNamespace(status=400,reason="Bad request"), {"code":160004,"message":"thread already exists"})]:
        fresh=DiscordAdapter(PlatformConfig(enabled=True,token="test-token"))
        parent.send.reset_mock()
        message.create_thread=AsyncMock(side_effect=failure)
        thread,error=await fresh._try_auto_create_thread(message)
        assert thread is None and "uncertain" in type(error).__name__.lower()
        message.create_thread.assert_awaited_once()
        parent.send.assert_not_awaited()
        assert not fresh._auto_thread_cooldowns
    fresh=DiscordAdapter(PlatformConfig(enabled=True,token="test-token"))
    message.create_thread=AsyncMock(side_effect=refusal)
    seed.create_thread=AsyncMock(side_effect=TimeoutError())
    seed.delete.reset_mock()
    parent.send.reset_mock()
    thread,error=await fresh._try_auto_create_thread(message)
    assert thread is None and "uncertain" in type(error).__name__.lower()
    message.create_thread.assert_awaited_once()
    seed.create_thread.assert_awaited_once()
    seed.delete.assert_not_awaited()
    parent.send.assert_awaited_once()
