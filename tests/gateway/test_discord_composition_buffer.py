"""Discord queue-mode composition window regressions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.run import GatewayRunner


def _event(text, number, author="one", thread="thread"):
    source = SessionSource(platform=Platform.DISCORD, chat_id=thread, thread_id=thread,
                           chat_type="group", user_id=author)
    raw = SimpleNamespace(id=number, content=text, add_reaction=AsyncMock(), remove_reaction=AsyncMock())
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                        message_id=str(number), raw_message=raw)


@pytest.mark.asyncio
async def test_fixed_window_seals_edits_and_waits_even_when_run_finishes(monkeypatch):
    import gateway.discord_composition as composition
    clock = [100.0]
    monkeypatch.setattr(composition.time, "monotonic", lambda: clock[0])
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=object()))
    runner = object.__new__(GatewayRunner)
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=[]))
    runner._peek_session_state = runner._session_state
    runner._adapter_for_source = lambda source: adapter
    a, b = _event("first", 1), _event("second", 2)
    for event in (a, b):
        raw = event.raw_message
        raw.channel = SimpleNamespace(fetch_message=AsyncMock(
            side_effect=lambda mid, raw=raw: SimpleNamespace(content=raw.content)))
    first_raw = a.raw_message
    assert runner._queue_discord_composition("s", a, adapter)
    clock[0] = 129.0
    assert runner._queue_discord_composition("s", b, adapter)
    assert adapter._pending_messages["s"] is a
    assert not a.raw_message.add_reaction.await_count
    task = asyncio.create_task(composition.begin_composition_turn(a))
    await asyncio.sleep(0)
    assert not task.done()
    a.raw_message.content = "edited first"
    clock[0] = 130.0
    await runner._seal_discord_composition("s", a, adapter)
    await task
    assert a.text == "edited first\nsecond"
    assert a.message_id == "2"
    assert first_raw.channel.fetch_message.await_count == 1
    assert b.raw_message.channel.fetch_message.await_count == 1
    b.raw_message.content = "too late"
    assert a.text == "edited first\nsecond"
    assert not b.raw_message.add_reaction.await_count  # run started before sealing


@pytest.mark.asyncio
async def test_deadline_sender_and_security_boundaries_and_waiting_reaction(monkeypatch):
    import gateway.discord_composition as composition
    clock = [0.0]
    monkeypatch.setattr(composition.time, "monotonic", lambda: clock[0])
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=object()))
    runner = object.__new__(GatewayRunner)
    overflow = []
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=overflow))
    runner._peek_session_state = runner._session_state
    runner._adapter_for_source = lambda source: adapter
    a, b, c, d = (_event("a", 1), _event("b", 2, "other"),
                  _event("c", 3), _event("d", 4))
    for event in (a, b, c, d):
        raw = event.raw_message
        raw.channel = SimpleNamespace(fetch_message=AsyncMock(
            side_effect=lambda mid, raw=raw: SimpleNamespace(content=raw.content)))
    assert runner._queue_discord_composition("s", a, adapter)
    clock[0] = 1.0
    assert runner._queue_discord_composition("s", b, adapter)
    c.metadata = {"hermes_plugin_id": "unsafe"}
    assert not runner._queue_discord_composition("s", c, adapter)
    clock[0] = 30.0
    assert runner._queue_discord_composition("s", d, adapter)
    assert [e.text for e in overflow] == ["b", "d"]
    await runner._seal_discord_composition("s", a, adapter)
    assert a.raw_message.add_reaction.await_args.args == ("⏳",)
    await composition.begin_composition_turn(a)
    assert a.raw_message.remove_reaction.await_args.args[0] == "⏳"
    for state in runner._discord_compositions.values():
        state.task.cancel()


@pytest.mark.asyncio
async def test_begin_twice_removes_waiting_marker_once_even_if_removal_fails():
    from gateway.discord_composition import begin_composition_turn, Composition
    a = _event("hello", 1)
    state = Composition(event=a, deadline=0, messages=[a.raw_message])
    a._discord_composition = state
    a._discord_composition_bot = object()
    state.reacted = True
    state.sealed.set()
    a.raw_message.remove_reaction.side_effect = RuntimeError("permission denied")
    await begin_composition_turn(a)
    await begin_composition_turn(a)
    assert a.raw_message.remove_reaction.await_count == 1


def test_media_after_composition_stays_separate_fifo_turn():
    adapter = SimpleNamespace(_pending_messages={})
    runner = object.__new__(GatewayRunner)
    overflow = []
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=overflow))
    runner._peek_session_state = runner._session_state
    runner._adapter_for_source = lambda source: adapter
    a = _event("text", 1)
    from gateway.discord_composition import Composition
    a._discord_composition = Composition(event=a, deadline=30, messages=[a.raw_message])
    adapter._pending_messages["s"] = a
    photo = _event("", 2)
    photo.message_type = MessageType.PHOTO
    photo.media_urls = ["https://example.org/p.png"]
    runner._queue_or_replace_pending_event("s", photo)
    assert adapter._pending_messages["s"] is a
    assert overflow == [photo]


@pytest.mark.asyncio
async def test_seal_preserves_discord_mention_normalization():
    from gateway.discord_composition import Composition, seal_composition
    a = _event("question", 1)
    a.raw_message.content = "<@77> question"
    a.raw_message.channel = SimpleNamespace(fetch_message=AsyncMock(
        return_value=SimpleNamespace(content="<@77> revised question")))
    state = Composition(event=a, deadline=0, messages=[a.raw_message])
    a._discord_composition_bot = SimpleNamespace(id=77)
    state.begun = True
    await seal_composition(state, None)
    assert a.text == "revised question"


@pytest.mark.asyncio
async def test_edit_cannot_turn_normal_text_into_control_command():
    from gateway.discord_composition import Composition, seal_composition
    a = _event("normal", 1)
    a.raw_message.channel = SimpleNamespace(fetch_message=AsyncMock(
        return_value=SimpleNamespace(content="/stop")))
    state = Composition(event=a, deadline=0, messages=[a.raw_message])
    state.begun = True
    await seal_composition(state, None)
    assert a.text == "normal"


@pytest.mark.asyncio
async def test_changed_reply_context_cannot_merge_into_first_event():
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=None))
    runner = object.__new__(GatewayRunner)
    overflow = []
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=overflow))
    runner._peek_session_state = runner._session_state
    runner._adapter_for_source = lambda source: adapter
    a, b = _event("a", 1), _event("b", 2)
    b.reply_to_message_id = "different-parent"
    assert runner._queue_discord_composition("s", a, adapter)
    assert runner._queue_discord_composition("s", b, adapter)
    assert len(a._discord_composition.messages) == 1
    assert overflow == [b]
    a._discord_composition.task.cancel()
    b._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_busy_handler_routes_normal_discord_queue_without_ack():
    from tests.gateway.test_busy_session_ack import _make_runner
    from gateway.session import build_session_key
    from unittest.mock import MagicMock
    runner, _ = _make_runner()
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "interrupt"
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=None))
    a = _event("first", 1)
    key = build_session_key(a.source)
    runner.adapters[a.source.platform] = adapter
    runner._running_agents[key] = MagicMock()
    assert await runner._handle_active_session_busy_message(a, key)
    assert adapter._pending_messages[key] is a
    assert a._discord_composition.messages == [a.raw_message]
    a._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_buffered_processing_hooks_emit_no_other_emoji():
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig
    from gateway.platforms.event import ProcessingOutcome
    from unittest.mock import patch
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="placeholder"))
    adapter._client = SimpleNamespace(user=object())
    a = _event("hello", 1)
    from gateway.discord_composition import Composition
    a._discord_composition = Composition(event=a, deadline=0, messages=[a.raw_message])
    with patch.object(adapter, "_reactions_enabled", return_value=True), \
         patch.object(adapter, "_record_discord_processing_start"), \
         patch.object(adapter, "_record_discord_processing_complete"):
        await adapter.on_processing_start(a)
        await adapter.on_processing_complete(a, ProcessingOutcome.FAILURE)
    a.raw_message.add_reaction.assert_not_awaited()
    a.raw_message.remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_fifo_does_not_fall_through_to_adapter_text_merge():
    from tests.gateway.test_busy_session_ack import _make_runner
    from gateway.session import build_session_key
    from unittest.mock import MagicMock
    runner, _ = _make_runner()
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=None))
    a = _event("full", 1)
    key = build_session_key(a.source)
    runner.adapters[a.source.platform] = adapter
    runner._running_agents[key] = MagicMock()
    runner._BUSY_QUEUE_MAX_PENDING = 0
    assert await runner._handle_active_session_busy_message(a, key)
    assert key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_removed_queue_item_gets_no_waiting_reaction(monkeypatch):
    import gateway.discord_composition as composition
    clock = [0.0]
    monkeypatch.setattr(composition.time, "monotonic", lambda: clock[0])
    runner = object.__new__(GatewayRunner)
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=[]))
    runner._peek_session_state = runner._session_state
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=None))
    a = _event("cancelled", 1)
    a.raw_message.channel = SimpleNamespace(fetch_message=AsyncMock(
        return_value=SimpleNamespace(content="cancelled")))
    assert runner._queue_discord_composition("s", a, adapter)
    adapter._pending_messages.clear()  # /stop or reset removed this queue item
    clock[0] = 30.0
    await runner._seal_discord_composition("s", a, adapter)
    a.raw_message.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_failure_uses_original_receipt_not_mutated_cache():
    from gateway.discord_composition import Composition, seal_composition
    a = _event("receipt", 1)
    a.raw_message.content = "/stop"
    a.raw_message.channel = SimpleNamespace(fetch_message=AsyncMock(
        side_effect=RuntimeError("Discord unavailable")))
    state = Composition(event=a, deadline=0, messages=[a.raw_message], received=["receipt"])
    state.begun = True
    await seal_composition(state, None)
    assert a.text == "receipt"


@pytest.mark.asyncio
async def test_thread_history_backfill_does_not_split_same_sender_window(monkeypatch):
    import gateway.discord_composition as composition
    clock = [0.0]
    monkeypatch.setattr(composition.time, "monotonic", lambda: clock[0])
    runner = object.__new__(GatewayRunner)
    overflow = []
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=overflow))
    runner._peek_session_state = runner._session_state
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=None))
    a, b = _event("first", 1), _event("second", 2)
    a.channel_context = "[Recent channel messages]\n[User] earlier"
    b.channel_context = a.channel_context + "\n[User] first"
    assert runner._queue_discord_composition("s", a, adapter)
    clock[0] = 1.0
    assert runner._queue_discord_composition("s", b, adapter)
    assert adapter._pending_messages["s"] is a
    assert overflow == []
    assert a._discord_composition.messages == [a.raw_message, b.raw_message]
    a._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_busy_discord_text_reaches_composer_before_ingress_batch(monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig
    from gateway.session import build_session_key
    from tests.gateway.test_discord_free_response import FakeTextChannel, make_message
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="placeholder"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._busy_text_mode = "queue"
    adapter._text_batch_delay_seconds = 0.6
    adapter.handle_message = AsyncMock()
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    channel = FakeTextChannel(channel_id=123)
    a = make_message(channel=channel, content="first")
    b = make_message(channel=channel, content="second")
    b.id = 124
    from gateway.config import Platform
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="42")
    adapter._active_sessions[build_session_key(source)] = asyncio.Event()
    await adapter._handle_message(a)
    await adapter._handle_message(b)
    assert adapter.handle_message.await_count == 2
    assert [call.args[0].message_id for call in adapter.handle_message.await_args_list] == ["123", "124"]
    assert adapter._pending_text_batches == {}
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_finished_turn_dequeues_unsealed_composition_without_waiting():
    from gateway.discord_composition import Composition
    runner = object.__new__(GatewayRunner)
    runner._peek_session_state = lambda key: None
    runner._draining = False
    runner._pending_event_audio_paths = lambda event: []
    a = _event("follow up", 1)
    a._discord_composition = Composition(event=a, deadline=30, messages=[a.raw_message])
    adapter = SimpleNamespace(_pending_messages={"s": a})
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    # The caller delivers the finished task's answer in _run_agent_queued_followup.
    # Dequeue must not block that delivery while the new message is still editable.
    pending_event, pending = await asyncio.wait_for(
        runner._run_agent_drain_pending({"final_response": "current answer"}, adapter, a.source, "s"),
        timeout=0.2,
    )
    assert pending_event is a
    assert pending == "follow up"


@pytest.mark.asyncio
async def test_current_answer_is_delivered_before_queued_window_closes():
    from gateway.discord_composition import Composition
    runner = object.__new__(GatewayRunner)
    a = _event("queued", 1)
    state = Composition(event=a, deadline=30, messages=[a.raw_message])
    a._discord_composition = state
    delivered = asyncio.Event()

    async def deliver(*args):
        delivered.set()

    runner._run_agent_deliver_first_response = deliver
    runner._is_goal_continuation_event = lambda event: True
    runner._goal_still_active_for_session = lambda session_id: False
    ctx = SimpleNamespace(source=a.source, session_id="id", session_key="s",
                          run_generation=1, _interrupt_depth=0, history=[],
                          _status_thread_metadata=None, result_holder=[{"final_response": "answer"}])
    job = asyncio.create_task(runner._run_agent_queued_followup(
        ctx, None, a.text, a, {"final_response": "answer"},
        {"messages": [], "interrupted": False}, None))
    await asyncio.wait_for(delivered.wait(), timeout=0.2)
    assert not job.done()  # The next turn still waits for edits.
    state.sealed.set()
    await asyncio.wait_for(job, timeout=0.2)
