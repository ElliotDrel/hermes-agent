"""Admission lifetime regressions: discarded work must not exhaust the gateway."""
import asyncio
import json

import pytest

from gateway.session_messaging import bind_session_messenger
from tools.session_message_tool import send_session_message
from tests.gateway.test_session_messaging import harness


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery', ['started', 'queued'])
async def test_discarded_admissions_do_not_exhaust_future_real_turns(monkeypatch, delivery):
    import gateway.session_messaging as messaging
    monkeypatch.setattr(messaging, 'MAX_PENDING', 3, raising=False)
    messenger, src, entries, adapter, events, queued, busy = harness()
    if delivery == 'queued':
        busy.add('b')
    for number in range(5):
        with bind_session_messenger(messenger, src, 'a', f'human-{number}'):
            receipt = json.loads(await asyncio.to_thread(send_session_message, 'b', str(number)))
        assert receipt.get('accepted') is True, 'Discarded admissions permanently exhaust the pending cap'
        # A reset/stop/preparation rejection can discard admitted work before bind.
        events.clear()
        queued.clear()
    assert not getattr(messenger, 'pending', {}), 'Messenger retains orphan chain records'


@pytest.mark.asyncio
async def test_peer_missing_trusted_budget_fails_closed():
    messenger, src, entries, adapter, events, queued, busy = harness()
    with bind_session_messenger(messenger, src, 'a', 'session-message:lost:receipt'):
        receipt = json.loads(await asyncio.to_thread(send_session_message, 'b', 'no fresh budget'))
    assert 'error' in receipt, 'Peer input without its trusted budget gets a fresh automatic chain'


@pytest.mark.asyncio
async def test_live_queued_branches_share_budget_without_expiry():
    messenger, src, entries, adapter, events, queued, busy = harness()
    busy.add('b')
    with bind_session_messenger(messenger, src, 'a', None):
        for number in range(8):
            assert json.loads(await asyncio.to_thread(send_session_message, 'b', str(number)))['accepted']
    for event in queued:
        with bind_session_messenger(messenger, entries['b'].origin, 'b', event):
            receipt = json.loads(await asyncio.to_thread(send_session_message, 'a', 'branch reply'))
        assert 'error' in receipt, 'A queued branch lost its shared exhausted budget'


@pytest.mark.asyncio
async def test_real_adapter_cancel_before_bind_releases_admission(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionStore
    from tests.gateway.test_run_cleanup_progress import CleanupCaptureAdapter, _make_runner
    from tests.gateway.test_session_messaging import source
    import gateway.session_messaging as messaging
    monkeypatch.setattr(messaging, 'MAX_PENDING', 3, raising=False)
    adapter = CleanupCaptureAdapter(Platform.DISCORD)
    runner = _make_runner(adapter)
    runner.config = GatewayConfig()
    runner._init_lifecycle_state()
    runner._profile_adapters = {}
    runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
    a, b = source(), source('2')
    sender = runner.session_store.get_or_create_session(a)
    target = runner.session_store.get_or_create_session(b)
    messenger = messaging.SessionMessenger(runner)
    entered = asyncio.Event()
    async def before_preparation(event):
        entered.set()
        await asyncio.Event().wait()  # Stop before the destination binds a turn.
    adapter.set_message_handler(before_preparation)
    for number in range(5):
        entered.clear()
        with bind_session_messenger(messenger, a, sender.session_id, f'user-{number}'):
            receipt = json.loads(await asyncio.to_thread(send_session_message, target.session_id, str(number)))
        assert receipt.get('accepted') is True, 'Cancelled admissions permanently exhaust the pending cap'
        await asyncio.wait_for(entered.wait(), 3)
        task = adapter._session_tasks[target.session_key]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert target.session_key not in adapter._active_sessions
    assert not getattr(messenger, 'pending', {})
