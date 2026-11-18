"""Discord acknowledges every message on receipt and a merged turn keeps every message's identity.

Behavior contract (HERMES-FORK-018): each accepted message is marked ⏳ (conversation busy) or 👀
(idle) when it arrives; a turn marks every message it carries 👀 at start and ✅/❌ at its end; the
reply anchors to the latest merged message; reaction calls are detached and never delay the turn;
a dropped message loses its ⏳. Real GatewayRunner, DiscordAdapter, SessionAuthority and drain over
SQLite; the agent turn and Discord's message objects are stand-ins.
"""
import asyncio
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
WAITING, WORKING = '⏳', '👀'


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
@pytest.mark.parametrize('restarted', [False, True], ids=['live', 'restored-after-restart'])
async def test_busy_burst_acks_every_message_and_keeps_identity(monkeypatch, tmp_path, restarted):
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
    assert board.shown[1000] == {WORKING}, 'an idle message is 👀 from receipt'

    burst = [_event(adapter, board, tmp_path, IDS[0], 'caption', photo=True)]
    burst += [_event(adapter, board, tmp_path, mid, photo=True) for mid in IDS[1:]]
    callers = [await _receive(adapter, e) for e in burst]
    await adapter.flush_reactions()
    assert board.marks(IDS) == {mid: {WAITING} for mid in IDS}, 'every busy message is ⏳ on receipt'
    await asyncio.wait_for(asyncio.gather(*callers), 10)

    rows = list_session_admissions(authority.db, session_id=authority.logical_owner(
        runner.session_store.peek_session_id(runner.session_store._generate_session_key(first.source))),
        pending_only=False)
    assert len(rows) == 2, 'the whole burst is one queued turn'
    assert 'absorbed_message_ids' not in rows[0]['payload']['native_text_v1']['event']  # old shape: no field
    assert restore_native(rows[0]['payload'], runner).message_ids() == ['1000']
    assert rows[1]['payload']['native_text_v1']['event']['absorbed_message_ids'] == [str(m) for m in IDS]
    assert restore_native(rows[1]['payload'], runner).absorbed_message_ids == [str(m) for m in IDS]

    if restarted:
        adapter._receipt_marks().clear()  # a new process knows nothing of the earlier marks
    finish.set()
    await async_wait_until(lambda: len(turns) == 2, timeout=10, what='merged turn executed')
    merged = turns[1]
    assert merged['ids'] == [str(m) for m in IDS]
    assert merged['marks'] == {mid: {WORKING} for mid in IDS}, 'turn start: every message 👀, no ⏳ left'
    assert merged['anchor'] == str(IDS[-1]), 'the reply anchors to the latest message'
    assert merged['note'].startswith(f'[Triggering message id: `{IDS[-1]}` (merged 8 messages)')
    await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what='turn closed')
    await adapter.flush_reactions()
    assert board.marks(IDS + [1000]) == {mid: {'✅'} for mid in IDS + [1000]}
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_idle_message_is_acknowledged_once(monkeypatch, tmp_path):
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
    await adapter.flush_reactions()
    assert board.shown[3001] == {WORKING}
    await adapter._process_message_background(event, build_session_key(event.source))
    await adapter.flush_reactions()
    assert [c for c in board.calls if c[2] == WORKING and c[1] == 'add'] == [(3001, 'add', WORKING)]
    assert board.shown[3001] == {'✅'}


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
    assert board.marks([5001, 5002]) == {5001: {WAITING}, 5002: {WAITING}}
    discard_busy_burst(adapter, key)  # /stop or /new while the burst is still held
    await adapter.flush_reactions()
    assert board.marks([5001, 5002]) == {5001: set(), 5002: set()}

    queued = [_event(adapter, board, tmp_path, mid, 'queued') for mid in (6001, 6002)]
    callers = [await _receive(adapter, e) for e in queued]
    await asyncio.wait_for(asyncio.gather(*callers), 10)  # committed behind the running turn
    await adapter.flush_reactions()
    assert board.marks([6001, 6002]) == {6001: {WAITING}, 6002: {WAITING}}
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
    assert board.marks([8001, 8002]) == {8001: set(), 8002: {WAITING}}
    adapter._text_debounce_store()[key] = TextDebounceState(third, None, 0.0, 0.0)
    adapter._discard_text_debounce(key)  # a control command drops the debounce buffer
    await adapter.flush_reactions()
    assert board.marks([8003]) == {8003: set()}
    await adapter.cancel_session_processing(key)  # /stop drops the pending slot
    await adapter.flush_reactions()
    assert board.marks([8002, 8004]) == {8002: set(), 8004: {WAITING}}, 'only what was dropped is cleared'


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
    assert board.marks([8101, 8102]) == {8101: set(), 8102: {WORKING}}
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
    assert board.shown[8201] == {'✅'}


@pytest.mark.asyncio
async def test_unrecorded_waiting_mark_is_removed_and_drops_read_no_config(tmp_path):
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)
    stuck = _busy_ack(adapter, board, tmp_path, 8301)
    adapter._receipt_marks().clear()  # evicted past the cap, or a timed-out call: the ⏳ still shows
    await adapter.flush_reactions()
    assert board.shown[8301] == {WAITING}
    await adapter.on_processing_start(stuck)
    await adapter.on_processing_complete(stuck, ProcessingOutcome.SUCCESS)
    await adapter.flush_reactions()
    assert board.shown[8301] == {'✅'}
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
    assert board.shown[8401] == {WAITING}
    join_event(steered)
    await adapter.flush_reactions()
    assert board.shown[8401] == {WORKING}


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
    assert board.shown[9001] == {WAITING}
    adapter.on_messages_dropped(source, ['9001'])
    await adapter.flush_reactions()
    assert board.shown[9001] == set()


@pytest.mark.asyncio
async def test_merged_event_reacts_to_each_of_its_own_messages(tmp_path):
    """A merged event keeps the head's ``raw_message`` while ``message_id`` is the latest: every carried
    message still gets its own 👀 and result, never the head twice."""
    from gateway.platforms.event import ProcessingOutcome

    board = Board()
    adapter = _adapter(board)
    head, tail = (_busy_ack(adapter, board, tmp_path, mid) for mid in (9101, 9102))
    head.absorb_turn_input(tail)
    head.message_id = '9102'  # the debounce merge path keeps the head's raw_message and moves the id
    await adapter.flush_reactions()
    await adapter.on_processing_start(head)
    await adapter.flush_reactions()
    assert board.marks([9101, 9102]) == {9101: {WORKING}, 9102: {WORKING}}
    await adapter.on_processing_complete(head, ProcessingOutcome.SUCCESS)
    await adapter.flush_reactions()
    assert board.marks([9101, 9102]) == {9101: {'✅'}, 9102: {'✅'}}
    assert [c for c in board.calls if c[:2] == (9101, 'add')] == [(9101, 'add', WAITING), (9101, 'add', WORKING), (9101, 'add', '✅')]
