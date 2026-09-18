from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(session_id="session-1")
    runner._session_db = AsyncMock()
    runner._session_db.get_session_title.side_effect = [
        "Old Thread Title",
        "Old Thread Title",
        "New Thread Title",
    ]
    runner._session_db.get_session_title_source.return_value = "llm"
    runner._session_db.get_messages.return_value = [
        {"role": "user", "content": "Fix the stale Discord reconnect state."},
        {"role": "assistant", "content": "The reconnect cache is stale."},
    ]
    runner._session_db.set_session_title.return_value = True
    adapter = SimpleNamespace(
        send=AsyncMock(),
        rename_thread=AsyncMock(return_value=True),
    )
    # GatewayRunner exposes adapters as a platform-keyed mapping; it has no
    # get_adapter() method. Keep this fixture aligned with the live runtime so
    # the handler's Discord lookup cannot be masked by a test-only attribute.
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter


@pytest.mark.asyncio
async def test_rename_generates_from_history_and_renames_discord_thread():
    runner, adapter = _runner()
    event = MessageEvent(
        text="/rename",
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id="user-1",
            chat_id="thread-1",
            thread_id="thread-1",
            chat_type="thread",
        ),
    )

    with patch(
        "agent.title_generator.generate_regenerated_title",
        return_value="Fix Stale Discord Reconnects",
    ) as generate:
        result = await runner._handle_rename_command(event)

    assert result == (
        "Renamed this thread from **Old Thread Title** to **Fix Stale Discord Reconnects**."
    )
    adapter.send.assert_awaited_once_with(
        "thread-1", "Generating a replacement title. Current title “Old Thread Title”."
    )
    generate.assert_called_once()
    runner._session_db.set_session_title.assert_awaited_once_with(
        "session-1", "Fix Stale Discord Reconnects"
    )
    adapter.rename_thread.assert_awaited_once_with(
        "thread-1", "Fix Stale Discord Reconnects", raise_on_error=True
    )


@pytest.mark.asyncio
async def test_rename_uses_explicit_title_without_title_generation():
    runner, adapter = _runner()
    event = MessageEvent(
        text="/rename Launch Plan",
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id="user-1",
            chat_id="thread-1",
            thread_id="thread-1",
            chat_type="thread",
        ),
    )

    with patch(
        "agent.title_generator.generate_regenerated_title",
        return_value="Generated Title That Must Not Be Used",
    ) as generate:
        result = await runner._handle_rename_command(event)

    assert result == "Renamed this thread from **Old Thread Title** to **Launch Plan**."
    generate.assert_not_called()
    runner._session_db.get_messages.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "thread-1", "Renaming this thread. Current title “Old Thread Title”. Requested title “Launch Plan”."
    )
    runner._session_db.set_session_title.assert_awaited_once_with("session-1", "Launch Plan")
    adapter.rename_thread.assert_awaited_once_with(
        "thread-1", "Launch Plan", raise_on_error=True
    )


@pytest.mark.asyncio
async def test_manual_rename_reports_rate_limit_and_restored_title():
    runner, adapter = _runner()
    runner._session_db.get_session_title.side_effect = ["Old Thread Title", "Launch Plan"]

    class RateLimitedRename(Exception):
        status = 429
        retry_after = 486.9

    adapter.rename_thread.side_effect = RateLimitedRename("Too many requests")
    event = MessageEvent(
        text="/rename Launch Plan",
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id="user-1",
            chat_id="thread-1",
            thread_id="thread-1",
            chat_type="thread",
        ),
    )

    result = await runner._handle_rename_command(event)

    assert result == (
        "Discord rate-limited the rename to **Launch Plan**. The thread title did not change. "
        "Retry in about 487 seconds. The saved title was restored to **Old Thread Title**."
    )
    adapter.send.assert_awaited_once_with(
        "thread-1", "Renaming this thread. Current title “Old Thread Title”. Requested title “Launch Plan”."
    )
    runner._session_db.set_session_title.assert_has_awaits([
        call("session-1", "Launch Plan"),
        call("session-1", "Old Thread Title"),
    ])


@pytest.mark.asyncio
async def test_manual_rename_reports_discordpy_ratelimited_without_status():
    """discord.py raises ``RateLimited`` with ``retry_after`` and NO ``status``.

    This is the exact shape produced in production when discord.http logs
    "Timeout of 48.27 was too long, erroring instead" for a thread-rename
    PATCH. A handler keying only on ``status == 429`` misclassifies it as an
    unknown failure, so the user never learns it was a rate limit.
    """
    runner, adapter = _runner()
    runner._session_db.get_session_title.side_effect = ["Old Thread Title", "Launch Plan"]

    class RateLimited(Exception):
        # No `status` attribute — mirrors discord.errors.RateLimited.
        def __init__(self, retry_after):
            super().__init__(f"Too many requests. Retry in {retry_after:.2f} seconds.")
            self.retry_after = retry_after

    adapter.rename_thread.side_effect = RateLimited(48.27)
    event = MessageEvent(
        text="/rename Launch Plan",
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id="user-1",
            chat_id="thread-1",
            thread_id="thread-1",
            chat_type="thread",
        ),
    )

    result = await runner._handle_rename_command(event)

    assert result == (
        "Discord rate-limited the rename to **Launch Plan**. The thread title did not change. "
        "Retry in about 49 seconds. The saved title was restored to **Old Thread Title**."
    )


@pytest.mark.asyncio
async def test_manual_rename_reports_unexpected_error_type_and_message():
    """An unclassified failure must still name the real exception."""
    runner, adapter = _runner()
    runner._session_db.get_session_title.side_effect = ["Old Thread Title", "Launch Plan"]

    adapter.rename_thread.side_effect = RuntimeError("websocket closed")
    event = MessageEvent(
        text="/rename Launch Plan",
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id="user-1",
            chat_id="thread-1",
            thread_id="thread-1",
            chat_type="thread",
        ),
    )

    result = await runner._handle_rename_command(event)

    assert "RuntimeError" in result
    assert "websocket closed" in result
    assert "**Launch Plan**" in result
    assert "restored to **Old Thread Title**" in result


@pytest.mark.asyncio
async def test_rename_dispatches_while_agent_is_running():
    """`/rename` must not hit the generic mid-run busy-reject catch-all.

    It reads session history from the DB, runs the title model off-thread, and
    writes only the session title plus the Discord thread name — none of which
    the in-flight turn owns. Without busy_policy="dispatch" plus an entry in
    _gateway_plain_command_handlers(), the Guard-2 dispatcher returns
    "⏳ Agent is running — `/rename` can't run mid-turn." instead.
    """
    from hermes_cli.commands import resolve_command

    runner, adapter = _runner()
    cmd_def = resolve_command("rename")
    assert cmd_def is not None
    assert cmd_def.busy_policy == "dispatch"

    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="user-1",
        chat_id="thread-1",
        thread_id="thread-1",
        chat_type="thread",
    )
    event = MessageEvent(text="/rename", source=source)

    with patch(
        "agent.title_generator.generate_regenerated_title",
        return_value="Fix Stale Discord Reconnects",
    ):
        result = await runner._dispatch_busy_slash_command(
            event, cmd_def, "discord:thread-1", source,
        )

    assert result == (
        "Renamed this thread from **Old Thread Title** to **Fix Stale Discord Reconnects**."
    )
    assert "can't run mid-turn" not in result
    adapter.rename_thread.assert_awaited_once_with(
        "thread-1", "Fix Stale Discord Reconnects", raise_on_error=True
    )


@pytest.mark.asyncio
async def test_rename_rejects_non_thread_context():
    runner, _adapter = _runner()
    event = MessageEvent(
        text="/rename",
        source=SessionSource(platform=Platform.DISCORD, user_id="user-1", chat_id="channel-1"),
    )

    assert await runner._handle_rename_command(event) == "`/rename` can only be used inside a Discord thread."
