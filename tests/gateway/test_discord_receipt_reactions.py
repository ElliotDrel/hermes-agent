"""Discord numbers queued messages and a merged turn keeps every message's identity.

Behavior contract (HERMES-FORK-018): an idle message gets no reaction; a message that arrives while
its conversation is busy gets the next queue number and keeps it; work starting on a message (or a
drop) removes its number; the counter restarts at 1 once the queue is empty; there are no working or
outcome reactions. The reply anchors to the latest merged message; reaction calls are detached and
never delay the turn. Real GatewayRunner, DiscordAdapter, SessionAuthority and drain over
SQLite; the agent turn and Discord's message objects are stand-ins.
"""
import asyncio
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
N = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']


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
    return asyncio.create_task(adapter.handle_message(event))


IDS = list(range(2001, 2009))  # a caption image plus seven image messages


@pytest.mark.asyncio
async def test_busy_burst_shares_number_and_keeps_identity(monkeypatch, tmp_path):
    from gateway.platforms.base import _reply_anchor_for_event
    from gateway.run import GatewayRunner
    from gateway.session_envelope import restore_native
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    import gateway.session as gateway_session

    monkeypatch.setattr(gateway_session, '_discord_tools_loaded', lambda: True)
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    running, finish = asyncio.Event(), asyncio.Event()
    turns = []

    async def turn(event, source, quick_key, run_generation):
        await adapter.flush_reactions()  # the test reads the board; a turn never waits for it
        turns.append({
            'text': event.text, 'ids': event.message_ids(), 'marks': board.marks(event.message_ids()),
            'anchor': _reply_anchor_for_event(event),
            'note': GatewayRunner._prepend_inbound_reply_context(event, event.source, 'body').split('\n')[0]})
        if event.text == 'A':
            running.set()
            await finish.wait()
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)

    first = _event(adapter, board, tmp_path, 1000, 'A')
    await (await _receive(adapter, first))
    await asyncio.wait_for(running.wait(), 10)
    assert board.shown[1000] == set(), 'an idle message runs at once with no reaction'

    burst = [_event(adapter, board, tmp_path, IDS[0], 'caption', photo=True)]
    burst += [_event(adapter, board, tmp_path, mid, photo=True) for mid in IDS[1:]]
    callers = [await _receive(adapter, e) for e in burst]
    await adapter.flush_reactions()
    assert board.marks(IDS) == {mid: {'\u23f3', N[0]} for mid in IDS}, 'one open batch shares its number and hourglass'
    await asyncio.wait_for(asyncio.gather(*callers), 10)
    await adapter.flush_reactions()
    assert board.marks(IDS) == {mid: {N[0]} for mid in IDS}

    rows = list_session_admissions(authority.db, session_id=authority.logical_owner(
        runner.session_store.peek_session_id(runner.session_store._generate_session_key(first.source))),
        pending_only=False)
    assert len(rows) == 2, 'the whole burst is one queued turn'
    assert 'absorbed_message_ids' not in rows[0]['payload']['native_text_v1']['event']  # old shape: no field
    assert restore_native(rows[0]['payload'], runner).message_ids() == ['1000']
    assert rows[1]['payload']['native_text_v1']['event']['absorbed_message_ids'] == [str(m) for m in IDS]
    assert restore_native(rows[1]['payload'], runner).absorbed_message_ids == [str(m) for m in IDS]

    finish.set()
    await async_wait_until(lambda: len(turns) == 2, timeout=10, what='merged turn executed')
    merged = turns[1]
    assert merged['ids'] == [str(m) for m in IDS]
    assert merged['marks'] == {mid: set() for mid in IDS}, 'work starting removes every number'
    assert merged['anchor'] == str(IDS[-1]), 'the reply anchors to the latest message'
    assert merged['note'].startswith(f'[Triggering message id: `{IDS[-1]}` (merged 8 messages)')
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what='turn closed')
    await adapter.flush_reactions()
    assert board.marks(IDS + [1000]) == {mid: set() for mid in IDS + [1000]}, 'no outcome reactions'
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_idle_message_makes_no_reaction_calls(monkeypatch, tmp_path):
    from gateway.platforms.base import SendResult
    from gateway.session import build_session_key

    board = Board()
    adapter = _adapter(board)
    adapter.send = lambda *a, **k: asyncio.sleep(0, SendResult(success=True, message_id='1'))

    async def handler(_event):
        return 'ack'
    adapter.set_message_handler(handler)
    event = _event(adapter, board, tmp_path, 3001, 'hello')
    adapter._acknowledge_receipt(event.raw_message, event.source)
    await adapter._process_message_background(event, build_session_key(event.source))
    await adapter.flush_reactions()
    assert board.calls == []


@pytest.mark.asyncio
async def test_slow_or_failing_reactions_never_delay_the_turn(tmp_path):
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)
    adapter._RECEIPT_OP_TIMEOUT = 0.2
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
        adapter._active_sessions[adapter._event_session_key(event)] = asyncio.Event()
        adapter._acknowledge_receipt(event.raw_message, event.source)  # returns at once
        await asyncio.wait_for(adapter.on_processing_start(event), 0.1)
        await asyncio.wait_for(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS), 0.1)
    await asyncio.wait_for(adapter.flush_reactions(), 5)  # bounded: every call gave up or failed quietly
    assert not adapter._background_tasks


@pytest.mark.asyncio
async def test_stopped_and_discarded_messages_lose_their_waiting_mark(monkeypatch, tmp_path):
    from gateway.session_ingress_burst import discard_busy_burst, holds_busy_burst
    from gateway.session_results import execution_result

    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board)
    running, finish = asyncio.Event(), asyncio.Event()

    async def turn(event, source, quick_key, run_generation):
        if event.text == 'A':
            running.set()
            await finish.wait()
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    first = _event(adapter, board, tmp_path, 1000, 'A')
    await (await _receive(adapter, first))
    await asyncio.wait_for(running.wait(), 10)
    key = adapter._event_session_key(first)

    held = [_event(adapter, board, tmp_path, mid, 'more') for mid in (5001, 5002)]
    for e in held:
        await _receive(adapter, e)
    await async_wait_until(lambda: holds_busy_burst(adapter, key), timeout=5, what='burst held')
    await adapter.flush_reactions()
    assert board.marks([5001, 5002]) == {5001: {'\u23f3', N[0]}, 5002: {'\u23f3', N[0]}}
    discard_busy_burst(adapter, key)  # /stop or /new while the burst is still held
    await adapter.flush_reactions()
    assert board.marks([5001, 5002]) == {5001: set(), 5002: set()}

    queued = [_event(adapter, board, tmp_path, mid, 'queued') for mid in (6001, 6002)]
    callers = [await _receive(adapter, e) for e in queued]
    await asyncio.wait_for(asyncio.gather(*callers), 10)  # committed behind the running turn
    await adapter.flush_reactions()
    assert board.marks([6001, 6002]) == {6001: {N[0]}, 6002: {N[0]}}, 'an emptied queue counts from 1 again'
    await runner._hm_stop_owner_turn(key)  # /stop cancels the committed queued rows
    await adapter.flush_reactions()
    assert board.marks([6001, 6002]) == {6001: set(), 6002: set()}
    finish.set()
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what='turn closed')
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_reactions_disabled_makes_no_reaction_calls(tmp_path):
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board, reactions=False)
    event = _event(adapter, board, tmp_path, 7001, 'x')
    adapter._acknowledge_receipt(event.raw_message, event.source)
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    adapter.on_messages_dropped(event.source, ['7001'])
    await adapter.flush_reactions()
    assert board.calls == []


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
    """A message received while its conversation is busy: numbered on receipt, bound to its adapter."""
    event = _event(adapter, board, tmp_path, mid, text)
    adapter._active_sessions[adapter._event_session_key(event)] = asyncio.Event()
    queue = adapter._queues().get(adapter._event_session_key(event))
    if queue and queue[2]:
        adapter.on_busy_burst_closed(event.source, list(queue[2][2]))
    adapter._acknowledge_receipt(event.raw_message, event.source)
    adapter.on_busy_burst_closed(event.source, event.message_ids())
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
    assert board.marks([8001, 8002]) == {8001: set(), 8002: {N[1]}}
    adapter._text_debounce_store()[key] = TextDebounceState(third, None, 0.0, 0.0)
    adapter._discard_text_debounce(key)  # a control command drops the debounce buffer
    await adapter.flush_reactions()
    assert board.marks([8003]) == {8003: set()}
    await adapter.cancel_session_processing(key)  # /stop drops the pending slot
    await adapter.flush_reactions()
    assert board.marks([8002, 8004]) == {8002: set(), 8004: {N[3]}}, 'only what was dropped is cleared'


@pytest.mark.asyncio
async def test_message_never_dispatched_loses_its_receipt_mark(tmp_path):
    board = Board()
    adapter = _adapter(board)
    gone, sent = (_event(adapter, board, tmp_path, mid) for mid in (8101, 8102))
    adapter._active_sessions[adapter._event_session_key(gone)] = asyncio.Event()

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
    assert board.marks([8101, 8102]) == {8101: set(), 8102: {'\u23f3', N[0]}}
    with pytest.raises(RuntimeError):
        await adapter._guard_receipt(gone.raw_message, failing)
    await adapter.flush_reactions()
    assert board.shown[8101] == set()


@pytest.mark.asyncio
async def test_shutdown_lets_a_pending_removal_finish(tmp_path):
    board = Board()
    adapter = _adapter(board)

    class Slow(FakeMessage):
        async def remove_reaction(self, emoji, user):
            await asyncio.sleep(0.2)
            await super().remove_reaction(emoji, user)

    event = _busy_ack(adapter, board, tmp_path, 8201)
    await adapter.flush_reactions()
    adapter._receipt_marks()['8201'][1] = Slow(board, 8201)
    await adapter.on_processing_start(event)
    await adapter.cancel_background_tasks()  # graceful restart
    assert board.shown[8201] == set()


@pytest.mark.asyncio
async def test_numbers_stay_put_and_restart_at_one_once_the_queue_empties(tmp_path):
    board = Board()
    adapter = _adapter(board)
    first, second, third = (_busy_ack(adapter, board, tmp_path, mid) for mid in (8301, 8302, 8303))
    adapter.on_messages_dropped(first.source, ['8302'])
    fourth = _busy_ack(adapter, board, tmp_path, 8304)
    await adapter.flush_reactions()
    assert board.marks([8301, 8302, 8303, 8304]) == {8301: {N[0]}, 8302: set(), 8303: {N[2]}, 8304: {N[3]}}
    for event in (first, third, fourth):
        await adapter.on_processing_start(event)
    again = _busy_ack(adapter, board, tmp_path, 8305)
    await adapter.flush_reactions()
    assert board.marks([8301, 8303, 8304, 8305]) == {8301: set(), 8303: set(), 8304: set(), 8305: {N[0]}}
    adapter.config.extra['reactions'] = False  # a drop path outside the profile scope sees other config
    adapter.on_messages_dropped(again.source, ['8305'])
    await adapter.flush_reactions()
    assert board.shown[8305] == set()
    adapter.config.extra['reactions'] = True
    numbered = [_busy_ack(adapter, board, tmp_path, 8500 + i) for i in range(10)]
    overflow = _event(adapter, board, tmp_path, 8601, 'overflow')
    adapter._acknowledge_receipt(overflow.raw_message, overflow.source)
    adapter.on_messages_dropped(first.source, [e.message_id for e in numbered])
    same_batch = _event(adapter, board, tmp_path, 8602, 'same batch')
    adapter._acknowledge_receipt(same_batch.raw_message, same_batch.source)
    await adapter.flush_reactions()
    assert board.marks([8601, 8602]) == {8601: {'\u23f3'}, 8602: {'\u23f3'}}
    adapter.on_busy_burst_closed(overflow.source, ['8601', '8602'])
    fresh = _busy_ack(adapter, board, tmp_path, 8603)
    await adapter.flush_reactions()
    assert board.shown[8603] == {N[0]}, 'a new batch resets after numbered messages are gone'



@pytest.mark.asyncio
async def test_message_folded_into_a_running_turn_loses_its_number(tmp_path):
    from gateway.receipt_drops import join_event

    board = Board()
    adapter = _adapter(board)
    steered = _busy_ack(adapter, board, tmp_path, 8401)
    await adapter.flush_reactions()
    assert board.shown[8401] == {N[0]}
    join_event(steered)
    await adapter.flush_reactions()
    assert board.shown[8401] == set()


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
    assert board.shown[9001] == {'\u23f3', N[0]}
    adapter.on_messages_dropped(source, ['9001'])
    await adapter.flush_reactions()
    assert board.shown[9001] == set()


@pytest.mark.asyncio
async def test_merged_event_reacts_to_each_of_its_own_messages(tmp_path):
    """A merged event keeps the head's ``raw_message`` while ``message_id`` is the latest: every carried
    message loses its own number, never the head's twice."""
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)
    head, tail = (_busy_ack(adapter, board, tmp_path, mid) for mid in (9101, 9102))
    head.absorb_turn_input(tail)
    head.message_id = '9102'  # the debounce merge path keeps the head's raw_message and moves the id
    await adapter.flush_reactions()
    await adapter.on_processing_start(head)
    await adapter.flush_reactions()
    assert board.marks([9101, 9102]) == {9101: set(), 9102: set()}
    assert [c for c in board.calls if c[0] == 9101] == [(9101, 'add', '\u23f3'), (9101, 'add', N[0]), (9101, 'remove', '\u23f3'), (9101, 'remove', N[0])]


@pytest.mark.asyncio
async def test_idle_receipt_compositor_is_shared_and_adopted_without_waiting(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from gateway.run_turn_progress import GatewayTurnProgressMixin

    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    board = Board()
    adapter = _adapter(board, reactions=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id='working')
    adapter.send = AsyncMock(side_effect=send)
    a, b = (_event(adapter, board, tmp_path, mid, 'chunk') for mid in (9201, 9202))
    adapter._acknowledge_receipt(a.raw_message, a.source)
    adapter._acknowledge_receipt(b.raw_message, b.source)
    await asyncio.wait_for(entered.wait(), 2)
    key = adapter._event_session_key(a)
    compositor = adapter._receipt_progress[key][0]
    ctx = SimpleNamespace(progress_compositor=None, session_key=key, run_generation=7,
                          source=a.source, inbound_message_ids=['9201', '9202'], _progress_reply_to='9202', _progress_metadata={},
                          _cleanup_progress=False)
    runner = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
        _session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(progress_compositor=None)),
        _delivery_adapter_for=lambda source: adapter), _track_compositor_id=lambda c: None)
    adopted = GatewayTurnProgressMixin.start_progress_compositor(runner)
    assert adopted is compositor and adopted.generation == 7
    assert adapter.send.await_count == 1 and not adopted._start_task.done()
    release.set()
    await adopted._start_task
    assert adopted.message_id == 'working' and not adapter._receipt_progress
    assert board.calls == []
    adapter._active_sessions[key] = asyncio.Event()
    busy = _event(adapter, board, tmp_path, 9203, 'busy')
    adapter._acknowledge_receipt(busy.raw_message, busy.source)
    assert adapter.send.await_count == 1 and not adapter._receipt_progress


@pytest.mark.asyncio
async def test_dropped_idle_receipt_deletes_a_late_working_send(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from gateway.run_turn_bubble_cleanup import settle_detached_deletes

    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    adapter = _adapter(Board())
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id='working')
    adapter.send = AsyncMock(side_effect=send)
    adapter.delete_message = AsyncMock(return_value=True)
    event = _event(adapter, Board(), tmp_path, 9301, 'drop')
    async def download_failure():
        adapter._acknowledge_receipt(event.raw_message, event.source)
        await entered.wait()
        raise ValueError('download failed')
    with pytest.raises(ValueError):
        await adapter._guard_receipt(event.raw_message, download_failure)
    release.set()
    await settle_detached_deletes()
    adapter.delete_message.assert_awaited_once_with('room', 'working')
    assert not adapter._receipt_progress


@pytest.mark.asyncio
async def test_receipts_reserve_one_burst_across_delayed_preparation(monkeypatch, tmp_path):
    import gateway.session_ingress_burst as bursts
    board = Board()
    adapter = _adapter(board)
    adapter._shared_runtime_owns_input = lambda event: True
    adapter._busy_text_debounce_seconds = 30
    adapter._busy_text_hard_cap_seconds = 3600
    clock = [0.0]
    monkeypatch.setattr(bursts, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    waits = []
    class TimerClock:
        def __getattr__(self, name):
            return getattr(asyncio, name)
        async def sleep(self, delay):
            wake = asyncio.Event()
            waits.append(wake)
            await wake.wait()
    monkeypatch.setattr(bursts, 'asyncio', TimerClock())
    committed = []
    async def admit(adapter, event, key, **kwargs):
        committed.append(event)
        event._gateway_accepted = True
    monkeypatch.setattr('gateway.session_ingress.dispatch_shared_busy', admit)
    events = [_event(adapter, board, tmp_path, mid, str(mid), photo=mid in (9401, 9402)) for mid in (9401, 9402, 9403, 9404)]
    expected_media = [path for event in events[:2] for path in event.media_urls]
    key = adapter._event_session_key(events[0])
    adapter._active_sessions[key] = asyncio.Event()
    callers = []
    try:
        adapter._acknowledge_receipt(events[0].raw_message, events[0].source)
        # The first receipt reserves the holder even before its download finishes.
        holder = adapter._shared_bursts[key]
        await asyncio.sleep(0)
        original_window = waits[-1]
        clock[0] = 29
        adapter._acknowledge_receipt(events[1].raw_message, events[1].source)
        assert adapter._shared_bursts[key] is holder and holder.last_ts == 29
        callers.append(asyncio.create_task(bursts.dispatch_busy_burst(adapter, events[0], key)))
        await asyncio.sleep(0)
        clock[0] = 30
        original_window.set()
        await asyncio.sleep(0)
        assert holder.commit is None, 'the receipt at 29s restarted the original 30s window'
        clock[0] = 31
        adapter._acknowledge_receipt(events[2].raw_message, events[2].source)
        callers.append(asyncio.create_task(bursts.dispatch_busy_burst(adapter, events[2], key)))
        await asyncio.sleep(0)
        await adapter.flush_reactions()
        assert board.marks([9401, 9402, 9403]) == {mid: {'\u23f3', N[0]} for mid in (9401, 9402, 9403)}
        clock[0] = 61
        waits[-1].set()
        await async_wait_until(lambda: holder.commit is not None, timeout=2, what='receipt window closed')
        assert not holder.commit.done() and not committed, 'preparing input prevents admission'
        callers.append(asyncio.create_task(bursts.dispatch_busy_burst(adapter, events[1], key)))
        await asyncio.wait_for(asyncio.gather(*callers), 2)
        assert [e.message_ids() for e in committed] == [['9401', '9402', '9403']]
        assert committed[0].text == '9401\n9402\n9403'
        assert committed[0].media_urls == expected_media
        adapter._acknowledge_receipt(events[3].raw_message, events[3].source)
        await adapter.flush_reactions()
        assert board.shown[9404] == {'\u23f3', N[1]}, 'only a new holder gets the next number'
        bursts.discard_busy_burst(adapter, key)
        assert adapter._bind_receipt(events[3].raw_message, events[3]) is False
        await adapter.flush_reactions()
        assert board.shown[9404] == set(), 'dropping a preparing receipt also clears its marks'
    finally:
        for task in callers:
            task.cancel()
        await asyncio.gather(*callers, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['turn', 'hygiene'])
async def test_unrelated_turn_cannot_adopt_a_preparing_inputs_working_message(monkeypatch, tmp_path, site):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from gateway.run_turn_progress import GatewayTurnProgressMixin
    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    adapter = _adapter(Board(), reactions=False)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='working'))
    event = _event(adapter, Board(), tmp_path, 9501, 'downloading')
    adapter._acknowledge_receipt(event.raw_message, event.source)
    key = adapter._event_session_key(event)
    owned = adapter._receipt_progress[key][0]
    ctx = SimpleNamespace(progress_compositor=None, session_key=key, run_generation=9,
                          source=event.source, inbound_message_ids=['9502'],
                          _progress_reply_to='9502', _progress_metadata={}, _cleanup_progress=False)
    runner = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
        _session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(progress_compositor=None)),
        _delivery_adapter_for=lambda source: adapter), _track_compositor_id=lambda c: None)
    def start():
        if site == 'turn':
            return GatewayTurnProgressMixin.start_progress_compositor(runner)
        from gateway.run_turn_hygiene import GatewayTurnHygieneMixin
        from gateway.platforms.event import MessageEvent
        runner._is_session_run_current = lambda *args: True
        attempt = SimpleNamespace(meta={})
        hs = SimpleNamespace(data={'display': {'progress_compositor': 'single_message'}})
        executing = MessageEvent(text='', source=event.source, message_id=ctx.inbound_message_ids[-1],
                                 absorbed_message_ids=list(ctx.inbound_message_ids))
        GatewayTurnHygieneMixin._hmwa_hygiene_start_progress(
            runner, attempt, hs, executing, event.source, key, 9)
        return attempt.progress_compositor
    runner._delivery_adapter_for = lambda source: adapter
    runner._session_state = runner._runner._session_state
    unrelated = start()
    await unrelated._start_task
    assert unrelated is not owned and adapter._receipt_progress[key][0] is owned
    # A merged idle batch may adopt using any one of its genuine input ids.
    ctx.progress_compositor = None
    ctx.inbound_message_ids = ['9500', '9501']
    adopted = start()
    await adopted._start_task
    assert adopted is owned and not adapter._receipt_progress


@pytest.mark.asyncio
@pytest.mark.parametrize('reactions', [True, False])
async def test_guard_releases_reservation_when_plaintext_becomes_command(tmp_path, reactions):
    from unittest.mock import AsyncMock
    from gateway.session_ingress_burst import _flush
    board = Board()
    adapter = _adapter(board, reactions=reactions)
    adapter.set_message_handler(AsyncMock(return_value=None))
    adapter._shared_runtime_owns_input = lambda event: not event.get_command()
    adapter._dispatch_inline_reply = AsyncMock()
    event = _event(adapter, board, tmp_path, 9601, 'restart gateway')
    event.source.chat_type = 'dm'
    key = adapter._event_session_key(event)
    adapter._active_sessions[key] = asyncio.Event()
    try:
        async def receive():
            adapter._acknowledge_receipt(event.raw_message, event.source)
            adapter._bind_receipt(event.raw_message, event)
            adapter._receipt_dispatched(event.raw_message)
            await adapter.handle_message(event)
        await adapter._guard_receipt(event.raw_message, receive)
        assert event.get_command() == 'restart'
        holder = adapter._shared_bursts[key]
        assert not holder.receipts and holder.prepared.is_set(), 'converted commands release their preparation hold'
        await asyncio.wait_for(_flush(adapter, key, holder), 2)
        await adapter.flush_reactions()
        assert board.shown[9601] == set()
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_empty_burst_keeps_its_predecessors_fifo_position(monkeypatch, tmp_path):
    import gateway.session_ingress_burst as bursts
    adapter = _adapter(Board(), reactions=False)
    events = [_event(adapter, Board(), tmp_path, 9700 + i, label) for i, label in enumerate(('A', 'B', 'C'))]
    key = adapter._event_session_key(events[0])
    entered, release = asyncio.Event(), asyncio.Event()
    admitted, callers = [], []
    async def admit(adapter, event, key, **kwargs):
        if event.text == 'A':
            entered.set()
            await release.wait()
        admitted.append(event.text)
        event._gateway_accepted = True
    monkeypatch.setattr('gateway.session_ingress.dispatch_shared_busy', admit)
    try:
        first = bursts.reserve_busy_receipt(adapter, events[0], key)
        callers.append(asyncio.create_task(bursts.dispatch_busy_burst(adapter, events[0], key)))
        bursts._flush(adapter, key, first)
        await asyncio.wait_for(entered.wait(), 2)
        empty = bursts.reserve_busy_receipt(adapter, events[1], key)
        bursts.drop_busy_receipts(adapter, events[1].message_ids())
        middle = bursts._flush(adapter, key, empty)
        await asyncio.wait({middle}, timeout=2)
        last = bursts.reserve_busy_receipt(adapter, events[2], key)
        callers.append(asyncio.create_task(bursts.dispatch_busy_burst(adapter, events[2], key)))
        tail = bursts._flush(adapter, key, last)
        await asyncio.wait({tail}, timeout=2)
        release.set()
        await asyncio.wait_for(asyncio.gather(*callers, middle, tail), 2)
        assert admitted == ['A', 'C'], 'an empty middle batch must not let C bypass A'
    finally:
        release.set()
        for task in callers:
            task.cancel()
        await asyncio.gather(*callers, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_stale_guard_is_healed_before_receipt_classifies_input(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    board = Board()
    adapter = _adapter(board)
    adapter.set_message_handler(AsyncMock(return_value=None))
    adapter._shared_runtime_owns_input = lambda event: True
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='working'))
    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    event = _event(adapter, board, tmp_path, 9801, 'new input')
    key = adapter._event_session_key(event)
    adapter._active_sessions[key] = asyncio.Event()
    exited = asyncio.get_running_loop().create_future()
    exited.set_result(None)
    adapter._session_tasks[key] = exited
    started = []
    adapter._start_session_processing = lambda event, key: started.append(event) or True
    try:
        adapter._acknowledge_receipt(event.raw_message, event.source)
        adapter._bind_receipt(event.raw_message, event)
        adapter._receipt_dispatched(event.raw_message)
        await adapter.handle_message(event)
        assert event._gateway_accepted is True and started == [event], 'stale guards must not drop fresh input'
        owned = adapter.take_receipt_progress(key, 1, event.message_ids())
        assert owned is not None, 'the healed conversation gets idle Working at receipt'
        await owned._start_task
        await adapter.flush_reactions()
        assert board.calls == []
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_empty_holder_reallocates_number_through_the_queue_counter(tmp_path):
    from gateway.session_ingress_burst import _flush
    board = Board()
    adapter = _adapter(board)
    adapter._shared_runtime_owns_input = lambda event: True
    events = [_event(adapter, board, tmp_path, mid, 'x') for mid in (9901, 9902, 9903)]
    key = adapter._event_session_key(events[0])
    adapter._active_sessions[key] = asyncio.Event()
    try:
        adapter._acknowledge_receipt(events[0].raw_message, events[0].source)
        holder = adapter._shared_bursts[key]
        adapter.on_messages_dropped(events[0].source, events[0].message_ids())
        adapter._acknowledge_receipt(events[1].raw_message, events[1].source)
        _flush(adapter, key, holder)
        adapter._acknowledge_receipt(events[2].raw_message, events[2].source)
        await adapter.flush_reactions()
        assert board.shown[9901] == set()
        assert board.shown[9902] == {N[0]}
        assert board.shown[9903] == {'\u23f3', N[1]}, 'distinct nonempty batches must have distinct numbers'
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_queue", [False, True])
async def test_queued_turn_reuses_receipt_progress_until_queue_ends(monkeypatch, tmp_path, drop_queue):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from gateway.run_turn_bubble_cleanup import register_bubble_cleanup, settle_detached_deletes
    from gateway.run_turn_progress import GatewayTurnProgressMixin
    from gateway.session_ingress_burst import discard_busy_burst

    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    adapter = _adapter(Board(), reactions=False)
    board = Board()
    adapter._shared_runtime_owns_input = lambda event: True
    operations = []

    async def send(chat_id, content, **kwargs):
        operations.append(('send', content))
        return SendResult(success=True, message_id='working')

    async def edit(chat_id, message_id, content, **kwargs):
        operations.append(('edit', content))
        return SendResult(success=True, message_id=message_id)

    adapter.send = send
    adapter.edit_message = edit
    adapter.delete_message = AsyncMock(return_value=True)
    first = _event(adapter, board, tmp_path, 9911, 'first')
    second = _event(adapter, board, tmp_path, 9912, 'second')
    key = adapter._event_session_key(first)

    def start(event, generation):
        ctx = SimpleNamespace(progress_compositor=None, session_key=key, run_generation=generation,
                              source=event.source, inbound_message_ids=event.message_ids(),
                              _progress_reply_to=event.message_id, _progress_metadata={},
                              _cleanup_progress=True, _cleanup_adapter=adapter, _cleanup_msg_ids=[],
                              progress_compositor_mode='single_message')
        turn = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
            _session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(progress_compositor=None)),
            _delivery_adapter_for=lambda source: adapter), _track_compositor_id=lambda c: None)
        GatewayTurnProgressMixin.start_progress_compositor(turn)
        return ctx

    try:
        adapter._acknowledge_receipt(first.raw_message, first.source)
        ctx = start(first, 1)
        await ctx.progress_compositor._start_task
        ctx.progress_compositor.publish_activity('first progress')
        await ctx.progress_compositor.flush(force=True)
        adapter._active_sessions[key] = asyncio.Event()
        adapter._acknowledge_receipt(second.raw_message, second.source)
        register_bubble_cleanup({'completed': True}, ctx)
        operations.append(('reply', 'first'))
        await adapter.pop_post_delivery_callback(key, generation=1)()
        assert adapter.delete_message.await_count == 0, 'queued work must keep the same Working message'
        assert adapter.take_receipt_progress(key, 99, ['slash-command']) is None
        if drop_queue:
            discard_busy_burst(adapter, key)
            await settle_detached_deletes()
        else:
            successor = start(second, 2)
            assert successor.progress_compositor.message_id == 'working'
            successor.progress_compositor.publish_activity('second progress')
            await successor.progress_compositor.flush(force=True)
            # The previous compositor is frozen even if a late producer tries to update it.
            ctx.progress_compositor.publish_activity('stale')
            await ctx.progress_compositor.flush(force=True)
            discard_busy_burst(adapter, key)
            register_bubble_cleanup({'completed': True}, successor)
            operations.append(('reply', 'second'))
            await adapter.pop_post_delivery_callback(key, generation=2)()
            assert any(op == 'edit' and 'second progress' in text for op, text in operations)
            assert not any(op == 'edit' and 'stale' in text for op, text in operations)
        assert sum(op == 'send' for op, _ in operations) == 1
        adapter.delete_message.assert_awaited_once_with('room', 'working')
        assert not adapter._receipt_progress
    finally:
        discard_busy_burst(adapter, key)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_committed_discord_queue_reuses_working_through_real_delivery(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from gateway.run_turn_bubble_cleanup import register_bubble_cleanup
    from gateway.run_turn_progress import GatewayTurnProgressMixin
    from gateway.session_results import execution_result

    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    board = Board()
    runner, adapter, authority = await _owner(monkeypatch, tmp_path, board, reactions=False)
    adapter._busy_text_debounce_seconds = adapter._busy_text_hard_cap_seconds = 0.03
    entered, finish = asyncio.Event(), asyncio.Event()
    second_entered, finish_second = asyncio.Event(), asyncio.Event()
    sends, edits, turns = [], [], []

    async def send(chat_id, content, **kwargs):
        sends.append(content)
        return SendResult(success=True, message_id=str(len(sends)))

    async def edit(chat_id, message_id, content, **kwargs):
        edits.append((message_id, content))
        return SendResult(success=True, message_id=message_id)

    adapter.send = send
    adapter.edit_message = edit
    async def delete(chat_id, message_id):
        assert sends[-1] == 'answer three'
        return True

    adapter.delete_message = AsyncMock(side_effect=delete)

    async def turn(event, source, quick_key, generation):
        key = adapter._event_session_key(event)
        event._hermes_run_generation = generation
        runner._bind_adapter_run_generation(adapter, key, generation)
        ctx = SimpleNamespace(progress_compositor=None, session_key=key, run_generation=generation,
                              source=event.source, inbound_message_ids=event.message_ids(),
                              _progress_reply_to=event.message_id, _progress_metadata={},
                              _cleanup_progress=True, _cleanup_adapter=adapter, _cleanup_msg_ids=[],
                              progress_compositor_mode='single_message')
        consumer = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
            _session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(progress_compositor=None)),
            _delivery_adapter_for=lambda source: adapter), _track_compositor_id=lambda c: None)
        compositor = GatewayTurnProgressMixin.start_progress_compositor(consumer)
        if compositor._start_task:
            await compositor._start_task
        turns.append(compositor.message_id)
        compositor.publish_activity(event.text)
        await compositor.flush(force=True)
        if len(turns) == 1:
            entered.set()
            await finish.wait()
        elif len(turns) == 2:
            second_entered.set()
            await finish_second.wait()
        result = {'final_response': 'answer ' + event.text, 'messages': []}
        execution_result.get()['result'] = result
        register_bubble_cleanup(result, ctx)
        return result['final_response']

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        await (await _receive(adapter, _event(adapter, board, tmp_path, 9921, 'one')))
        await asyncio.wait_for(entered.wait(), 10)
        await (await _receive(adapter, _event(adapter, board, tmp_path, 9922, 'two')))
        await async_wait_until(lambda: not adapter.__dict__.get('_shared_burst_commits'),
                               timeout=10, what='second input committed')
        finish.set()
        await asyncio.wait_for(second_entered.wait(), 10)
        key = adapter._event_session_key(_event(adapter, board, tmp_path, 9923, 'three'))
        await async_wait_until(lambda: key not in adapter._active_sessions, timeout=10,
                               what='original adapter guard released')
        await (await _receive(adapter, _event(adapter, board, tmp_path, 9923, 'three')))
        finish_second.set()
        await async_wait_until(lambda: len(turns) == 3 and adapter.delete_message.await_count == 1,
                               timeout=10, what='last reply and progress deletion')
        assert turns == ['1', '1', '1']
        assert sum('Working' in text for text in sends) == 1
        assert any(mid == '1' and 'one' in text for mid, text in edits)
        assert any(mid == '1' and 'two' in text for mid, text in edits)
        adapter.delete_message.assert_awaited_once_with('room', '1')
        assert not adapter._receipt_progress and not adapter._progress_waiting
    finally:
        finish.set()
        finish_second.set()
        await adapter.cancel_background_tasks()
        await asyncio.to_thread(authority.db.close)
