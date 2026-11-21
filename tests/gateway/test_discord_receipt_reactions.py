"""Discord fixed-window batches through real adapters, runner and SQLite authority.

Receipts show waiting plus permanent positions; merged turns keep every message ID.
Only the agent and Discord transport are stand-ins.
"""
import asyncio
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
WAITING = '⏳'


class Board:
    """What each Discord message currently shows, and every call made to get there."""

    def __init__(self):
        self.shown: dict[int, set[str]] = {}
        self.calls: list[tuple[int, str, str]] = []

    def message(self, mid):
        return FakeMessage(self, int(mid))

    def marks(self, ids):
        return {int(mid): self.shown.get(int(mid), set()) for mid in ids}


class FakeMessage:
    def __init__(self, board, mid):
        self.id, self._board = mid, board
        board.shown.setdefault(mid, set())

    def __repr__(self):
        return f'DiscordMessage(id={self.id})'

    async def add_reaction(self, emoji):
        self._board.calls.append((self.id, 'add', emoji))
        self._board.shown[self.id].add(emoji)

    async def remove_reaction(self, emoji, user):
        self._board.calls.append((self.id, 'remove', emoji))
        self._board.shown[self.id].discard(emoji)


def _adapter(board, **extra):
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False,
                                            extra={'group_sessions_per_user': False, **extra}))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=1))
    from gateway.platforms.base import SendResult
    async def send(*args, **kwargs):
        return SendResult(success=True, message_id='progress')
    adapter.send = send

    async def resolve(_channel_id):
        return SimpleNamespace(get_partial_message=board.message)
    adapter._resolve_channel = resolve
    return adapter


async def _owner(monkeypatch, tmp_path, board, **extra):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(group_sessions_per_user=False))
    adapter = _adapter(board, **extra)

    async def send(chat_id, content, reply_to=None, metadata=None):
        adapter.replies.append(reply_to)
        return SendResult(success=True, message_id='sent')
    adapter.replies = []
    monkeypatch.setattr(adapter, 'send', send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter, busy_text_mode='queue')
    return runner, adapter, authority


def _event(adapter, board, tmp_path, mid, text='', photo=False):
    from gateway.platforms.event import MessageEvent, MessageType
    source = adapter.build_source(chat_id='room', chat_type='group', user_id='42')
    raw = board.message(mid)
    if not photo:
        return MessageEvent(text=text, source=source, message_id=str(mid), raw_message=raw)
    path = tmp_path / f'p{mid}.png'
    path.write_bytes(PNG + bytes([mid % 251]))
    return MessageEvent(text=text, source=source, message_id=str(mid), raw_message=raw,
                        message_type=MessageType.PHOTO, media_urls=[str(path)], media_types=['image/png'])


async def _receive(adapter, event):
    """What Discord's ``_handle_message`` does for an accepted message, then dispatch."""
    adapter._acknowledge_receipt(event.raw_message, event.source)
    adapter._bind_receipt(event.raw_message, event)
    adapter._receipt_dispatched(event.raw_message)
    return asyncio.create_task(adapter.handle_message(event))



@pytest.mark.asyncio
async def test_fixed_window_mixed_media_is_one_durable_turn(monkeypatch, tmp_path):
    import time as real_time
    import gateway.session_ingress_burst as bursts
    from gateway.session_results import execution_result
    from hermes_state_runtime import pending_native_admissions
    clock = [real_time.time()]
    monkeypatch.setattr(bursts, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=real_time.monotonic))
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    seen, started = [], []
    monkeypatch.setattr(adapter, '_record_discord_processing_start',
                        lambda event, **kwargs: started.append(event.message_ids()))
    async def turn(event, *args):
        await adapter.flush_reactions()
        from gateway.run_turn_runner import TurnRunner
        ctx = SimpleNamespace(progress_compositor=None, session_key=runner.session_store._generate_session_key(event.source),
                              run_generation=args[-1], source=event.source, event_message_id=event.message_id,
                              _cleanup_progress=False)
        turn_runner = TurnRunner.__new__(TurnRunner)
        turn_runner._ctx, turn_runner._runner = ctx, runner
        inherited = turn_runner.start_progress_compositor(adapter)
        assert inherited is adapter._discord_progress_by_message[event.message_id]
        await inherited._start_task
        assert len(adapter.replies) == 1, 'the turn reuses the receipt Working message'
        seen.append((event.text, event.message_ids(), list(event.media_urls)))
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    first = _event(adapter, board, tmp_path, 1001, 'first')
    await _receive(adapter, first)
    await async_wait_until(lambda: len(pending_native_admissions(authority.db)) == 1, timeout=10)
    clock[0] += 29
    second = _event(adapter, board, tmp_path, 1002, 'caption', photo=True)
    await _receive(adapter, second)
    await async_wait_until(lambda: len(pending_native_admissions(authority.db)) == 2, timeout=10)
    await adapter.flush_reactions()
    assert board.marks([1001, 1002]) == {1001: {'⏳', '1️⃣'}, 1002: {'⏳', '2️⃣'}}
    assert not seen and not started
    assert adapter.replies == ['1001'], 'one Working message is sent as soon as the batch opens'
    clock[0] += 1
    await async_wait_until(lambda: bool(seen), timeout=10)
    assert seen[0][:2] == ('first\n\ncaption', ['1001', '1002'])
    assert len(seen[0][2]) == 1
    assert started == [['1001', '1002']], 'the executing batch records every ID once'
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
    assert board.marks([1001, 1002]) == {1001: {'1️⃣'}, 1002: {'2️⃣'}}
    assert adapter.replies[-1] == '1002'
    replay = _event(adapter, board, tmp_path, 1001, 'first')
    await adapter.handle_message(replay)
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
    assert replay._gateway_accepted is True, 'the original fingerprint still replays after batching'
    assert len(seen) == 1, 'a replay never executes the merged turn twice'
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_busy_batch_clock_begins_after_turn_and_survives_memory_loss(monkeypatch, tmp_path):
    import time as real_time
    import gateway.session_ingress_burst as bursts
    from gateway.session_results import execution_result
    from hermes_state_runtime import pending_native_admissions
    clock = [real_time.time()]
    monkeypatch.setattr(bursts, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=real_time.monotonic))
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    entered, finish = asyncio.Event(), asyncio.Event()
    seen = []
    async def turn(event, *args):
        seen.append(event.message_ids())
        if len(seen) == 1:
            entered.set()
            await finish.wait()
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    first = _event(adapter, board, tmp_path, 2001, 'A')
    await _receive(adapter, first)
    await async_wait_until(lambda: len(pending_native_admissions(authority.db)) == 1, timeout=10)
    clock[0] += 30
    await asyncio.wait_for(entered.wait(), 10)
    async def must_not_preempt(*args):
        raise AssertionError('a fixed batch must not steer or interrupt the running turn')
    monkeypatch.setattr(runner, '_hm_busy_preempt', must_not_preempt)
    for mid in (2002, 2003):
        await _receive(adapter, _event(adapter, board, tmp_path, mid, 'next'))
    await async_wait_until(lambda: len(pending_native_admissions(authority.db)) == 3, timeout=10)
    await adapter.flush_reactions()
    assert board.marks([2002, 2003]) == {2002: {'⏳', '1️⃣'}, 2003: {'⏳', '2️⃣'}}
    adapter.__dict__.pop('_discord_batches')  # restored owner has only committed envelopes
    adapter._receipt_marks().clear()
    clock[0] += 100
    finish.set()
    def timer_started():
        with authority.db._read_ctx() as conn:
            return conn.execute("SELECT value FROM state_meta WHERE key LIKE 'discord-batch:%:2002'").fetchone()
    await async_wait_until(timer_started, timeout=10)
    assert len(seen) == 1
    # Rebuild the authority from SQLite, with no adapter batch state or live waiters.
    from gateway.session_authority import initialize_session_authority
    bindings = [(sid, live.source, adapter) for sid, live in authority.sessions.items()]
    drains = [live.task for live in authority.sessions.values() if live.task and not live.task.done()]
    for task in drains:
        task.cancel()
    await asyncio.gather(*drains, return_exceptions=True)
    for task in list(adapter._background_tasks):
        task.cancel()
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    adapter.__dict__.pop('_discord_batch_by_message', None)
    adapter.__dict__.pop('_discord_progress_by_message', None)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='restarted')
    assert set((await authority.recover_native_sessions(bindings)).values()) == {'ready'}
    await _receive(adapter, _event(adapter, board, tmp_path, 2004, 'after restart'))
    await async_wait_until(lambda: len(pending_native_admissions(authority.db)) == 3, timeout=10)
    await adapter.flush_reactions()
    assert board.shown[2004] == {'⏳', '3️⃣'}, 'a new receipt joins the saved window without restarting it'
    clock[0] += 30
    await async_wait_until(lambda: len(seen) == 2, timeout=10)
    assert seen[1] == ['2002', '2003', '2004']
    await async_wait_until(lambda: not any(live.task and not live.task.done() for live in authority.sessions.values()), timeout=10)
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
    assert board.marks([2002, 2003]) == {2002: {'1️⃣'}, 2003: {'2️⃣'}}
    with authority.db._read_ctx() as conn:
        left = conn.execute("SELECT key FROM state_meta WHERE key LIKE 'discord-batch:%' "
                            "OR key LIKE 'discord-progress:%'").fetchall()
    assert not left, 'a claimed batch leaves no per-batch state behind'
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_receipt_numbers_stop_at_ten_and_drops_clear_both(tmp_path, caplog):
    from gateway.platforms.base import SendResult
    board = Board()
    adapter = _adapter(board)
    async def send(*args, **kwargs):
        return SendResult(success=True, message_id='sent')
    adapter.send = send
    import logging
    with caplog.at_level(logging.INFO, logger='plugins.platforms.discord.adapter_reactions'):
        for mid in range(1, 12):
            event = _event(adapter, board, tmp_path, mid, 'x')
            adapter._acknowledge_receipt(event.raw_message, event.source)
        await adapter.flush_reactions()
    acknowledgements = [record for record in caplog.records if 'receipt ack ⏳' in record.getMessage()]
    assert len(acknowledgements) == len(board.shown), 'one latency INFO line per waiting acknowledgement'
    assert board.shown[1] == {'⏳', '1️⃣'}
    assert board.shown[10] == {'⏳', '🔟'}
    assert board.shown[11] == {'⏳'}
    adapter.on_messages_dropped(event.source, [str(mid) for mid in range(1, 12)])
    await adapter.flush_reactions()
    assert not any(board.shown.values())


@pytest.mark.asyncio
async def test_reactions_setting_does_not_disable_batching(monkeypatch, tmp_path):
    import os
    from pathlib import Path
    from gateway.config import load_gateway_config, Platform
    from gateway.platforms.base import SendResult
    home = Path(os.environ['HERMES_HOME'])
    (home / 'config.yaml').write_text('discord:\n  batch_window_seconds: 12\n  reactions: false\n', encoding='utf-8')
    monkeypatch.setenv('DISCORD_BOT_TOKEN', 'fixture-token')
    # Register restoration even when the bridge creates a previously absent env value.
    monkeypatch.setenv('DISCORD_REACTIONS', 'true')
    monkeypatch.delenv('DISCORD_REACTIONS')
    config = load_gateway_config()
    assert config.platforms[Platform.DISCORD].extra['batch_window_seconds'] == 12
    board = Board()
    adapter = _adapter(board, **config.platforms[Platform.DISCORD].extra)
    async def send(*args, **kwargs):
        return SendResult(success=True, message_id='sent')
    adapter.send = send
    event = _event(adapter, board, tmp_path, 7001, 'x')
    adapter._acknowledge_receipt(event.raw_message, event.source)
    adapter.on_messages_joined_turn(event.source, event.message_ids())
    await adapter.flush_reactions()
    assert board.calls == []
    batch = adapter._discord_batches[adapter._source_session_key(event.source)]
    assert batch['ids'] == ['7001'] and batch['seconds'] == 12


def test_merged_ids_are_ordered_deduped_and_not_shared():
    from gateway.platforms.event import MessageEvent
    from gateway.run_inbound import discord_triggering_note, event_triggering_note

    head, second, third = (MessageEvent(text=t, message_id=m) for t, m in (('a', '1'), ('b', '2'), ('c', '3')))
    assert head.message_ids() == ['1'] and head.absorbed_message_ids == []
    assert event_triggering_note(head) == discord_triggering_note('1')  # a single message: bytes unchanged
    head.absorb_turn_input(second)
    copy = head.absorbed_message_ids
    head.absorb_turn_input(third)
    head.absorb_turn_input(second)  # a re-merge adds nothing
    assert head.message_ids() == ['1', '2', '3'] and copy == ['1', '2']
    assert '(merged 3 messages)' in event_triggering_note(head) and '`3`' in event_triggering_note(head)


def _busy_ack(adapter, board, tmp_path, mid, text='x'):
    """A message received while its conversation is busy: ⏳ on receipt, event bound to its adapter."""
    event = _event(adapter, board, tmp_path, mid, text)
    adapter._active_sessions[adapter._event_session_key(event)] = asyncio.Event()
    adapter._acknowledge_receipt(event.raw_message, event.source)
    adapter._bind_receipt(event.raw_message, event)
    adapter._receipt_dispatched(event.raw_message)
    return event


@pytest.mark.asyncio
async def test_non_durable_drops_clear_the_waiting_mark(tmp_path):
    from gateway.platforms.base import TextDebounceState, merge_pending_message_event

    board = Board()
    adapter = _adapter(board)
    first, second, third, fourth = (_busy_ack(adapter, board, tmp_path, mid) for mid in (8001, 8002, 8003, 8004))
    key = adapter._event_session_key(first)
    merge_pending_message_event(adapter._pending_messages, key, first)
    merge_pending_message_event(adapter._pending_messages, key, second)  # replaces the slot's input
    await adapter.flush_reactions()
    assert board.marks([8001, 8002]) == {8001: set(), 8002: {WAITING, '2️⃣'}}
    adapter._text_debounce_store()[key] = TextDebounceState(third, None, 0.0, 0.0)
    adapter._discard_text_debounce(key)  # a control command drops the debounce buffer
    await adapter.flush_reactions()
    assert board.marks([8003]) == {8003: set()}
    await adapter.cancel_session_processing(key)  # /stop drops the pending slot
    await adapter.flush_reactions()
    assert board.marks([8002, 8004]) == {8002: set(), 8004: {WAITING, '4️⃣'}}, 'only what was dropped is cleared'


@pytest.mark.asyncio
async def test_message_never_dispatched_loses_its_receipt_mark(tmp_path):
    board = Board()
    adapter = _adapter(board)
    gone, sent = (_event(adapter, board, tmp_path, mid) for mid in (8101, 8102))

    async def early_exit():
        adapter._acknowledge_receipt(gone.raw_message, gone.source)
        return False  # e.g. a mention-only ping or a failed attachment download

    async def dispatched():
        adapter._acknowledge_receipt(sent.raw_message, sent.source)
        adapter._receipt_dispatched(sent.raw_message)
        return True

    async def failing():
        adapter._acknowledge_receipt(gone.raw_message, gone.source)
        raise RuntimeError('attachment download failed')

    await adapter._guard_receipt(gone.raw_message, early_exit)
    await adapter._guard_receipt(sent.raw_message, dispatched)
    await adapter.flush_reactions()
    assert board.marks([8101, 8102]) == {8101: set(), 8102: {WAITING, '1️⃣'}}
    with pytest.raises(RuntimeError):
        await adapter._guard_receipt(gone.raw_message, failing)
    await adapter.flush_reactions()
    assert board.shown[8101] == set()


@pytest.mark.asyncio
async def test_shutdown_lets_the_final_reaction_finish(tmp_path):
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)

    class Slow(FakeMessage):
        async def add_reaction(self, emoji):
            await asyncio.sleep(0.2)
            await super().add_reaction(emoji)

    event = _event(adapter, board, tmp_path, 8201)
    event.raw_message = Slow(board, 8201)
    adapter._acknowledge_receipt(event.raw_message, event.source)
    await adapter.flush_reactions()
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await adapter.cancel_background_tasks()  # graceful restart
    assert board.shown[8201] == {WAITING, '1️⃣'}


@pytest.mark.asyncio
async def test_unrecorded_waiting_mark_is_removed_and_drops_read_no_config(tmp_path):
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)
    stuck = _busy_ack(adapter, board, tmp_path, 8301)
    adapter._receipt_marks().clear()  # evicted past the cap, or a timed-out call: the ⏳ still shows
    await adapter.flush_reactions()
    assert board.shown[8301] == {WAITING, '1️⃣'}
    await adapter.on_processing_start(stuck)
    await adapter.on_processing_complete(stuck, ProcessingOutcome.SUCCESS)
    await adapter.flush_reactions()
    assert board.shown[8301] == {'1️⃣'}
    held = _busy_ack(adapter, board, tmp_path, 8302)
    adapter.config.extra['reactions'] = False  # a drop path outside the profile scope sees other config
    adapter.on_messages_dropped(held.source, ['8302'])
    await adapter.flush_reactions()
    assert board.shown[8302] == set()


@pytest.mark.asyncio
async def test_message_folded_into_a_running_turn_switches_to_working(tmp_path):
    from gateway.receipt_drops import join_event

    board = Board()
    adapter = _adapter(board)
    steered = _busy_ack(adapter, board, tmp_path, 8401)
    await adapter.flush_reactions()
    assert board.shown[8401] == {WAITING, '1️⃣'}
    join_event(steered)
    await adapter.flush_reactions()
    assert board.shown[8401] == {'1️⃣'}


@pytest.mark.asyncio
async def test_dropped_auto_thread_starter_loses_its_mark_in_the_parent_channel(tmp_path):
    """The starter lives in the parent channel; clearing its mark must not look for it inside the thread."""
    board = Board()
    adapter = _adapter(board)

    class Missing:  # a message id looked up in the wrong channel: Discord answers 404
        async def remove_reaction(self, *_):
            raise LookupError

        add_reaction = remove_reaction

    async def resolve(channel_id):
        found = board.message if channel_id == 'parent' else (lambda _mid: Missing())
        return SimpleNamespace(get_partial_message=found)
    adapter._resolve_channel = resolve
    source = adapter.build_source(chat_id='thread', parent_chat_id='parent', chat_type='thread',
                                  user_id='42', auto_thread_created=True)
    starter = board.message(9001)
    adapter._active_sessions[adapter._source_session_key(source)] = asyncio.Event()
    adapter._acknowledge_receipt(starter, source)
    await adapter.flush_reactions()
    assert board.shown[9001] == {WAITING, '1️⃣'}
    adapter.on_messages_dropped(source, ['9001'])
    await adapter.flush_reactions()
    assert board.shown[9001] == set()


@pytest.mark.asyncio
async def test_merged_event_reacts_to_each_of_its_own_messages(tmp_path):
    """A merged event keeps the head's ``raw_message`` while ``message_id`` is the latest: every carried
    message keeps its own permanent batch position, never the head twice."""
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)
    head, tail = (_busy_ack(adapter, board, tmp_path, mid) for mid in (9101, 9102))
    head.absorb_turn_input(tail)
    head.message_id = '9102'  # the debounce merge path keeps the head's raw_message and moves the id
    await adapter.flush_reactions()
    await adapter.on_processing_start(head)
    await adapter.flush_reactions()
    assert board.marks([9101, 9102]) == {9101: {'1️⃣'}, 9102: {'2️⃣'}}
    await adapter.on_processing_complete(head, ProcessingOutcome.SUCCESS)
    await adapter.flush_reactions()
    assert board.marks([9101, 9102]) == {9101: {'1️⃣'}, 9102: {'2️⃣'}}
    assert [c for c in board.calls if c[:2] == (9101, 'add')] == [(9101, 'add', WAITING), (9101, 'add', '1️⃣')]


@pytest.mark.asyncio
async def test_preparing_receipts_preserve_order_and_stop_fence(monkeypatch, tmp_path):
    from gateway.session_ingress_burst import wait_discord_receipt_predecessors, discord_receipt_committed
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    first, second = [_event(adapter, board, tmp_path, mid, 'x') for mid in (9201, 9202)]
    adapter._acknowledge_receipt(first.raw_message, first.source)
    adapter._acknowledge_receipt(second.raw_message, second.source)
    waiting = asyncio.create_task(wait_discord_receipt_predecessors(adapter, second))
    await asyncio.sleep(0)
    assert not waiting.done(), 'a later download cannot overtake its earlier receipt'
    key = adapter._source_session_key(first.source)
    await runner._hm_stop_owner_turn(key)
    discord_receipt_committed(adapter, first.message_id)
    assert await asyncio.wait_for(waiting, 10) is False, 'stop fences still-preparing receipts'
    adapter.on_messages_dropped(first.source, ['9201', '9202'])
    await adapter.flush_reactions()
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_stop_drops_a_durable_collecting_batch(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from hermes_state_runtime import pending_native_admissions
    from gateway.run_turn_bubble_cleanup import settle_detached_deletes
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    adapter.delete_message = AsyncMock(return_value=True)
    first = _event(adapter, board, tmp_path, 9301, 'x')
    await _receive(adapter, first)
    await _receive(adapter, _event(adapter, board, tmp_path, 9302, 'y'))
    await async_wait_until(lambda: len(pending_native_admissions(authority.db)) == 2, timeout=10)
    await runner._hm_stop_owner_turn(adapter._source_session_key(first.source))
    await adapter.flush_reactions()
    await settle_detached_deletes()
    assert board.marks([9301, 9302]) == {9301: set(), 9302: set()}
    assert not pending_native_admissions(authority.db)
    adapter.delete_message.assert_awaited_once()
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_slow_or_failing_reactions_never_delay_the_turn(tmp_path):
    from gateway.platforms.event import ProcessingOutcome
    board = Board()
    adapter = _adapter(board)
    hang = asyncio.Event()
    class Hung(FakeMessage):
        async def add_reaction(self, emoji):
            await hang.wait()
    class Broken(FakeMessage):
        async def add_reaction(self, emoji):
            raise RuntimeError('429 rate limited')
    for cls, mid in ((Hung, 4001), (Broken, 4002)):
        event = _event(adapter, board, tmp_path, mid, 'x')
        event.raw_message = cls(board, mid)
        adapter._acknowledge_receipt(event.raw_message, event.source)
        await asyncio.wait_for(adapter.on_processing_start(event), 2)
        await asyncio.wait_for(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS), 2)
    hang.set()
    await asyncio.wait_for(adapter.flush_reactions(), 5)
    assert not adapter._background_tasks
