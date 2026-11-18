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
    runner._session_db.get_session.return_value = {"title": "Old Thread Title", "title_source": "user"}
    runner._session_db.set_session_title.return_value = True
    adapter = SimpleNamespace(send=AsyncMock(), rename_thread=AsyncMock(return_value=True), get_thread_name=AsyncMock(return_value="Old Thread Title"))
    runner._delivery_adapter_for = lambda source: adapter
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
    assert "Launch Plan" in result and "confirmed" in result
    runner._session_db.set_session_title_if_current.assert_awaited_once_with("session-1", "Launch Plan", expected_title="Old Thread Title", expected_source="user")
    adapter.rename_thread.assert_awaited_once_with("thread-1", "Launch Plan", only_if_current_name="Old Thread Title", raise_on_error=True)


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
    adapter.rename_thread.assert_awaited_once_with("thread-1", "Repair Discord Command Publishing", only_if_current_name="Old Thread Title", raise_on_error=True)


@pytest.mark.asyncio
async def test_rename_dispatches_while_agent_is_busy():
    from hermes_cli.commands import resolve_command
    runner, _ = _runner()
    command = resolve_command("rename")
    assert command and command.busy_policy == "dispatch"
    result = await runner._dispatch_busy_slash_command(_event(), command, "discord:thread-1", _event().source)
    assert "Renamed this thread" in result


@pytest.mark.asyncio
async def test_acknowledgement_precedes_validation_and_generation():
    runner, adapter = _runner()
    requested = "é" * 100 + "OVERFLOW"
    reply = await runner._handle_rename_command(_event("/rename " + requested))
    notice = adapter.send.call_args.args[1]
    assert "Current title: **Old Thread Title**" in notice
    assert requested[:100] in notice and "OVERFLOW" not in notice
    assert "108 characters" in notice and "100-character cutoff" in notice
    assert "First 100 characters" in reply and "108 characters" in reply
    assert ".." not in reply
    adapter.rename_thread.assert_not_awaited()
    runner._session_db.set_session_title_if_current.assert_not_awaited()
    runner._session_db.get_session.assert_not_awaited()

    adapter.send.reset_mock()
    runner._session_db.get_messages.return_value = [{"role": "user", "content": "Rename this project"}]
    def generate(*args):
        assert adapter.send.await_count == 1
        assert "Generating new title…" in adapter.send.call_args.args[1]
        return "Generated Name"
    with patch("agent.title_generator.generate_regenerated_title", side_effect=generate):
        reply = await runner._handle_rename_command(_event("/rename"))
    assert "Generated Name" in reply and "confirmed" in reply


@pytest.mark.asyncio
async def test_ack_failure_does_not_rename():
    runner, adapter = _runner()
    adapter.send.return_value = SimpleNamespace(success=False)
    reply = await runner._handle_rename_command(_event())
    assert "acknowledgement" in reply
    adapter.rename_thread.assert_not_awaited()
    runner._session_db.set_session_title_if_current.assert_not_awaited()
