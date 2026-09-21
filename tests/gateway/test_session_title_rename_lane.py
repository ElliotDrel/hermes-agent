"""Which title stage is allowed to spend a platform rename.

Titling is two-stage: a derived slice of the user's own words lands inline, and
the model's version replaces it a moment later. A local sidebar wants both. A
Discord thread or a Telegram topic wants only the second — renaming twice lands
on the same name at twice the cost, and Discord allows two channel renames per
ten minutes, so the throwaway can be the one that survives.
"""

from __future__ import annotations

import types
import weakref

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner


def _attach(lane):
    """Attach the title callback for *lane* and return (callback, renames)."""
    renames: list = []
    source = types.SimpleNamespace(platform=Platform.DISCORD, chat_id="chan-1")

    runner = types.SimpleNamespace(
        _is_telegram_topic_lane=lambda src: lane == "telegram",
        _is_discord_auto_thread_lane=lambda src: lane == "discord",
        _is_discord_manual_thread_lane=lambda src: lane == "manual-discord",
        _is_relay_discord_channel_lane=lambda src: False,
        _schedule_telegram_topic_title_rename=(
            lambda src, sid, title: renames.append(title)
        ),
        _schedule_discord_semantic_thread_rename=(
            lambda src, sid, title: renames.append(title)
        ),
    )
    holder = types.SimpleNamespace(
        _runner=runner,
        _attach_session_title_callback=TurnRunner._attach_session_title_callback,
    )
    agent = types.SimpleNamespace(session_id="sess-1")
    holder._attach_session_title_callback(
        holder, agent, types.SimpleNamespace(source=source)
    )
    return agent._on_session_title, renames


@pytest.mark.parametrize("lane", ["telegram", "discord", "manual-discord"])
def test_the_rename_waits_for_the_model_title(lane):
    callback, renames = _attach(lane)

    callback("fix the flaky auth test in log", "derived")
    assert renames == []

    callback("Fix flaky auth test", "llm")
    assert renames == ["Fix flaky auth test"]


def test_manual_thread_lane_requires_opt_in_and_captured_name():
    """Manual threads are eligible only with both explicit permission and a no-clobber name."""

    class ManualRenameRunner:
        _is_discord_manual_thread_lane = GatewayRunner._is_discord_manual_thread_lane

        def __init__(self, enabled):
            self.adapter = types.SimpleNamespace(
                config=types.SimpleNamespace(extra={"rename_manual_threads": enabled})
            )

        def _adapter_for_source(self, source):
            return self.adapter

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
        auto_thread_initial_name="Cronjob Response: daily-report",
    )

    assert ManualRenameRunner(True)._is_discord_manual_thread_lane(source) is True
    assert ManualRenameRunner(False)._is_discord_manual_thread_lane(source) is False
    source.auto_thread_initial_name = None
    assert ManualRenameRunner(True)._is_discord_manual_thread_lane(source) is False


def test_manual_thread_lane_uses_the_adapter_environment_aware_resolver(monkeypatch):
    """An environment-only opt-in must govern capture and callback eligibility alike."""
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_RENAME_MANUAL_THREADS", "true")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={"rename_manual_threads": False}))
    runner = types.SimpleNamespace(
        adapter=adapter,
        _adapter_for_source=lambda source: adapter,
        _is_discord_manual_thread_lane=GatewayRunner._is_discord_manual_thread_lane,
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
        auto_thread_initial_name="Cronjob Response: daily-report",
    )

    assert adapter._semantic_thread_initial_name(types.SimpleNamespace(name="Cronjob Response: daily-report"))
    assert runner._is_discord_manual_thread_lane(runner, source) is True


def test_manual_thread_initial_name_uses_current_discord_name_only_when_enabled():
    """The guard is captured from Discord itself, not reconstructed from message content."""
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    channel = types.SimpleNamespace(name="Cronjob Response: daily-report")
    enabled = DiscordAdapter(PlatformConfig(enabled=True, extra={"rename_manual_threads": True}))
    disabled = DiscordAdapter(PlatformConfig(enabled=True, extra={"rename_manual_threads": False}))

    assert enabled._semantic_thread_initial_name(channel) == channel.name
    assert disabled._semantic_thread_initial_name(channel) is None


@pytest.mark.anyio
@pytest.mark.parametrize("auto_created", [True, False])
async def test_native_thread_rename_passes_only_the_initial_name_guard(auto_created):
    """Hermes-created and opted-in manual lanes share the strict no-clobber contract."""
    calls: list[tuple[str, str, str | None]] = []

    class StrictNativeAdapter:
        config = types.SimpleNamespace(extra={"rename_manual_threads": True})

        async def rename_thread(
            self,
            thread_id: str,
            name: str,
            *,
            only_if_current_name: str | None = None,
        ) -> bool:
            calls.append((thread_id, name, only_if_current_name))
            return True

    class NativeRenameRunner:
        _is_discord_auto_thread_lane = GatewayRunner._is_discord_auto_thread_lane
        _is_discord_manual_thread_lane = GatewayRunner._is_discord_manual_thread_lane
        _sanitize_discord_thread_title = GatewayRunner._sanitize_discord_thread_title
        _rename_discord_auto_thread_for_session_title = (
            GatewayRunner._rename_discord_auto_thread_for_session_title
        )

        def __init__(self, adapter):
            self.adapters = {Platform.DISCORD: adapter}

        def _adapter_for_source(self, source):
            return self.adapters[source.platform]

    source = types.SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        thread_id="999",
        auto_thread_created=auto_created,
        auto_thread_initial_name="Initial words",
    )

    runner = NativeRenameRunner(StrictNativeAdapter())
    await runner._rename_discord_auto_thread_for_session_title(
        source,
        "session-1",
        "Semantic Session Title",
    )

    assert calls == [("999", "Semantic Session Title", "Initial words")]


def test_title_thread_copy_preserves_transport_adapter_ref(monkeypatch):
    """Multiplex-routed sources must keep their transport owner for side effects."""
    captured_sources = []

    class Adapter:
        pass

    adapter = Adapter()

    async def noop():
        return None

    def fake_schedule(coro, loop, logger=None, log_message=None):
        coro.close()
        return None

    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", fake_schedule)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
        profile="runtime-profile",
        auto_thread_created=True,
        auto_thread_initial_name="Initial words",
    )
    source._transport_adapter_ref = weakref.ref(adapter)

    runner = types.SimpleNamespace(
        _gateway_loop=types.SimpleNamespace(is_closed=lambda: False),
        _schedule_rename_from_title_thread=GatewayRunner._schedule_rename_from_title_thread,
    )

    runner._schedule_rename_from_title_thread(
        runner,
        source,
        lambda copied: captured_sources.append(copied) or noop(),
        "Discord semantic thread rename",
    )

    assert len(captured_sources) == 1
    copied = captured_sources[0]
    assert copied is not source
    assert copied.profile == "runtime-profile"
    assert copied._transport_adapter_ref() is adapter
