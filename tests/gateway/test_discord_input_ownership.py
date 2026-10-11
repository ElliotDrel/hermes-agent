"""Accepted physical Discord input retains a real owner through composition."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import ProcessingOutcome
from gateway.run import GatewayRunner
from gateway.discord_composition import begin_composition_turn
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.test_discord_composition_buffer import _event


def lane():
    runner = object.__new__(GatewayRunner)
    overflow = []
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=overflow))
    runner._peek_session_state = runner._session_state
    runner._draining = False
    runner._pending_event_audio_paths = lambda event: []
    adapter = SimpleNamespace(_pending_messages={}, _client=SimpleNamespace(user=None))
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter, overflow


def fetchable(event):
    raw = event.raw_message
    raw.channel = SimpleNamespace(fetch_message=AsyncMock(
        side_effect=lambda mid: SimpleNamespace(content=raw.content)))


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', [False, True])
async def test_detached_registry_cannot_accept_but_selected_drain_still_can(selected):
    runner, adapter, overflow = lane()
    a, b = _event('first', 1), _event('second', 2)
    assert runner._queue_discord_composition('s', a, adapter)
    if selected:
        drained, _ = await runner._run_agent_drain_pending({'messages': []}, adapter, a.source, 's')
        assert drained is a
    else:
        adapter._pending_messages.clear()
    assert runner._queue_discord_composition('s', b, adapter)
    if selected:
        assert a._discord_composition.messages == [a.raw_message, b.raw_message]
        assert not adapter._pending_messages
    else:
        assert adapter._pending_messages['s'] is b
        assert a._discord_composition.messages == [a.raw_message]
        b._discord_composition.task.cancel()
    assert overflow == []
    a._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_actual_queued_followup_payload_order_and_history_prefix():
    runner, adapter, overflow = lane()
    a, b, c = _event('first', 1), _event('second', 2), _event('other sender', 3, author='other')
    for event in (a, b, c):
        fetchable(event)
        assert runner._queue_discord_composition('s', event, adapter)
    a.raw_message.content = 'edited first'
    a._discord_composition.deadline = c._discord_composition.deadline = 0
    await runner._seal_discord_composition('s', a, adapter)
    await runner._seal_discord_composition('s', c, adapter)
    history = [{'role': 'user', 'content': 'prior'}, {'role': 'assistant', 'content': 'answer'}]
    original = copy.deepcopy(history)
    payloads = []
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=lambda **kw: kw['event'].text)
    runner._session_key_for_source = lambda source: 's'
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._intake_adapter_for = lambda source: None
    runner._refresh_agent_cache_message_count = AsyncMock()

    async def model_boundary(**kw):
        payloads.append(kw)
        return {'final_response': 'done', 'messages': kw['history'] + [
            {'role': 'user', 'content': kw['message']}, {'role': 'assistant', 'content': 'done'}]}

    runner._run_agent = model_boundary
    ctx = SimpleNamespace(source=a.source, session_id='id', session_key='s', run_generation=1,
                          _interrupt_depth=0, history=history, _status_thread_metadata=None,
                          result_holder=[{'final_response': 'answer'}], context_prompt='stable cached system')
    result = {'final_response': 'answer', 'messages': history, 'interrupted': False}
    for text in ('edited first\nsecond', 'other sender'):
        event, pending = await runner._run_agent_drain_pending(result, adapter, a.source, 's')
        assert pending == text
        await runner._run_agent_queued_followup(ctx, adapter, pending, event, result, result, None)
        result['messages'] = payloads[-1]['history'] + [
            {'role': 'user', 'content': text}, {'role': 'assistant', 'content': 'done'}]
    assert [p['message'] for p in payloads] == ['edited first\nsecond', 'other sender']
    assert [p['inbound_message_id'] for p in payloads] == ['1', '3']
    assert all(p['context_prompt'] == 'stable cached system' for p in payloads)
    assert history == original
    assert payloads[1]['history'][:len(original)] == original
    assert [row['role'] for row in payloads[1]['history']] == ['user', 'assistant', 'user', 'assistant']
    assert runner._refresh_agent_cache_message_count.await_count == 2
    for event in (a, c):
        event._discord_composition.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,status,replied', [
    (ProcessingOutcome.SUCCESS, 'responded', 1),
    (ProcessingOutcome.FAILURE, 'failed', 0),
    (ProcessingOutcome.CANCELLED, 'cancelled', 0),
])
async def test_every_physical_member_recovery_state_and_completion_retry(tmp_path, monkeypatch, outcome, status, replied):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL', 'true')
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='placeholder'))
    runner, queue_adapter, _ = lane()
    a, b = _event('one', 1), _event('two', 2)
    for event in (a, b):
        event.raw_message.author = SimpleNamespace(id=42)
        fetchable(event)
        assert runner._queue_discord_composition('s', event, queue_adapter)
    a._discord_composition.deadline = 0
    await runner._seal_discord_composition('s', a, queue_adapter)
    state = a._discord_composition
    b.raw_message.remove_reaction.side_effect = [RuntimeError('transient failure'), None]
    await begin_composition_turn(a)
    assert not state.reaction_removed
    await adapter.on_processing_start(a)
    await adapter.on_processing_complete(a, outcome)
    assert state.reaction_removed
    assert b.raw_message.remove_reaction.await_count == 2
    rows = adapter._with_discord_recovery_db(lambda db: db.execute(
        'SELECT message_id,status,replied FROM discord_messages ORDER BY message_id').fetchall())
    assert rows == [('1', status, replied), ('2', status, replied)]
    a._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_stop_invalidates_selected_owner_before_async_teardown(monkeypatch):
    from tools import async_delegation
    runner, adapter, _ = lane()
    a, b = _event('first', 1), _event('second', 2)
    runner._queue_discord_composition('s', a, adapter)
    state = a._discord_composition
    state.selected = True
    adapter._pending_messages.clear()
    runner._peek_session_state = lambda key: None
    runner._interrupt_running_turn = lambda *a, **k: 1
    runner._drop_turn_slot = lambda *a, **k: None
    monkeypatch.setattr(async_delegation, 'interrupt_for_session', lambda **kw: None)
    await runner._interrupt_and_clear_session('s', a.source, interrupt_reason='stop', invalidation_reason='stop')
    assert state.cancelled
    assert runner._queue_discord_composition('s', b, adapter)
    assert adapter._pending_messages['s'] is b
    assert state.messages == [a.raw_message]
    for event in (a, b):
        event._discord_composition.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['command', 'media', 'security'])
async def test_queued_boundary_cannot_be_crossed_by_later_composition_join(boundary):
    runner, adapter, overflow = lane()
    a, b = _event('before', 1), _event('after', 3)
    runner._queue_discord_composition('s', a, adapter)
    barrier = _event('/queue separate' if boundary == 'command' else 'separate', 2)
    if boundary == 'security':
        barrier.allow_gateway_control = False
    if boundary == 'media':
        from gateway.platforms.event import MessageType
        barrier.message_type = MessageType.PHOTO
    runner._enqueue_fifo('s', barrier, adapter)
    assert runner._queue_discord_composition('s', b, adapter)
    assert overflow == [barrier, b]
    assert a._discord_composition.messages == [a.raw_message]
    for event in (a, b):
        event._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_real_discord_ingress_returns_refused_capacity_receipt(monkeypatch):
    from tests.gateway.test_busy_session_ack import _make_runner
    from tests.gateway.test_discord_free_response import FakeTextChannel, make_message
    runner, _ = _make_runner()
    runner._busy_input_mode = 'queue'
    runner._busy_text_mode = 'queue'
    runner._BUSY_QUEUE_MAX_PENDING = 0
    runner._send_busy_reply = AsyncMock()
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='placeholder'))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._busy_text_mode = 'queue'
    adapter._text_batch_delay_seconds = 0
    adapter._message_handler = AsyncMock()
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    runner._delivery_adapter_for = lambda source: adapter
    monkeypatch.setenv('DISCORD_AUTO_THREAD', 'false')
    monkeypatch.setenv('DISCORD_REQUIRE_MENTION', 'false')
    channel = FakeTextChannel(channel_id=123)
    message = make_message(channel=channel, content='not admitted')
    from gateway.config import Platform
    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.DISCORD, chat_id='123', chat_type='group', user_id='42')
    key = adapter._source_session_key(source)
    adapter._active_sessions[key] = asyncio.Event()
    adapter._heal_stale_session_lock = lambda key: None
    assert await adapter._handle_message(message, recovered=True) is False
    assert not adapter._pending_messages
    assert 'not accepted' in runner._send_busy_reply.await_args.args[-1]
