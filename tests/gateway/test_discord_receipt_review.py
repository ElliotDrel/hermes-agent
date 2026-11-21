"""Review regressions for fixed Discord receipt batches (HERMES-FORK-018)."""
import asyncio
import json
import time as real_time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.waiting import async_wait_until
from tests.gateway.test_discord_receipt_reactions import Board, _adapter, _event, _owner


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_kind', ['approval', 'consumer', 'unauthorized'])
async def test_nonadmitted_receipt_releases_route_and_display(monkeypatch, tmp_path, exit_kind):
    from gateway.run_turn_bubble_cleanup import settle_detached_deletes
    from gateway.session_ingress_burst import wait_discord_receipt_predecessors
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    adapter.delete_message = AsyncMock(return_value=True)
    event = _event(adapter, board, tmp_path, 21001, 'yes')
    key = adapter._source_session_key(event.source)
    if exit_kind == 'unauthorized':
        monkeypatch.setattr(runner, '_is_user_authorized_for_source', lambda *a, **k: False)
    else:
        adapter._active_sessions[key] = asyncio.Event()
        monkeypatch.setattr(adapter, '_heal_stale_session_lock', lambda *a: None)
        monkeypatch.setattr(runner, '_is_session_running', lambda *a: True)
        monkeypatch.setattr(runner, '_route_plaintext_approval_while_busy', AsyncMock(return_value=exit_kind == 'approval'))
        if exit_kind == 'consumer':
            monkeypatch.setattr(runner, '_hm_post_admission_consume', AsyncMock(return_value=(True, None)))

    import gateway.session_ingress_burst as bursts
    from tests.gateway.test_discord_free_response import FakeTextChannel, make_message
    for name in ('DISCORD_REQUIRE_MENTION', 'DISCORD_AUTO_THREAD', 'DISCORD_HISTORY_BACKFILL'):
        monkeypatch.setenv(name, 'false')
    message = make_message(channel=FakeTextChannel(channel_id='room'), content='yes')
    message.id = int(event.message_id)
    message.add_reaction = event.raw_message.add_reaction
    message.remove_reaction = event.raw_message.remove_reaction
    predecessor_wait = AsyncMock(wraps=bursts.wait_discord_receipt_predecessors)
    monkeypatch.setattr(bursts, 'wait_discord_receipt_predecessors', predecessor_wait)
    await adapter._handle_message(message)
    predecessor_wait.assert_awaited_once()
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
    await adapter.flush_reactions()
    await settle_detached_deletes()
    assert event.message_id not in adapter._discord_preparing
    assert event.message_id not in adapter._discord_batch_by_message
    assert event.message_id not in adapter._discord_progress_by_message
    assert board.shown[21001] == set()
    adapter.delete_message.assert_awaited_once()
    following = _event(adapter, board, tmp_path, 21002, 'next')
    adapter._acknowledge_receipt(following.raw_message, following.source)
    assert await asyncio.wait_for(wait_discord_receipt_predecessors(adapter, following), 2)
    adapter.on_messages_dropped(following.source, following.message_ids())
    await adapter.flush_reactions()
    await settle_detached_deletes()
    adapter._active_sessions.clear()
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_prepared", [True, False])
async def test_expired_queued_batch_defers_successor_window(monkeypatch, tmp_path, first_prepared):
    import gateway.session_ingress_burst as bursts
    from gateway.session_results import execution_result
    clock = [real_time.time()]
    monkeypatch.setattr(bursts, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=real_time.monotonic))
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    schedule = authority._schedule
    monkeypatch.setattr(authority, '_schedule', lambda *a: None)
    first = _event(adapter, board, tmp_path, 22001, 'first')
    adapter._acknowledge_receipt(first.raw_message, first.source)
    if first_prepared:
        receipt = await authority.admit_native(first)
    clock[0] += 31
    if first_prepared:
        adapter.__dict__.pop("_discord_batches")  # queued work remains authoritative without a live batch cache
    second = _event(adapter, board, tmp_path, 22002, 'second')
    adapter._acknowledge_receipt(second.raw_message, second.source)
    if not first_prepared:
        receipt = await authority.admit_native(first)
    await authority.admit_native(second)
    entered, finish = asyncio.Event(), asyncio.Event()
    seen = []
    async def turn(event, *args):
        seen.append(event.message_id)
        if event.message_id == first.message_id:
            entered.set()
            await finish.wait()
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(authority, '_schedule', schedule)
    schedule(receipt.ref)
    await asyncio.wait_for(entered.wait(), 10)
    clock[0] += 100
    finish.set()
    def window():
        with authority.db._read_ctx() as conn:
            row = conn.execute('SELECT value FROM state_meta WHERE key=?',
                ('discord-batch:' + receipt.ref.session_id + ':22002',)).fetchone()
        return json.loads(row[0]) if row else None
    state = await async_wait_until(window, timeout=10)
    assert state['deadline'] == clock[0] + 30
    assert seen == ['22001']
    clock[0] += 30
    await async_wait_until(lambda: seen == ['22001', '22002'], timeout=10)
    await async_wait_until(lambda: all(not live.task or live.task.done() for live in authority.sessions.values()), timeout=10)
    await adapter.flush_reactions()
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform_name', ['telegram', 'discord'])
async def test_busy_delivery_identity_changes_only_for_discord(monkeypatch, tmp_path, platform_name):
    import gateway.session_ingress as ingress
    from gateway.config import Platform
    board = Board()
    adapter = _adapter(board)
    event = _event(adapter, board, tmp_path, 23001, 'first')
    event.source.platform = Platform(platform_name)
    sent = []
    async def handler(event):
        event.message_id = '23002'
        event.absorbed_message_ids = ['23001', '23002']
        ingress.busy_acceptance.get().set_result(None)
        ingress.busy_commit.get().set_result(None)
        return 'ok'
    async def deliver(adapter, event, *args):
        sent.append((event.message_id, event.message_ids()))
        return True
    adapter.set_message_handler(handler)
    monkeypatch.setattr(ingress, 'deliver_response', deliver)
    await ingress.dispatch_shared_busy(adapter, event, adapter._source_session_key(event.source), durable=True)
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
    expected = ('23002', ['23001', '23002']) if platform_name == 'discord' else ('23001', ['23001'])
    assert sent == [expected]


@pytest.mark.asyncio
async def test_receipt_failure_does_not_prevent_dispatch(monkeypatch, tmp_path):
    import gateway.session_ingress_burst as bursts
    board = Board()
    adapter = _adapter(board)
    event = _event(adapter, board, tmp_path, 24001, 'hello')
    seen = []
    def broken(*args):
        raise RuntimeError('receipt bookkeeping unavailable')
    monkeypatch.setattr(bursts, 'open_discord_batch', broken)
    async def dispatch(message, *args, **kwargs):
        adapter._acknowledge_receipt(message, event.source)
        seen.append(message.id)
        return True
    monkeypatch.setattr(adapter, '_handle_message_dispatch', dispatch)
    assert await adapter._handle_message(event.raw_message)
    assert seen == [24001]


@pytest.mark.asyncio
async def test_collecting_receipts_keep_their_batch_identity(monkeypatch, tmp_path):
    import gateway.session_ingress_burst as bursts
    from gateway.session_ingress_burst import open_discord_batch, discord_batch_descriptor
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    monkeypatch.setattr(bursts, "_save_discord_progress", lambda *args: None)
    event = _event(adapter, board, tmp_path, 25001, 'first')
    batch, _ = open_discord_batch(adapter, event.source, event.message_id)
    for mid in range(25002, 26027):
        open_discord_batch(adapter, event.source, str(mid))
    descriptor = discord_batch_descriptor(adapter, event)
    assert descriptor is not None and descriptor['id'] == batch['id']
    assert adapter._discord_progress_by_message[event.message_id] is batch['progress']
    await batch['progress']._start_task
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_standalone_receipt_does_not_post_unowned_working_message(tmp_path):
    board = Board()
    adapter = _adapter(board)
    adapter.send = AsyncMock()
    event = _event(adapter, board, tmp_path, 27001, 'hello')
    adapter._acknowledge_receipt(event.raw_message, event.source)
    await adapter.flush_reactions()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_retained_breadcrumb_does_not_pin_finished_batch_mappings(monkeypatch, tmp_path):
    import gateway.session_ingress_burst as bursts
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    monkeypatch.setattr(bursts, '_save_discord_progress', lambda *args: None)
    adapter.delete_message = AsyncMock(return_value=True)
    event = _event(adapter, board, tmp_path, 28001, 'first')
    batch, _ = bursts.open_discord_batch(adapter, event.source, event.message_id)
    await batch['progress']._start_task
    breadcrumb = batch['progress']
    retained_id = breadcrumb.message_id
    batch['closed'] = True  # the collection window ended; the failed turn retained its open breadcrumb
    for mid in range(28002, 29027):
        live, _ = bursts.open_discord_batch(adapter, event.source, str(mid))
    assert event.message_id not in adapter._discord_progress_by_message
    assert event.message_id not in adapter._discord_batch_by_message
    assert adapter._discord_progress_by_message[str(mid)] is live['progress']
    assert breadcrumb.message_id == retained_id and not breadcrumb.closed
    adapter.delete_message.assert_not_awaited()
    await live['progress']._start_task
    await asyncio.to_thread(authority.db.close)
