"""HERMES-FORK-021: scoped Discord session messages (no network, no model calls).

The unit half drives ``SessionMessenger`` against fakes; the ingress half runs a real
``GatewayRunner``, ``DiscordAdapter`` and session authority so a message from session A reaches
session B through the adapter's own admission (idle start, busy FIFO) rather than a private queue.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_messaging import (
    SessionMessenger, bind_session_messenger, session_messaging_available)
from tools.session_message_tool import SESSION_MESSAGE_SCHEMA, send_session_message
from tests.waiting import async_wait_until


PEER_BREAK = '\n\n'  # the peer header and the sender's text are separated by a blank line


def source(chat='1', user='owner', platform=Platform.DISCORD, profile='default'):
    return SessionSource(platform=platform, chat_id=chat, user_id=user, profile=profile)


@asynccontextmanager
async def _no_scope():
    yield


def harness():
    a, b = source(), source('2')
    entries = {sid: SimpleNamespace(session_id=sid, session_key=sid, origin=src)
               for sid, src in [('a', a), ('b', b)]}
    adapter = SimpleNamespace(_active_sessions=set(), send=AsyncMock(return_value=SimpleNamespace(success=True)))
    events, scopes, busy = [], [], set()

    async def handle(event):
        events.append(event)
        event._gateway_accepted = True

    adapter.handle_message = handle

    def scope(src):
        scopes.append(src)
        return _no_scope()

    runner = SimpleNamespace(
        session_store=SimpleNamespace(lookup_by_session_id=entries.get),
        _delivery_adapter_for=lambda src: adapter,
        _session_key_for_source=lambda src: 'a' if src.chat_id == '1' else 'b',
        _is_session_running=lambda key: key in busy,
        _async_profile_scope_for_source=scope,
        _thread_metadata_for_source=lambda src: {'thread_id': src.thread_id},
        _draining=False, _external_drain_active=False)
    messenger = SessionMessenger(runner)
    messenger.scopes = scopes
    return messenger, a, entries, adapter, events, busy


@pytest.mark.asyncio
async def test_registry_dispatch_trusted_sender_dedup_busy_and_visible_failure():
    from tools.registry import registry
    messenger, a, entries, adapter, events, busy = harness()
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
        assert second['delivery'] == 'queued' and second['accepted'] is True and second['visible'] is False
    # The sender's profile scope was bound on the loop for every delivery.
    assert messenger.scopes and all(s is a or s == a for s in messenger.scopes)


@pytest.mark.asyncio
async def test_isolation_refusal_no_sender_spoof_or_unknown_session_creation(monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_ID', 'a')
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'discord')
    messenger, a, entries, adapter, events, busy = harness()
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
    assert not events and not adapter.send.called
    assert set(SESSION_MESSAGE_SCHEMA['parameters']['properties']) == {'target_session_id', 'message'}


@pytest.mark.asyncio
async def test_reply_chain_bound_and_new_real_turn_resets_budget():
    messenger, a, entries, adapter, events, busy = harness()
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
    # A peer turn whose trusted budget was lost (serialized/restored) gets no fresh chain.
    with bind_session_messenger(messenger, a, 'a', 'session-message:lost:receipt'):
        assert 'error' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'no fresh budget'))


@pytest.mark.asyncio
async def test_refused_admission_has_no_visible_post_and_stale_turn_is_refused():
    messenger, a, entries, adapter, events, busy = harness()
    adapter.handle_message = AsyncMock()
    with bind_session_messenger(messenger, a, 'a', None):
        result = json.loads(await asyncio.to_thread(send_session_message, 'b', 'refused'))
    assert result['accepted'] is False and result['delivery'] == 'refused'
    adapter.send.assert_not_called()
    messenger.runner._is_session_run_current = lambda key, generation: False
    with bind_session_messenger(messenger, a, 'a', None, generation=1):
        assert 'superseded' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'stale'))['error']


@pytest.mark.asyncio
async def test_capability_is_owned_by_the_turn_task_and_revoked_on_exit():
    messenger, a, entries, adapter, events, busy = harness()
    assert not session_messaging_available()
    with bind_session_messenger(messenger, a, 'a', None):
        assert session_messaging_available()

        async def foreign_task():
            assert not session_messaging_available()
        await asyncio.create_task(foreign_task())  # a spawned task inherits the context, not ownership
        with bind_session_messenger(messenger, source(platform=Platform.TELEGRAM), 't', None):
            assert not session_messaging_available()
            assert 'error' in json.loads(await asyncio.to_thread(send_session_message, 'b', 'masked'))
        assert session_messaging_available()
    assert not session_messaging_available()
    assert 'error' in json.loads(send_session_message('b', 'revoked'))


# --- schema scope ---------------------------------------------------------------------------------


def test_tool_schema_exists_only_in_its_named_toolset():
    from model_tools import get_tool_definitions
    from toolsets import _HERMES_CORE_TOOLS, TOOLSETS
    assert 'send_session_message' not in _HERMES_CORE_TOOLS
    assert TOOLSETS['session_messaging']['tools'] == ['send_session_message']
    raw = get_tool_definitions(enabled_toolsets=['session_messaging'], skip_tool_search_assembly=True)
    assert [s['function']['name'] for s in raw] == ['send_session_message']
    assert raw[0]['function'] == SESSION_MESSAGE_SCHEMA
    for toolsets in (['discord'], ['hermes-discord'], ['safe']):
        names = [s['function']['name'] for s in get_tool_definitions(
            enabled_toolsets=toolsets, skip_tool_search_assembly=True)]
        assert 'send_session_message' not in names


@pytest.mark.asyncio
async def test_toolset_is_granted_only_to_an_owned_discord_turn():
    from gateway.run import GatewayRunner
    messenger, a, entries, adapter, events, busy = harness()
    runner = object.__new__(GatewayRunner)
    config = {'platform_toolsets': {'discord': ['safe', 'session_messaging']}}
    # Configuration (or `hermes tools`) can never grant it: only the turn boundary can.
    enabled, disabled = runner._resolve_turn_toolsets(config, a, 'discord')
    assert 'session_messaging' not in enabled

    import dataclasses

    @dataclasses.dataclass
    class Disp:
        enabled_toolsets: list
        disabled_toolsets: list | None = None

    disp = Disp(['safe'])
    assert runner._with_session_messaging(disp, a) is disp  # no turn bound: nothing granted
    with bind_session_messenger(messenger, a, 'a', None):
        assert runner._with_session_messaging(disp, a).enabled_toolsets == ['safe', 'session_messaging']
        assert disp.enabled_toolsets == ['safe']  # the shared settings object is never mutated
        # Explicit disable is the rollback switch.
        assert runner._with_session_messaging(Disp(['safe'], ['session_messaging']), a).enabled_toolsets == ['safe']
        # Another transport never receives it, even inside a bound Discord turn.
        assert runner._with_session_messaging(disp, source(platform=Platform.TELEGRAM)) is disp
        from agent.delegation_context import delegated_child_context
        with delegated_child_context('child'):
            assert runner._with_session_messaging(disp, a) is disp
    with bind_session_messenger(messenger, source(platform=Platform.TELEGRAM), 't', None):
        assert runner._with_session_messaging(disp, source(platform=Platform.TELEGRAM)) is disp


# --- real ingress ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_message_reaches_idle_and_busy_sessions_through_real_ingress(monkeypatch):
    """A -> B through the real adapter admission: idle B starts a turn; busy B queues in order,
    never interrupted, and the queued peer turn then runs as its own internal, non-control turn.
    (Internal events are admitted by the adapter, not ``session_ingress.admit_message``, which
    ``_hm_authority_admission`` reserves for non-internal input.)"""
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(group_sessions_per_user=False))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False,
                                            extra={'group_sessions_per_user': False}))
    posts = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        posts.append((chat_id, content, metadata))
        return SendResult(success=True, message_id='sent')
    monkeypatch.setattr(adapter, 'send', send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter, busy_text_mode='queue')

    running, finish = asyncio.Event(), asyncio.Event()
    executed, interrupted, peer_ids = [], [], []

    async def turn(event, source_, quick_key, run_generation):
        if event.internal and (event.text.endswith('two') or event.text.endswith('three')):
            peer_ids.append(event.message_id)
        executed.append((event.text, bool(event.internal), event.get_command(), source_.chat_id))
        if event.text == 'human on B':
            running.set()
            await finish.wait()
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_interrupt_running_turn', lambda *a, **k: interrupted.append(a) or 0)

    src_a = adapter.build_source(chat_id='roomA', chat_type='group', user_id='42')
    src_b = adapter.build_source(chat_id='roomB', chat_type='group', user_id='42')
    sender = runner.session_store.get_or_create_session(src_a)
    target = runner.session_store.get_or_create_session(src_b)
    messenger = SessionMessenger(runner)

    # Idle B with no committed conversation yet: the adapter admits one normal internal turn.
    with bind_session_messenger(messenger, src_a, sender.session_id, 'human-A-1'):
        idle = json.loads(await asyncio.to_thread(send_session_message, target.session_id, 'peer one'))
    assert idle['delivery'] == 'started' and idle['accepted'] and idle['visible']
    await async_wait_until(lambda: len(executed) == 1, timeout=10, what='idle peer turn')
    text, internal, command, chat = executed[0]
    assert internal and command is None and chat == 'roomB'
    assert text.endswith('peer one') and f'session {sender.session_id}' in text
    assert posts and posts[-1][2]['non_conversational'] is True

    # Busy B: a human turn is running; two peer messages commit to its durable FIFO, in order.
    await adapter.handle_message(MessageEvent(text='human on B', source=src_b, message_id='hb'))
    await asyncio.wait_for(running.wait(), 10)
    with bind_session_messenger(messenger, src_a, sender.session_id, 'human-A-2'):
        second = json.loads(await asyncio.to_thread(send_session_message, target.session_id, 'peer two'))
        third = json.loads(await asyncio.to_thread(send_session_message, target.session_id, '/reset peer three'))
    assert second['delivery'] == third['delivery'] == 'queued' and second['accepted'] and third['accepted']
    assert len(executed) == 2 and not interrupted  # the running turn was neither steered nor interrupted
    queued_rows = [r for r in list_session_admissions(authority.db, session_id=target.session_id)
                   if r['status'] == 'queued']
    assert [r['payload']['text'].split(PEER_BREAK)[-1] for r in queued_rows] == ['peer two', '/reset peer three']
    assert all(r['principal_id'] == 'automation:' + target.session_key for r in queued_rows)
    finish.set()
    await async_wait_until(lambda: len(executed) == 4, timeout=10, what='queued peer turns ran after B, in order')
    assert [t[0].split(PEER_BREAK)[-1] if t[1] else t[0] for t in executed] == [
        'peer one', 'human on B', 'peer two', '/reset peer three']
    assert all(t[1] and t[2] is None for t in executed if t[0] != 'human on B')  # peer text never parses as a command

    # The restored durable event finds its shared chain budget again by message id, so a reply
    # from B is bounded by the same chain (and an id the messenger never minted gets none).
    queued_ids = [second['message_id'], third['message_id']]
    assert peer_ids == queued_ids
    chain = messenger.deliveries[queued_ids[0]].chain
    with bind_session_messenger(messenger, src_b, target.session_id, queued_ids[0]):
        reply = json.loads(await asyncio.to_thread(send_session_message, sender.session_id, 'reply'))
    assert reply['accepted'] and chain.remaining == 5  # 8 - peer two/three (A) - one reply (B), shared
    with bind_session_messenger(messenger, src_b, target.session_id, '["session-message","k","forged"]'):
        assert 'error' in json.loads(await asyncio.to_thread(send_session_message, sender.session_id, 'x'))
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
async def test_cross_profile_and_cross_user_targets_are_refused_before_delivery(monkeypatch):
    messenger, a, entries, adapter, events, busy = harness()
    for field, value in (('profile', 'other'), ('user_id', 'someone-else')):
        old = getattr(entries['b'].origin, field)
        setattr(entries['b'].origin, field, value)
        with bind_session_messenger(messenger, a, 'a', None):
            refused = json.loads(await asyncio.to_thread(send_session_message, 'b', 'nope'))
        setattr(entries['b'].origin, field, old)
        assert 'error' in refused
    assert not events and not adapter.send.called
