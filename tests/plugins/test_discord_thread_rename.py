"""Real native adapter with synthetic aiohttp responses; no Discord traffic."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from plugins.platforms.discord import thread_rename as transport
from plugins.platforms.discord.adapter import DiscordAdapter


class Response:
    def __init__(self, status, data):
        self.status, self.data = status, data
        self.reason = "Synthetic"
        self.headers = {"content-type": "application/json"}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def text(self, **kwargs):
        return json.dumps(self.data)


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def adapter_for():
    adapter = object.__new__(DiscordAdapter)
    global_over = asyncio.Event()
    global_over.set()
    http = discord.http.HTTPClient(asyncio.get_running_loop())
    http.token = "synthetic-token"
    http._global_over = global_over
    adapter._client = SimpleNamespace(http=http)
    return adapter


@pytest.mark.asyncio
async def test_exact_confirmed_names_and_single_attempt_limits(monkeypatch):
    adapter = adapter_for()
    old = {"type":11, "name":"Old"}
    for name in ("a" * 78, "a" * 100, "😀" * 100):
        target = {"type":11, "name":name}
        session = Session([Response(200, old), Response(200, target), Response(200, target)])
        monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
        assert await adapter.rename_thread("999", name, only_if_current_name="Old", raise_on_error=True)
        assert [c[0] for c in session.calls] == ["GET", "PATCH", "GET"]
        assert session.calls[1][2]["json"] == {"name":name}
        assert adapter._client.http.max_ratelimit_timeout is None
    session = Session([Response(200, {"type":11, "name":"Already visible"})])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    assert await adapter.rename_thread("999", "Already visible", raise_on_error=True)
    assert len(session.calls) == 1
    # Known exhausted native route must stop a mutation before network I/O.
    route = discord.http.Route("PATCH", '/channels/{channel_id}', channel_id=999)
    bucket = adapter._client.http.get_ratelimit(f'{route.key}:{route.major_parameters}')
    bucket.remaining = 0
    bucket.expires = asyncio.get_running_loop().time() + 470.7
    session = Session([Response(200, old)])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    with pytest.raises(discord.RateLimited):
        await adapter.rename_thread("999", "New", raise_on_error=True)
    assert len(session.calls) == 1
    adapter._client.http._buckets.clear()
    # A rename sub-rate-limit is surfaced, not slept through; next attempt honors it.
    session = Session([Response(200, old), Response(429, {"retry_after":470.7,"global":False})])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    with pytest.raises(discord.RateLimited) as exc:
        await adapter.rename_thread("999", "New", raise_on_error=True)
    assert exc.value.retry_after == 470.7
    count = len(session.calls)
    with pytest.raises(discord.RateLimited):
        await adapter.rename_thread("999", "New", raise_on_error=True)
    assert len(session.calls) == count
    assert not adapter._thread_renames_pending
    assert adapter._client.http._global_over.is_set()
    adapter._thread_rename_cooldowns.clear()
    session = Session([Response(429, {"retry_after":470.7, "global":True})])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    with pytest.raises(discord.RateLimited):
        await adapter.rename_thread("999", "New", raise_on_error=True)
    assert not adapter._client.http._global_over.is_set()
    with pytest.raises(discord.RateLimited):
        await adapter.rename_thread("1000", "New", raise_on_error=True)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_guard_failures_and_cancelled_transport_leave_no_retry(monkeypatch):
    adapter = adapter_for()
    session = Session([Response(200, {"type":11,"name":"Human decision"})])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    assert not await adapter.rename_thread("999", "New", only_if_current_name="Old")
    assert len(session.calls) == 1
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class Waiting(Response):
        async def __aenter__(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    for mode in ("cancel", "timeout"):
        session = Session([Response(200, {"type":11,"name":"Old"}), Waiting(200, {})])
        monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
        monkeypatch.setattr(transport, "RENAME_TIMEOUT", 0.05 if mode == "timeout" else 15)
        task = asyncio.create_task(adapter.rename_thread("999", "New", raise_on_error=True))
        await asyncio.wait_for(entered.wait(), 10)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(transport.RenameUnconfirmed):
                await task
        assert cancelled.is_set()
        assert not adapter._thread_renames_pending
        assert len(session.calls) == 2
        entered.clear()
        cancelled.clear()
    for status in (400, 403):
        session = Session([Response(200, {"type":11,"name":"Old"}), Response(status, {"message":"Rejected", "code":50035})])
        monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
        with pytest.raises(discord.HTTPException) as exc:
            await adapter.rename_thread("999", "New", raise_on_error=True)
        assert exc.value.status == status
        assert len(session.calls) == 2
    session = Session([Response(200, {"type":11,"name":"Old"}), Response(503, {"message":"Uncertain"})])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    with pytest.raises(transport.RenameUnconfirmed):
        await adapter.rename_thread("999", "New", raise_on_error=True)
    session = Session([Response(200, {"type":11,"name":"Old"}), Response(200, {"name":"Different"})])
    monkeypatch.setattr(transport.aiohttp, "ClientSession", lambda **kwargs: session)
    with pytest.raises(transport.RenameUnconfirmed):
        await adapter.rename_thread("999", "New", raise_on_error=True)
