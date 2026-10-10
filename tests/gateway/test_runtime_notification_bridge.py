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
        assert runner._overflow_queue(key)[-1].text == str(pending)
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
