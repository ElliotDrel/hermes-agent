"""Real native adapter; the channel's own ``edit`` and ``HTTPClient.get_channel`` are faked."""
import asyncio
from types import SimpleNamespace

import discord
import pytest

from plugins.platforms.discord import thread_rename as transport
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.adapter_thread_titles import SemanticThreadRenames


class Thread:
    parent_id = 1

    def __init__(self, name="Old", result=None, error=None):
        self.name, self.result, self.error, self.edits = name, result, error, []

    async def edit(self, *, name, reason=None):
        self.edits.append((name, reason))
        if isinstance(self.error, BaseException):
            raise self.error
        if callable(self.error):
            await self.error()
        return SimpleNamespace(name=name) if self.result is None else self.result


def adapter_for(thread=None, fresh=None):
    adapter = object.__new__(DiscordAdapter)
    adapter._semantic_thread_renames = SemanticThreadRenames()
    async def get_channel(_id):
        return fresh
    http = SimpleNamespace(get_channel=get_channel)
    adapter._client = SimpleNamespace(http=http, get_channel=lambda _id: thread)
    return adapter


def http_error(status):
    return discord.HTTPException(SimpleNamespace(status=status, reason="Synthetic"), {"message": "x", "code": 1})


@pytest.mark.asyncio
async def test_exact_names_edit_through_the_channel_and_confirm_applied_name():
    for name in ("a" * 78, "a" * 100, "😀" * 100):
        thread = Thread()
        assert await adapter_for(thread).rename_thread("999", name, only_if_current_name="Old", raise_on_error=True)
        assert thread.edits == [(name, "Hermes semantic session title")]
    thread = Thread("Already visible")
    assert await adapter_for(thread).rename_thread("999", "Already visible", raise_on_error=True)
    assert not thread.edits


@pytest.mark.asyncio
async def test_guard_mismatch_and_unconfirmed_applied_name_never_claim_success():
    thread = Thread("Human decision")
    adapter = adapter_for(thread)
    assert not await adapter.rename_thread("999", "New", only_if_current_name="Old")
    assert not thread.edits
    thread = Thread(result=SimpleNamespace(name="Different"))
    with pytest.raises(transport.RenameUnconfirmed):
        await adapter_for(thread).rename_thread("999", "New", raise_on_error=True)


@pytest.mark.asyncio
async def test_rate_limit_is_retained_and_later_renames_do_not_resend():
    thread = Thread(error=discord.RateLimited(470.7))
    adapter = adapter_for(thread)
    with pytest.raises(discord.RateLimited) as exc:
        await adapter.rename_thread("999", "New", raise_on_error=True)
    assert exc.value.retry_after == 470.7
    with pytest.raises(discord.RateLimited):
        await adapter.rename_thread("999", "New", raise_on_error=True)
    assert len(thread.edits) == 1
    assert not adapter._thread_renames_pending


@pytest.mark.asyncio
async def test_rejections_are_reported_and_server_errors_are_uncertain():
    for status in (400, 403):
        with pytest.raises(discord.HTTPException) as exc:
            await adapter_for(Thread(error=http_error(status))).rename_thread("999", "New", raise_on_error=True)
        assert exc.value.status == status
    with pytest.raises(transport.RenameUnconfirmed):
        await adapter_for(Thread(error=http_error(503))).rename_thread("999", "New", raise_on_error=True)


@pytest.mark.asyncio
async def test_cancelled_and_timed_out_edits_leave_no_retry(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_forever():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    for mode in ("cancel", "timeout"):
        thread = Thread(error=wait_forever)
        adapter = adapter_for(thread)
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
        assert len(thread.edits) == 1
        entered.clear()
        cancelled.clear()


@pytest.mark.asyncio
async def test_fresh_read_goes_through_the_native_http_client_and_honors_cooldown():
    adapter = adapter_for(fresh={"type": 11, "name": "Visible"})
    assert await adapter.get_thread_name("999") == "Visible"
    adapter = adapter_for(fresh={"type": 0, "name": "channel"})
    with pytest.raises(ValueError):
        await adapter.get_thread_name("999")
    adapter._thread_rename_cooldowns = {None: asyncio.get_running_loop().time() + 60}
    with pytest.raises(discord.RateLimited):
        await adapter.get_thread_name("999")


@pytest.mark.asyncio
async def test_stalled_channel_lookup_is_bounded_and_reports_not_renamed(monkeypatch):
    """The 15s budget covers ``fetch_channel`` too: a stalled lookup frees the rename slot and is
    a plain failure (nothing was sent), never an uncertain edit."""
    monkeypatch.setattr(transport, "RENAME_TIMEOUT", 0.05)
    cancelled = asyncio.Event()

    async def stalled_fetch(_id):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter = adapter_for()
    adapter._client = SimpleNamespace(http=adapter._client.http, get_channel=lambda _id: None, fetch_channel=stalled_fetch)
    with pytest.raises(transport.RenameLookupFailed) as exc:
        await asyncio.wait_for(adapter.rename_thread("999", "New", raise_on_error=True), 10)
    assert not isinstance(exc.value, transport.RenameUnconfirmed)
    assert cancelled.is_set()
    assert not adapter._thread_renames_pending
