"""Native authority FIFO completion owns its progress bubble through final delivery."""
import asyncio
import logging
import json

import pytest

from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('no_waiter', [False, True], ids=['busy-observer', 'settled-no-waiter'])
@pytest.mark.parametrize('outcome', ['ok', 'media-ok', 'media-failed', 'media-unknown', 'decline', 'text-unknown',
                                     'exception', 'cancel', 'streamed', 'streamed-media-failed', 'streamed-partial', 'partial'])
async def test_fifo_progress_settles_exact_owner_after_aggregate_delivery(monkeypatch, tmp_path, outcome, no_waiter):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.rolling_activity import finish_turn_activity
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    from tests.gateway.test_rolling_activity import _turn
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.relay.egress import EGRESS_DECLINE_CODE

    image = tmp_path / 'result.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 24)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42,43')
    runner = GatewayRunner(GatewayConfig(group_sessions_per_user=False))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False,
                                           extra={'group_sessions_per_user': False}))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter, busy_text_mode='queue')
    adapter._busy_text_debounce_seconds = 0.02
    adapter._busy_text_hard_cap_seconds = 0.1
    edits, sends, completed, contexts = [], [], [], {}
    running, release, successor = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(chat_id, content, reply_to=None, metadata=None):
        sends.append(content)
        if 'answer B' in content:
            if outcome == 'text-unknown':
                return None
            if outcome == 'decline':
                return SendResult(success=False, raw_response={'code': EGRESS_DECLINE_CODE})
            if outcome == 'exception':
                raise RuntimeError('synthetic transport failure')
            if outcome == 'cancel':
                raise asyncio.CancelledError()
        return SendResult(success=True, message_id='sent')

    async def edit(chat_id, message_id, content, metadata=None):
        edits.append((message_id, content))
        return SendResult(success=True, message_id=message_id)

    async def images(**kwargs):
        if outcome == 'media-unknown':
            return None
        return SendResult(success=outcome not in ('media-failed', 'streamed-media-failed'))

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(adapter, 'edit_message', edit)
    monkeypatch.setattr(adapter, 'send_multiple_images', images)
    monkeypatch.setattr(adapter, 'send_typing', _noop)
    monkeypatch.setattr(adapter, 'stop_typing', _noop)
    runner._should_send_voice_reply = lambda *a, **kw: False
    # This invokes the real post-stream media aggregation, with a synthetic transport.
    runner._delivery_adapter_for = lambda source: adapter

    async def turn(event, source, quick_key, run_generation):
        label = event.text[0]
        if label == 'A':
            running.set()
            await release.wait()
        elif label == 'C':
            successor.set()
            # Deliberately overlap successor execution with predecessor transport settlement.
            await asyncio.sleep(0)
        ctx, progress = _turn(adapter, initial_message_id='progress-' + label)
        ctx.source = source
        ctx.activity_result = {'completed': True}
        task = asyncio.create_task(progress.send_progress_messages())
        ctx.progress_queue.put(('__activity_start__',))
        ctx.progress_queue.put('Working ' + label)
        await finish_turn_activity(ctx, task, logging.getLogger(__name__))
        runner._run_agent_schedule_bubble_cleanup(ctx.activity_result, adapter, ctx)
        adapter.register_confirmed_delivery_callback(ctx.progress_delivery_owner, lambda: completed.append(label))
        contexts[label] = ctx
        event._progress_delivery_owner = ctx.progress_delivery_owner
        event._progress_delivery_failed = label == 'B' and outcome.endswith('partial')
        response = 'answer ' + label
        if label == 'B' and 'media' in outcome:
            response += ' MEDIA:' + image.as_posix()
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = {'final_response': response, 'messages': [], 'completed': True}
        if label == 'B' and outcome.startswith('streamed'):
            # Real streaming-final delivery policy scans media and sets the stream/failure sidecars.
            return await runner._hmwa_deliver_turn_response(
                event, source, None, quick_key, run_generation, {'already_sent': True}, [], response, None, False)
        return response

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)

    def event(label, sender='42'):
        return MessageEvent(text=label, message_id='input-' + label,
                            source=adapter.build_source(chat_id='room', chat_type='group', user_id=sender))

    try:
        await adapter.handle_message(event('A'))
        await asyncio.wait_for(running.wait(), 10)
        b, c = event('B'), event('C', '43')
        if no_waiter:
            # A committed queued input without a process-local waiter follows boot/recovery delivery.
            await authority.admit_native(b)
            await authority.admit_native(c)
        else:
            await adapter.handle_message(b)
            await adapter.handle_message(c)
            assert b._gateway_accepted and c._gateway_accepted
        sid = runner.session_store.peek_session_id(runner.session_store._generate_session_key(b.source))
        rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
        assert [r['payload']['text'] for r in rows] == ['A', 'B', 'C']
        release.set()
        if no_waiter and outcome == 'cancel':
            await async_wait_until(lambda: any('answer B' in text for text in sends), timeout=10, what='cancelled delivery starts')
            await asyncio.sleep(0)
            assert 'B' not in completed and contexts['B'].progress_delivery_owner in adapter._confirmed_delivery_callbacks
            assert not successor.is_set()  # cancelling recovery delivery cancels its drain, not a successful turn
            return
        await asyncio.wait_for(successor.wait(), 10)
        await async_wait_until(lambda: 'C' in completed, timeout=10, what='successor delivery completes')
        tasks = [task for task in adapter._background_tasks if not task.done()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        success = outcome in ('ok', 'media-ok', 'streamed')
        assert ('B' in completed) is success
        assert (contexts['B'].progress_delivery_owner in adapter._confirmed_delivery_callbacks) is not success
        assert contexts['C'].progress_delivery_owner not in adapter._confirmed_delivery_callbacks
        headers = [text for message_id, text in edits if message_id == 'progress-B']
        assert ('Completed' in headers[-1]) is success
        assert 'Finishing' in headers[-1] or success
        assert any(text.startswith('answer C') for text in sends)
        assert all(r['status'] == 'terminal' for r in list_session_admissions(authority.db, session_id=sid, pending_only=False))
    finally:
        release.set()
        print('FIFO_PROGRESS_TRACE ' + json.dumps({
            'mode': 'settled-no-waiter' if no_waiter else 'busy-observer', 'outcome': outcome,
            'completed': completed, 'headers': edits,
            'pending': [label for label, ctx in contexts.items()
                        if ctx.progress_delivery_owner in adapter._confirmed_delivery_callbacks],
            'validation': 'native authority and SQLite, real rolling sender, synthetic transport; no live Discord UI',
        }, ensure_ascii=False))
        await asyncio.to_thread(authority.db.close)


async def _noop(*args, **kwargs):
    pass
