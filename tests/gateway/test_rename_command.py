"""Regression coverage for the Discord-only /rename thread workflow."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _runner():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(session_id="session-1")
    runner._session_db = AsyncMock()
    runner._session_db.get_session_title.return_value = "Old Thread Title"
    runner._session_db.set_session_title.return_value = True
    adapter = SimpleNamespace(rename_thread=AsyncMock(return_value=True))
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter


def _event(text="/rename Launch Plan"):
    return MessageEvent(text=text, source=SessionSource(
        platform=Platform.DISCORD, user_id="user-1", chat_id="thread-1",
        thread_id="thread-1", chat_type="thread",
    ))


@pytest.mark.asyncio
async def test_rename_sets_visible_discord_thread_and_session_title():
    runner, adapter = _runner()
    result = await runner._handle_rename_command(_event())
    assert result == "Renamed this thread from **Old Thread Title** to **Launch Plan**."
    runner._session_db.set_session_title.assert_awaited_once_with("session-1", "Launch Plan")
    adapter.rename_thread.assert_awaited_once_with("thread-1", "Launch Plan", raise_on_error=True)


@pytest.mark.asyncio
async def test_rename_rejects_non_thread_context():
    runner, _ = _runner()
    event = MessageEvent(text="/rename Launch Plan", source=SessionSource(
        platform=Platform.DISCORD, user_id="user-1", chat_id="channel-1",
    ))
    assert await runner._handle_rename_command(event) == "`/rename` can only be used inside a Discord thread."


@pytest.mark.asyncio
async def test_bare_rename_uses_conversation_aware_regeneration_api():
    """Bare /rename keeps the first goal and assistant technical context available."""
    runner, adapter = _runner()
    runner._session_db.get_messages.return_value = [
        {"role": "user", "content": "Repair Discord slash-command publishing."},
        {"role": "assistant", "content": "Ignored for title input."},
    ]
    with patch("agent.title_generator.generate_regenerated_title", return_value="Repair Discord Command Publishing") as generate:
        result = await runner._handle_rename_command(_event("/rename"))
    assert "Repair Discord Command Publishing" in result
    generate.assert_called_once()
    adapter.rename_thread.assert_awaited_once_with("thread-1", "Repair Discord Command Publishing", raise_on_error=True)


@pytest.mark.asyncio
async def test_rename_dispatches_while_agent_is_busy():
    from hermes_cli.commands import resolve_command
    runner, _ = _runner()
    command = resolve_command("rename")
    assert command and command.busy_policy == "dispatch"
    result = await runner._dispatch_busy_slash_command(_event(), command, "discord:thread-1", _event().source)
    assert "Renamed this thread" in result
