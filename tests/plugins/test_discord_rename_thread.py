"""Discord adapter ``rename_thread`` error-surfacing regressions.

``rename_thread`` is a best-effort boolean API: every failure (unresolvable
thread, HTTP 403, HTTP 429 rate limit) collapses to ``False`` and is logged at
DEBUG. That makes a live ``/rename`` failure undiagnosable — the handler cannot
tell a rate limit from a permission problem, and the default log level hides the
cause entirely.

These tests pin the opt-in ``raise_on_error=True`` contract used by
``/rename``, plus the WARNING-level log the best-effort callers rely on.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform


def _adapter():
    from plugins.platforms.discord import adapter as discord_adapter

    inst = object.__new__(discord_adapter.DiscordAdapter)
    # ``name`` is a read-only property deriving from ``platform``; set the
    # backing attribute rather than the property. The code under test only
    # reads it for log formatting.
    inst.platform = Platform.DISCORD
    inst._client = MagicMock()
    return inst, discord_adapter


def _thread(name="Old Thread Title", edit=None):
    return SimpleNamespace(name=name, edit=edit or AsyncMock())


@pytest.mark.asyncio
async def test_rename_thread_raises_underlying_error_when_opted_in(monkeypatch):
    """``raise_on_error=True`` must propagate the real Discord exception."""
    inst, mod = _adapter()
    monkeypatch.setattr(mod, "DISCORD_AVAILABLE", True, raising=False)

    class RateLimited(Exception):
        status = 429
        retry_after = 486.9

    thread = _thread(edit=AsyncMock(side_effect=RateLimited("Too many requests")))
    inst._client.get_channel = MagicMock(return_value=thread)

    with pytest.raises(RateLimited):
        await inst.rename_thread("123", "Launch Plan", raise_on_error=True)


@pytest.mark.asyncio
async def test_rename_thread_default_stays_best_effort_boolean(monkeypatch):
    """Existing callers must keep the swallow-and-return-False contract."""
    inst, mod = _adapter()
    monkeypatch.setattr(mod, "DISCORD_AVAILABLE", True, raising=False)

    thread = _thread(edit=AsyncMock(side_effect=RuntimeError("boom")))
    inst._client.get_channel = MagicMock(return_value=thread)

    assert await inst.rename_thread("123", "Launch Plan") is False


@pytest.mark.asyncio
async def test_rename_thread_failure_logs_at_warning(monkeypatch, caplog):
    """A swallowed rename failure must be visible at the default log level."""
    inst, mod = _adapter()
    monkeypatch.setattr(mod, "DISCORD_AVAILABLE", True, raising=False)

    thread = _thread(edit=AsyncMock(side_effect=RuntimeError("boom")))
    inst._client.get_channel = MagicMock(return_value=thread)

    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        assert await inst.rename_thread("123", "Launch Plan") is False

    assert any(
        record.levelno >= logging.WARNING and "rename" in record.getMessage().lower()
        for record in caplog.records
    ), f"expected a WARNING rename-failure log, got {[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_rename_thread_raises_when_thread_cannot_be_resolved(monkeypatch):
    """An unresolvable thread must be distinguishable from an edit rejection."""
    inst, mod = _adapter()
    monkeypatch.setattr(mod, "DISCORD_AVAILABLE", True, raising=False)

    class NotFound(Exception):
        status = 404

    inst._client.get_channel = MagicMock(return_value=None)
    inst._client.fetch_channel = AsyncMock(side_effect=NotFound("Unknown Channel"))

    with pytest.raises(NotFound):
        await inst.rename_thread("123", "Launch Plan", raise_on_error=True)

    inst._client.get_channel = MagicMock(return_value=None)
    inst._client.fetch_channel = AsyncMock(side_effect=NotFound("Unknown Channel"))
    assert await inst.rename_thread("123", "Launch Plan") is False


@pytest.mark.asyncio
async def test_rename_thread_noop_on_matching_name_is_not_an_error(monkeypatch):
    """An already-correct name stays a success, even when raising is enabled."""
    inst, mod = _adapter()
    monkeypatch.setattr(mod, "DISCORD_AVAILABLE", True, raising=False)

    edit = AsyncMock()
    inst._client.get_channel = MagicMock(return_value=_thread(name="Launch Plan", edit=edit))

    assert await inst.rename_thread("123", "Launch Plan", raise_on_error=True) is True
    edit.assert_not_awaited()
