"""Graceful restart drains accepted Discord FIFO, not new ingress or hard-stop work."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.restart_test_helpers import make_restart_runner
from tests.gateway.test_discord_composition_buffer import _event
from tests.gateway.test_discord_input_ownership import fetchable


@pytest.mark.asyncio
@pytest.mark.parametrize('after_turn', [True, False])
async def test_restart_wait_drains_real_composition_fifo_and_recovery_accounting(tmp_path, monkeypatch, after_turn):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL', 'true')
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='placeholder'))
    adapter.send_typing = AsyncMock()
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    runner, _ = make_restart_runner(adapter)
    runner.adapters = {adapter.platform: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._intake_adapter_for = lambda source: adapter
    runner._pending_event_audio_paths = lambda event: []
    runner._restart_after_turn_timeout = 5 if after_turn else 0
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._active_deferred_agent_worker_count = lambda: 0
    runner._wedged_agent_count = lambda: 0
    runner._session_key_for_source = lambda source: 's'
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=lambda **kw: kw['event'].text)
    runner._refresh_agent_cache_message_count = AsyncMock()
    events = [_event('first', 1), _event('second', 2), _event('other', 3, author='other')]
    for event in events:
        event.raw_message.author = SimpleNamespace(id=42)
        fetchable(event)
        assert runner._queue_discord_composition('s', event, adapter)
    head, _, tail = events
    head.raw_message.content = 'edited first'
    tail.raw_message.content = '/stop'  # admitted text cannot gain control authority at seal
    history = [{'role': 'user', 'content': 'prior'}, {'role': 'assistant', 'content': 'answer'}]
    original = copy.deepcopy(history)
    result = {'final_response': 'answer', 'messages': history, 'interrupted': False}
    delivered, entered = asyncio.Event(), asyncio.Event()
    payloads = []
    runner._running_agents['s'] = MagicMock()
    runner._run_agent_deliver_first_response = AsyncMock(side_effect=lambda *a: delivered.set())

    async def model_boundary(**kw):
        payloads.append(kw)
        return {'final_response': 'done', 'messages': kw['history'] + [
            {'role': 'user', 'content': kw['message']}, {'role': 'assistant', 'content': 'done'}]}
    runner._run_agent = model_boundary
    ctx = SimpleNamespace(source=head.source, session_id='id', session_key='s', run_generation=1,
                          progress_compositor=None,
                          _interrupt_depth=0, history=history, _status_thread_metadata=None,
                          result_holder=[result], context_prompt='stable cached system')

    async def stop(**kw):
        assert runner._draining
        entered.set()
        _, timed_out = await runner._drain_active_agents(5)
        assert not timed_out
    runner.stop = stop
    assert runner.request_restart(detached=False, via_service=True)
    if not after_turn:
        await asyncio.wait_for(entered.wait(), 2)
    selected, text = await runner._run_agent_drain_pending(result, adapter, head.source, 's')
    assert selected is head
    job = asyncio.create_task(runner._run_agent_queued_followup(ctx, adapter, text, selected, result, result, None))
    await asyncio.wait_for(delivered.wait(), 2)
    assert not job.done() and runner._running_agent_count() == 1
    assert not runner._restart_task.done()
    head._discord_composition.deadline = 0
    await runner._seal_discord_composition('s', head, adapter)
    result = await job
    selected, text = await runner._run_agent_drain_pending(result, adapter, head.source, 's')
    assert selected is tail
    tail._discord_composition.deadline = 0
    await runner._seal_discord_composition('s', tail, adapter)
    await runner._run_agent_queued_followup(ctx, adapter, text, selected, result, result, None)
    assert [p['message'] for p in payloads] == ['edited first\nsecond', 'other']
    assert [p['inbound_message_id'] for p in payloads] == ['1', '3']
    assert all(p['context_prompt'] == ctx.context_prompt for p in payloads)
    assert history == original and payloads[1]['history'][:len(original)] == original
    assert [r['role'] for r in payloads[1]['history']] == ['user', 'assistant', 'user', 'assistant']
    assert runner._refresh_agent_cache_message_count.await_count == 2
    rows = adapter._with_discord_recovery_db(lambda db: db.execute(
        'SELECT message_id,status,replied FROM discord_messages ORDER BY message_id').fetchall())
    assert rows == [('1', 'responded', 1), ('2', 'responded', 1), ('3', 'responded', 1)]
    assert not adapter._pending_messages and not runner._overflow_queue('s')
    runner._running_agents.clear()
    await asyncio.wait_for(runner._restart_task, 2)
    for event in (head, tail):
        event._discord_composition.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted_at', ['worker', 'seal', 'cancel', 'stop'])
async def test_expired_drain_preserves_residual_owner_but_stop_revokes_it(interrupted_at):
    from tests.gateway.test_discord_input_ownership import lane
    runner, adapter, overflow = lane()
    runner._draining = True
    head, tail = _event('head', 1), _event('tail', 2, author='other')
    for event in (head, tail):
        fetchable(event)
        runner._queue_discord_composition('s', event, adapter)
    result = {'messages': [], 'final_response': 'answer', 'interrupted': interrupted_at == 'worker'}
    if interrupted_at == 'worker':
        assert await runner._run_agent_drain_pending(result, adapter, head.source, 's') == (None, None)
        assert adapter._pending_messages['s'] is head and overflow == [tail]
    else:
        selected, text = await runner._run_agent_drain_pending(result, adapter, head.source, 's')
        assert selected is head and adapter._pending_messages['s'] is tail
        delivered = asyncio.Event()
        runner._run_agent_deliver_first_response = AsyncMock(side_effect=lambda *a: delivered.set())
        ctx = SimpleNamespace(source=head.source, session_id='id', session_key='s', run_generation=1,
                          progress_compositor=None,
                              _interrupt_depth=0, history=[], _status_thread_metadata=None)
        job = asyncio.create_task(runner._run_agent_queued_followup(ctx, adapter, text, selected, result, result, None))
        await asyncio.wait_for(delivered.wait(), 2)
        if interrupted_at == 'stop':
            head._discord_composition.cancelled = True
            adapter._pending_messages.clear()
        elif interrupted_at == 'cancel':
            job.cancel()
        else:
            runner._drain_interrupted = True
        head._discord_composition.sealed.set()
        if interrupted_at == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(job, 2)
            runner._drain_interrupted = True
        else:
            assert await asyncio.wait_for(job, 2) is result
        if interrupted_at == 'stop':
            assert not adapter._pending_messages and not overflow
        else:
            assert adapter._pending_messages['s'] is head and overflow == [tail]
            assert not head._discord_composition.selected
            assert await runner._run_agent_drain_pending(result, adapter, head.source, 's') == (None, None)
    for event in (head, tail):
        event._discord_composition.task.cancel()


@pytest.mark.asyncio
async def test_adapter_timeout_cleanup_does_not_redrain_or_complete_residual():
    from gateway.platforms.event import ProcessingOutcome
    from tests.gateway.restart_test_helpers import RestartTestAdapter
    adapter = RestartTestAdapter()
    adapter.gateway_runner = SimpleNamespace(_drain_interrupted=True)
    current, residual = _event('current', 1), _event('residual', 2)
    adapter._pending_messages['s'] = residual
    adapter._message_handler = AsyncMock(return_value=None)
    adapter.on_processing_complete = AsyncMock()
    adapter._spawn_drain_task = MagicMock()
    await adapter._process_message_background(current, 's')
    assert adapter._pending_messages['s'] is residual
    adapter._spawn_drain_task.assert_not_called()
    assert adapter.on_processing_complete.await_args.args == (current, ProcessingOutcome.SUCCESS)


@pytest.mark.asyncio
async def test_fresh_discord_adapter_recovers_residual_from_native_history_with_live_gates(tmp_path, monkeypatch):
    from tests.gateway.test_discord_missed_message_backfill import FakeChannel, make_message
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL', 'true')
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS', '123')
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    monkeypatch.setenv('DISCORD_ALLOW_ALL_USERS', 'false')
    monkeypatch.setenv('DISCORD_REQUIRE_MENTION', 'false')
    monkeypatch.setenv('DISCORD_AUTO_THREAD', 'false')
    channel = FakeChannel()
    channel.fetch_message = AsyncMock()
    first = make_message(message_id=1, channel=channel, content='head')
    denied = make_message(message_id=2, channel=channel, author_id=77, content='unauthorized')
    last = make_message(message_id=3, channel=channel, content='tail')
    channel._history_messages = [last, denied, first]  # Discord REST returns newest first
    old = DiscordAdapter(PlatformConfig(enabled=True, token='placeholder'))
    for message in (first, last):
        old._record_discord_message_seen(message, status='received')
    fresh = DiscordAdapter(PlatformConfig(enabled=True, token='placeholder'))
    fresh._client = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda cid: channel)
    fresh._allowed_user_ids = fresh._get_allowed_users()  # connect's real configuration consumer
    admitted = []
    async def handler(event):
        admitted.append(event)
        return None
    fresh.set_message_handler(handler)
    fresh._text_batch_delay_seconds = 0
    await fresh._run_missed_message_backfill()
    async def settle():
        while fresh._background_tasks:
            await asyncio.gather(*list(fresh._background_tasks))
    await asyncio.wait_for(settle(), 5)
    assert [event.message_id for event in admitted] == ['1', '3']
    assert [event.text for event in admitted] == ['head', 'tail']
    assert all(event.raw_message in (first, last) for event in admitted)
    assert all(event.source.user_id == '42' and event.source.chat_id == '123' for event in admitted)
    assert fresh._discord_message_is_persistently_complete('1')
    assert fresh._discord_message_is_persistently_complete('3')
