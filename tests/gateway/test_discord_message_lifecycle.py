"""Discord temporary-message lifecycle regressions."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _adapter(channel=None, *, cached=True):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel if cached else None,
        fetch_channel=AsyncMock(return_value=channel),
    )
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_delete_message_uses_partial_message_without_fetch(cached):
    partial = SimpleNamespace(delete=AsyncMock())
    channel = SimpleNamespace(get_partial_message=MagicMock(return_value=partial))
    adapter = _adapter(channel, cached=cached)

    assert await adapter.delete_message("555", "42") is True
    channel.get_partial_message.assert_called_once_with(42)
    partial.delete.assert_awaited_once()
    if cached:
        adapter._client.fetch_channel.assert_not_awaited()
    else:
        adapter._client.fetch_channel.assert_awaited_once_with(555)


@pytest.mark.asyncio
async def test_delete_message_failure_is_nonfatal():
    partial = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("Unknown Message")))
    channel = SimpleNamespace(get_partial_message=MagicMock(return_value=partial))
    adapter = _adapter(channel)

    assert await adapter.delete_message("555", "42") is False


@pytest.mark.asyncio
async def test_delete_message_disconnected_is_false():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = None

    assert await adapter.delete_message("555", "42") is False


@pytest.mark.asyncio
async def test_edit_rate_limit_returns_retry_metadata():
    class RateLimitedForTest(RuntimeError):
        retry_after = 2.5

    msg = SimpleNamespace(edit=AsyncMock(side_effect=RateLimitedForTest("rate limited")))
    channel = SimpleNamespace(get_partial_message=MagicMock(return_value=msg))
    adapter = _adapter(channel)
    adapter._is_discord_rate_limit = MagicMock(return_value=True)
    adapter._extract_discord_retry_after = MagicMock(return_value=2.5)

    result = await adapter.edit_message("555", "42", "progress")

    assert result.success is False
    assert result.retryable is True
    assert result.retry_after == 2.5
    assert result.error_kind == "rate_limited"


@pytest.mark.asyncio
async def test_edit_network_failure_is_retryable():
    msg = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("temporary network failure")))
    channel = SimpleNamespace(get_partial_message=MagicMock(return_value=msg))
    adapter = _adapter(channel)
    adapter._is_discord_rate_limit = MagicMock(return_value=False)

    result = await adapter.edit_message("555", "42", "progress")

    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"