"""Shared wake admission reaches the active parent, not the human FIFO or children."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.runtime_notifications import is_notification_event
from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.wake import deliver_wake, admit_internal_event


class Parent(InterruptControlMixin):
    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._active_children = [MagicMock()]


def _gateway_fixture():
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._draining = False
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42', chat_type='dm')
    key = build_session_key(source)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._session_state(key).turn.agent = Parent()
    return runner, adapter, source, key


@pytest.mark.asyncio
async def test_mixed_handoff_preserves_human_marker_and_display_kind():
    from agent.runtime_notifications import deliver_runtime_notification
    from agent.prompt_builder import STEER_MARKER_OPEN
    from gateway.response_filters import display_kind_for_event
    runner, adapter, source, key = _gateway_fixture()
    parent = runner._session_state(key).turn.agent
    parent.steer('real human correction')
    deliver_runtime_notification(parent, 'work finished')
    leftover = parent._drain_pending_steer(close_runtime=True)
    event, text = await runner._run_agent_drain_pending({'pending_steer': leftover}, adapter, source, key)
    assert display_kind_for_event(event) == 'steer'
    assert STEER_MARKER_OPEN in text
    assert 'Runtime note' in text
    assert event._pending_steer_handoff.parts == leftover.parts


@pytest.mark.asyncio
async def test_runtime_only_handoff_reaches_running_successor_without_human_fifo():
    from agent.runtime_notifications import deliver_runtime_notification
    runner, adapter, source, key = _gateway_fixture()
    old = Parent()
    deliver_runtime_notification(old, 'late old work')
    deliver_runtime_notification(old, 'another late result')
    leftover = old._drain_pending_steer(close_runtime=True)
    human = MessageEvent(text='human FIFO', source=source)
    adapter._pending_messages[key] = human
    event, text = await runner._run_agent_drain_pending({'pending_steer': leftover}, adapter, source, key)
    successor = runner._session_state(key).turn.agent
    assert successor._pending_steer.parts == leftover.parts
    assert event is human and text == human.text
    assert not runner._overflow_queue(key)


@pytest.mark.asyncio
async def test_cached_startup_promotes_only_marked_notifications_once():
    from agent.runtime_notifications import mark_notification_event, deliver_runtime_notification
    runner, adapter, source, key = _gateway_fixture()
    parent = runner._session_state(key).turn.agent
    parent._drain_pending_steer(close_runtime=True)
    human = MessageEvent(text='human FIFO', source=source)
    adapter._pending_messages[key] = human
    for text in ('setup A', 'setup B'):
        event = MessageEvent(text=text, source=source, internal=True)
        mark_notification_event(event)
        await runner._handle_active_session_busy_message(event, key)
    from gateway.turn_context import TurnContext
    ctx = TurnContext(source=source, session_key=key)
    ctx.agent_holder[0] = parent
    parent._runtime_notification_closed = False  # facade safe-start reopening
    await runner._run_agent_track_agent(ctx)
    assert 'setup A' in parent._pending_steer and 'setup B' in parent._pending_steer
    assert str(parent._pending_steer).index('setup A') < str(parent._pending_steer).index('setup B')
    assert adapter._pending_messages[key] is human
    assert not runner._overflow_queue(key)
    await runner._run_agent_track_agent(ctx)
    pending = parent._drain_pending_steer(close_runtime=True)
    assert str(pending).count('setup A') == 1
    assert not deliver_runtime_notification(parent, 'after finalization')
    late = MessageEvent(text='closed retry', source=source, internal=True)
    mark_notification_event(late)
    await runner._handle_active_session_busy_message(late, key)
    await runner._run_agent_track_agent(ctx)
    assert runner._overflow_queue(key) == [late]
    assert parent._pending_steer is None


@pytest.mark.asyncio
async def test_active_wake_fifo_startup_and_late_handoff(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._draining = False
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42', chat_type='dm')
    key = build_session_key(source)
    parent = Parent()
    runner._session_state(key).turn.agent = parent
    human = MessageEvent(text='human FIFO', source=source)
    adapter._pending_messages[key] = human
    adapter._active_sessions[key] = asyncio.Event()
    async def handler(event): return None
    adapter.set_message_handler(handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    try:
        await deliver_wake(adapter, text='A completed', source=source)
        await deliver_wake(adapter, text='B progress', source=source)
        from agent.runtime_notifications import mark_notification_event
        fast = MessageEvent(text='runner fast path', source=source, internal=True)
        mark_notification_event(fast)
        await runner._hm_handle_running_session_message(fast, source, key)
        assert fast._gateway_accepted
        assert adapter._pending_messages[key] is human
        pending = parent._drain_pending_steer(close_runtime=True)
        assert str(pending).index('A completed') < str(pending).index('B progress')
        assert not runner._overflow_queue(key)
        parent._active_children[0].steer.assert_not_called()
        # Arrival after finalizer closure remains a distinct existing FIFO wake.
        await deliver_wake(adapter, text='late', source=source)
        assert runner._overflow_queue(key)[0].text == 'late'
        event, text = await runner._run_agent_drain_pending({'pending_steer':pending}, adapter, source, key)
        assert event is human and text == 'human FIFO'
        handoff = runner._overflow_queue(key)[-1]
        assert handoff._pending_steer_handoff.parts == pending.parts
        assert is_notification_event(handoff)
        # Sentinel startup can't take a notification: fallback remains accepted once.
        from gateway.run import _AGENT_PENDING_SENTINEL
        runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL
        await deliver_wake(adapter, text='startup', source=source)
        assert runner._overflow_queue(key)[-1].text == 'startup'
        # Peer/internal metadata does not acquire runtime provenance through shared receipts.
        peer = MessageEvent(text='/stop peer data', source=source, internal=True,
                            allow_gateway_control=False, metadata={'notification_category':'result', 'session_message':True})
        runner._is_user_authorized_for_source = lambda source: True
        runner._admit_bot_message_for_source = lambda source: True
        runner._effective_busy_input_mode = lambda source: 'queue'
        async def no_approval(*args): return False
        runner._route_plaintext_approval_while_busy = no_approval
        await admit_internal_event(adapter, peer)
        assert not is_notification_event(peer)
        assert peer.get_command() is None
        assert runner._overflow_queue(key)[-1] is peer
    finally:
        adapter._active_sessions.clear()
        adapter._pending_messages.clear()
