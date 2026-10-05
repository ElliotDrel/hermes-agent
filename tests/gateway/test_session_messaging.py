"""Discord session messages use existing routes and FIFO; no network/model calls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_messaging import SessionMessenger, bind_session_messenger
from tools.session_message_tool import send_session_message, SESSION_MESSAGE_SCHEMA


def source(chat='1', user='owner', platform=Platform.DISCORD, profile='default'):
    return SessionSource(platform=platform, chat_id=chat, user_id=user, profile=profile)


def harness():
    a, b = source(), source('2')
    entries = {sid: SimpleNamespace(session_id=sid, session_key=sid, origin=src)
               for sid, src in [('a', a), ('b', b)]}
    adapter = SimpleNamespace(_active_sessions=set(), send=AsyncMock(return_value=SimpleNamespace(success=True)))
    async def handle(event):
        events.append(event)
        event._gateway_accepted = True
    def queue(key, event):
        queued.append(event)
        event._gateway_accepted = True
    events, queued = [], []
    adapter.handle_message = handle
    runner = SimpleNamespace(session_store=SimpleNamespace(lookup_by_session_id=entries.get),
        _delivery_adapter_for=lambda src: adapter, _session_key_for_source=lambda src: 'a' if src.chat_id == '1' else 'b',
        _is_session_running=lambda key: key in busy,
        _queue_or_replace_pending_event=queue,
        _thread_metadata_for_source=lambda src: {'thread_id': src.thread_id},
        _draining=False, _external_drain_active=False)
    busy = set()
    return SessionMessenger(runner), a, entries, adapter, events, queued, busy


@pytest.mark.asyncio
async def test_registry_dispatch_trusted_sender_dedup_busy_fifo_and_visible_failure():
    from tools.registry import registry
    messenger, a, entries, adapter, events, queued, busy = harness()
    with bind_session_messenger(messenger, a, 'a', None):
        # A real tool-worker call crosses back to the owning event loop.
        first = json.loads(await asyncio.to_thread(registry.dispatch, 'send_session_message',
                                                   {'target_session_id': 'b', 'message': '/reset'}))
        duplicate = json.loads(await asyncio.to_thread(send_session_message, 'b', '/reset'))
        assert first['delivery'] == 'started' and first['visible'] is True
        assert duplicate['duplicate'] is True
        assert len(events) == 1 and adapter.send.await_count == 1
        assert events[0].internal and not events[0].allow_gateway_control
        assert not events[0].is_command()
        assert 'agent-origin' in events[0].text and 'real user' in events[0].text
        assert 'session a' in events[0].text and '/reset' in events[0].text
        assert adapter.send.call_args.kwargs['metadata']['non_conversational'] is True
        busy.add('b')
        adapter.send.return_value = SimpleNamespace(success=False)
        second = json.loads(await asyncio.to_thread(send_session_message, 'b', 'next'))
        third = json.loads(await asyncio.to_thread(send_session_message, 'b', 'last'))
        assert second['delivery'] == third['delivery'] == 'queued'
        assert second['visible'] is False and second['accepted'] is True
        assert ['next', 'last'] == [e.text.split('\n\n', 1)[1] for e in queued]


@pytest.mark.asyncio
async def test_isolation_refusal_no_sender_spoof_or_unknown_session_creation(monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_ID', 'a')
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'discord')
    messenger, a, entries, adapter, events, queued, busy = harness()
    assert 'error' in json.loads(send_session_message('b', 'outside runtime'))
    with bind_session_messenger(messenger, a, 'a', None):
        for target in ['a', 'missing']:
            assert 'error' in json.loads(await asyncio.to_thread(send_session_message, target, 'x'))
        from agent.delegation_context import delegated_child_context
        with delegated_child_context('child'):
            assert 'Delegated' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'child'))['error']
        for field, value in [('user_id', 'other'), ('profile', 'other'), ('platform', Platform.TELEGRAM)]:
            old = getattr(entries['b'].origin, field)
            setattr(entries['b'].origin, field, value)
            assert 'error' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'x'))
            setattr(entries['b'].origin, field, old)
        messenger.runner._draining = True
        assert 'error' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'x'))
    assert not events and not queued and not adapter.send.called
    assert set(SESSION_MESSAGE_SCHEMA['parameters']['properties']) == {'target_session_id', 'message'}


@pytest.mark.asyncio
async def test_reply_chain_bound_and_new_real_turn_resets_budget():
    messenger, a, entries, adapter, events, queued, busy = harness()
    src, sid, target, inbound = a, 'a', 'b', None
    for hop in range(4):
        with bind_session_messenger(messenger, src, sid, inbound):
            result = json.loads(await asyncio.to_thread(send_session_message, target, f'hop {hop}'))
            assert result['accepted']
        inbound = events[-1]
        sid, target = target, sid
        src = entries[sid].origin
    with bind_session_messenger(messenger, src, sid, inbound):
        assert 'error' in json.loads(await asyncio.to_thread(send_session_message, target, 'too far'))
    with bind_session_messenger(messenger, a, 'a', 'real-human-message'):
        assert json.loads(await asyncio.to_thread(send_session_message, 'b', 'new request'))['accepted']
    with bind_session_messenger(messenger, a, 'a', None):
        for number in range(8):
            assert json.loads(await asyncio.to_thread(send_session_message, 'b', f'branch {number}'))['accepted']
        assert 'error' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'ninth branch'))


@pytest.mark.asyncio
async def test_refused_admission_has_no_visible_post():
    messenger, a, entries, adapter, events, queued, busy = harness()
    adapter.handle_message = AsyncMock()
    with bind_session_messenger(messenger, a, 'a', None):
        result = json.loads(await asyncio.to_thread(send_session_message, 'b', 'refused'))
    assert result['accepted'] is False and result['delivery'] == 'refused'
    adapter.send.assert_not_called()


@pytest.mark.asyncio
async def test_parallel_retry_queue_cap_and_stale_turn_are_refused():
    messenger, a, entries, adapter, events, queued, busy = harness()
    with bind_session_messenger(messenger, a, 'a', None):
        results = await asyncio.gather(*[asyncio.to_thread(send_session_message, 'b', 'same') for _ in range(3)])
        assert len(events) == 1 and sum(json.loads(r).get('duplicate', False) for r in results) == 2
        busy.add('b')
        messenger.runner._queue_or_replace_pending_event = lambda key, event: None
        result = json.loads(await asyncio.to_thread(send_session_message, 'b', 'full'))
        assert result['delivery'] == 'refused' and not result['accepted']
    messenger.runner._is_session_run_current = lambda key, generation: False
    with bind_session_messenger(messenger, a, 'a', None, generation=1):
        assert 'superseded' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'stale'))['error']


@pytest.mark.asyncio
async def test_real_store_real_fifo_cold_admission_and_non_discord_mask(tmp_path):
    from contextlib import nullcontext
    from gateway.config import GatewayConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from gateway.session_messaging import session_messaging_available
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    a, b = source(), source('2')
    sender, target = store.get_or_create_session(a), store.get_or_create_session(b)
    adapter = SimpleNamespace(_active_sessions=set(), _pending_messages={},
                              send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = GatewayConfig()
    runner._draining = runner._external_drain_active = False
    runner._delivery_adapter_for = lambda src: adapter
    runner._is_session_running = lambda key: key == target.session_key
    overflow = []
    runner._session_state = lambda key: SimpleNamespace(conversation=SimpleNamespace(queued_events=overflow))
    runner._peek_session_state = runner._session_state
    messenger = SessionMessenger(runner)
    human = MessageEvent(text='human first', source=b, message_id='human')
    runner._queue_or_replace_pending_event(target.session_key, human)
    with bind_session_messenger(messenger, a, sender.session_id, None):
        for text in ['peer first', 'peer second']:
            assert json.loads(await asyncio.to_thread(send_session_message, target.session_id, text))['delivery'] == 'queued'
        assert session_messaging_available()
        # Other async tasks cannot claim this inherited capability's schema.
        async def foreign_task():
            assert not session_messaging_available()
        await asyncio.create_task(foreign_task())
        with bind_session_messenger(messenger, source(platform=Platform.TELEGRAM), 't', None):
            assert 'error' in json.loads(await asyncio.to_thread(send_session_message, target.session_id, 'masked'))
        assert session_messaging_available()
    assert adapter._pending_messages[target.session_key] is human
    assert [e.text.split('\n\n', 1)[1] for e in overflow] == ['peer first', 'peer second']
    assert all(e._gateway_accepted for e in overflow)
    # Real runner's pending-sentinel path must queue, never steer or merge.
    runner._queue_or_replace_pending_event = Mock()
    runner._hm_busy_slash_or_photo = AsyncMock(side_effect=AssertionError('control dispatch'))
    await runner._hm_handle_running_session_message(overflow[0], b, target.session_key)
    runner._queue_or_replace_pending_event.assert_called_once()
    # Real turn wrapper masks source on entry and revokes capability on exit.
    runner._profile_scope_for_source = lambda src: nullcontext()
    runner._resolve_enabled_toolsets_for_source = lambda *args: ['safe']
    async def inner(message, context, history, src, sid, **kwargs):
        enabled, disabled = runner._resolve_turn_toolsets({}, src, src.platform.value)
        assert ('session_messaging' in enabled) == (src.platform == Platform.DISCORD)
        assert disabled is None
        assert history == [{'role': 'user', 'content': 'unchanged'}]
        return {'messages': history}
    runner._run_agent_inner = inner
    for src in [a, source(platform=Platform.TELEGRAM)]:
        await runner._run_agent('new', 'stable', [{'role': 'user', 'content': 'unchanged'}], src, sender.session_id)
    assert not session_messaging_available()
    assert 'error' in json.loads(send_session_message(target.session_id, 'revoked'))
    with bind_session_messenger(messenger, a, sender.session_id, None):
        enabled, disabled = runner._resolve_turn_toolsets(
            {'agent': {'disabled_toolsets': ['session_messaging']}}, a, 'discord')
        assert enabled == ['safe'] and disabled == ['session_messaging']


@pytest.mark.asyncio
async def test_compression_sender_resolution_uses_owned_profile_route(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.run import GatewayRunner
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    a, b = source(), source('2')
    sender, target = store.get_or_create_session(a), store.get_or_create_session(b)
    parent, key = sender.session_id, sender.session_key
    child = parent + '-compressed'
    db = store._db_for_key(key)
    retained = [{'role': 'user', 'content': 'summary'}, {'role': 'assistant', 'content': 'retained answer'}]
    db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='discord',
        require_compression_lease=False, model='offline', model_config={},
        system_prompt='stable compressed system', messages=retained)
    assert store.advance_compression_session(key, parent, child)
    before = store.load_transcript(child)
    # Re-open the real routing store to prove persistence/resume, not an in-memory fixture.
    resumed = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = resumed
    runner._draining = runner._external_drain_active = False
    events = []
    async def admit(event):
        events.append(event)
        event._gateway_accepted = True
    adapter = SimpleNamespace(_active_sessions=set(), handle_message=admit,
                              send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner._delivery_adapter_for = lambda src: adapter
    runner._is_session_running = lambda key: False
    messenger = SessionMessenger(runner)
    with bind_session_messenger(messenger, a, parent, None):
        result = json.loads(await asyncio.to_thread(send_session_message, target.session_id, 'after compression'))
    assert result['sender_session_id'] == child
    assert resumed.lookup_by_session_id(child).session_key == key
    assert resumed.load_transcript(child) == before
    assert events[0].metadata['gateway_session_id'] == target.session_id
    assert events[0].metadata['gateway_session_strict'] is True


@pytest.mark.asyncio
async def test_real_adapter_idle_starts_one_normal_background_turn(tmp_path):
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.session import SessionStore
    from gateway.run import GatewayRunner
    class OfflineDiscordAdapter(BasePlatformAdapter):
        @property
        def name(self):
            return 'discord'
        async def connect(self, *, is_reconnect=False):
            return True
        async def disconnect(self):
            pass
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)
        async def get_chat_info(self, chat_id):
            return {'id': chat_id, 'type': 'dm'}
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    a, b = source(), source('2')
    sender, target = store.get_or_create_session(a), store.get_or_create_session(b)
    adapter = OfflineDiscordAdapter(PlatformConfig(enabled=True), Platform.DISCORD)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._draining = runner._external_drain_active = False
    runner._delivery_adapter_for = lambda src: adapter
    runner._is_session_running = lambda key: False
    entered, release = asyncio.Event(), asyncio.Event()
    received = []
    async def normal_handler(event):
        received.append(event)
        entered.set()
        await release.wait()
        return None
    adapter.set_message_handler(normal_handler)
    messenger = SessionMessenger(runner)
    with bind_session_messenger(messenger, a, sender.session_id, None):
        result = json.loads(await asyncio.to_thread(send_session_message, target.session_id, 'idle peer'))
    assert result['delivery'] == 'started' and result['accepted']
    await asyncio.wait_for(entered.wait(), 3)
    assert len(received) == 1 and not received[0].allow_gateway_control
    assert target.session_key in adapter._active_sessions
    task = adapter._session_tasks[target.session_key]
    release.set()
    await asyncio.wait_for(task, 3)
    assert target.session_key not in adapter._active_sessions
