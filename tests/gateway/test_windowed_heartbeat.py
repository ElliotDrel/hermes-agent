"""Windowed heartbeat command parity and actual gateway execution boundary."""
import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import heartbeat as hb
from hermes_cli.cli_commands_mixin import CLICommandsMixin


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', [Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK])
async def test_cli_gateway_option_prefix_parity(monkeypatch, platform):
    cli_mgr, gateway_mgr = hb.HeartbeatManager('cli-options'), hb.HeartbeatManager('gateway-options')
    cli = SimpleNamespace(_start_heartbeat_watchdog=lambda: None)
    cli._get_heartbeat_manager = lambda: cli_mgr
    cli._session_manager = lambda getter, label: getter()
    cli._heartbeat_set = lambda mgr, arg: CLICommandsMixin._heartbeat_set(cli, mgr, arg)
    cmd = 'every 15m --windows 07:15-09:00,23:00-00:30 --timezone America/New_York --include-time Ask "which set?"\nKeep — and --windows literal  '
    CLICommandsMixin._handle_heartbeat_command(cli, '/heartbeat ' + cmd)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._get_heartbeat_manager_for_event = AsyncMock(return_value=(gateway_mgr, None))
    runner._session_key_for_source = lambda source: 'route'
    runner._register_heartbeat_watch = lambda *args: None
    event = MessageEvent(text='/heartbeat ' + cmd, source=SessionSource(platform=platform, chat_id='parity'))
    result = await runner._handle_heartbeat_command(event)
    assert cli_mgr.state.windows == gateway_mgr.state.windows == ['07:15-09:00', '23:00-00:30']
    assert cli_mgr.state.prompt == gateway_mgr.state.prompt == 'Ask "which set?"\nKeep — and --windows literal  '
    assert cli_mgr.state.include_time and gateway_mgr.state.include_time
    for mgr in (cli_mgr, gateway_mgr):
        assert 'America/New_York' in mgr.status_line() and 'include-time' in mgr.status_line()
        assert '23:00-00:30' in mgr.status_line()
    assert 'Heartbeat set' in result
    before = gateway_mgr.state.to_json()
    event.text = '/heartbeat 15m --windows 09:00-09:00 --timezone UTC bad'
    assert 'Invalid heartbeat' in await runner._handle_heartbeat_command(event)
    assert gateway_mgr.state.to_json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('delayed', [False, True])
@pytest.mark.parametrize('control', [None, 'pause', 'clear', 'replacement'])
async def test_prepared_payload_rechecks_window_and_stamps_execution_time(tmp_path, monkeypatch, delayed, control):
    clock = SimpleNamespace(now=epoch('2026-09-30T09:00:05-04:00'))
    monkeypatch.setattr(hb, 'time', SimpleNamespace(time=lambda: clock.now))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_telegram_topic_lane = lambda source: False
    runner._cache_session_source = lambda *args: None
    runner._clear_session_env = lambda tokens: None
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='window', user_id='owner')
    entry = runner.session_store.get_or_create_session(source)
    await runner._warm_goals_session_db('test')
    manager = hb.HeartbeatManager(entry.session_id)
    state = manager.set('specific question', 900, windows=['07:15-09:00'], timezone='America/New_York', include_time=True)
    state.created_at = clock.now - 3600
    hb.save_heartbeat(entry.session_id, state)
    history = [{'role': 'user', 'content': 'cached prefix'}, {'role': 'assistant', 'content': 'old answer'}]
    snapshot = copy.deepcopy(history)
    observed = []
    controlled_state = []

    async def prepare(event, *args, **kwargs):
        # The real gateway uses a copied prepared message, not event.text at execution.
        prepared = runner._PreparedTurn(history, 'stable system/context', event.text, event.text, None, None)
        clock.now = epoch('2026-09-30T09:01:00-04:00' if delayed else '2026-09-30T09:00:59-04:00')
        if control:
            controls = hb.HeartbeatManager(entry.session_id)
            if control == 'replacement':
                controls.set('replacement', 900, include_time=True)
            else:
                getattr(controls, control)()
            latest = hb.load_heartbeat(entry.session_id)
            controlled_state.append(latest.to_json() if latest else None)
        return prepared, {}

    async def model(**kwargs):
        observed.append(kwargs)
        raise asyncio.CancelledError

    runner._hmwa_prepare_turn = prepare
    runner._run_agent = model
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    adapter.set_message_handler(lambda event: runner._handle_message_with_agent(event, source, entry.session_key, 1))
    try:
        await runner._heartbeat_poll_once({entry.session_key: (source, entry.session_id)})
        assert entry.session_key in adapter._session_tasks
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert len(observed) == int(not delayed and not control), 'closed windows or heartbeat controls must drop the prepared attempt'
        assert history == snapshot
        if observed:
            request = observed[0]
            assert '2026-09-30T09:00:59-04:00 (America/New_York)' in request['message']
            assert '09:00:05' not in request['message'], 'admission-time stamp must not reach the model stale'
            assert request['history'] == snapshot
            assert request['context_prompt'] == 'stable system/context'
            assert request['session_id'] == entry.session_id
            assert request['persist_user_message'] == request['message']
        if control:
            latest = hb.load_heartbeat(entry.session_id)
            assert (latest.to_json() if latest else None) == controlled_state[0]
        else:
            assert hb.HeartbeatManager(entry.session_id).state.fire_count == int(not delayed)
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', ['legacy — question', 'legacy – question', 'legacy\nquestion  '])
async def test_legacy_gateway_prompt_bytes_keep_event_parser_behavior(prompt):
    manager = hb.HeartbeatManager('legacy-' + prompt)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._get_heartbeat_manager_for_event = AsyncMock(return_value=(manager, None))
    runner._session_key_for_source = lambda source: 'route'
    runner._register_heartbeat_watch = lambda *args: None
    event = MessageEvent(text='/heartbeat every 15m ' + prompt, source=SessionSource(platform=Platform.DISCORD, chat_id='legacy'))
    expected = event.get_command_args().strip().split(None, 2)[2].strip()
    await runner._handle_heartbeat_command(event)
    assert manager.state.prompt == expected
    assert not manager.state.windows and not manager.state.include_time and manager.state.timezone is None


def test_windowed_refund_follows_compressed_owner_without_rewinding_controls(monkeypatch):
    from gateway.run_heartbeat_acceptance import settle_heartbeat_attempt
    clock = SimpleNamespace(now=epoch('2026-09-30T08:01:00-04:00'))
    monkeypatch.setattr(hb, 'time', SimpleNamespace(time=lambda: clock.now))
    manager = hb.HeartbeatManager('refund-parent')
    state = manager.set('question', 900, windows=['07:15-09:00'], timezone='America/New_York')
    state.created_at = clock.now - 3600
    hb.save_heartbeat(manager.session_id, state)
    assert manager.due_prompt()
    snapshot = hb.HeartbeatTick(manager).state
    assert hb.migrate_heartbeat_to_session('refund-parent', 'refund-child')
    event = SimpleNamespace(_heartbeat_state=snapshot, _heartbeat_resolved_session_id='refund-child', _heartbeat_execution_started=False)
    settle_heartbeat_attempt(event, manager)
    assert hb.load_heartbeat('refund-parent') is None
    assert hb.load_heartbeat('refund-child').fire_count == 0
    replacement = hb.HeartbeatManager('refund-child')
    replacement.set('replacement', 900, include_time=True)
    assert replacement.due_prompt(now=clock.now + 900)
    before = replacement.state.to_json()
    settle_heartbeat_attempt(event, manager)
    assert hb.load_heartbeat('refund-child').to_json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('busy_kind', ['running', 'adapter', 'queued'])
async def test_windowed_priority_and_other_thread_isolation(monkeypatch, busy_kind):
    clock = SimpleNamespace(now=epoch('2026-09-30T07:14:59-04:00'))
    monkeypatch.setattr(hb, 'time', SimpleNamespace(time=lambda: clock.now))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    sources = [SessionSource(platform=Platform.TELEGRAM, chat_id=name, user_id='owner') for name in ('busy-thread', 'idle-thread')]
    keys = [runner._session_key_for_source(source) for source in sources]
    watch = {key: (source, key) for key, source in zip(keys, sources)}
    await runner._warm_goals_session_db('test')
    for key in keys:
        mgr = hb.HeartbeatManager(key)
        state = mgr.set('question', 900, windows=['07:15-09:00'], timezone='America/New_York')
        state.created_at = 1
        hb.save_heartbeat(key, state)
    seen = []

    async def handler(event):
        seen.append(event.source.chat_id)
        event._heartbeat_execution_started = True

    adapter.set_message_handler(handler)
    await runner._heartbeat_poll_once(watch)
    assert not adapter._background_tasks and seen == []  # closed window never calls handler
    busy = {'running': runner._running_agents, 'adapter': adapter._active_sessions,
            'queued': adapter._pending_messages}[busy_kind]
    real_user = MessageEvent(text='real user', source=sources[0])
    busy[keys[0]] = real_user
    clock.now = epoch('2026-09-30T07:15:00-04:00')
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    await asyncio.sleep(0)
    assert seen == ['idle-thread']
    assert busy[keys[0]] is real_user
    assert hb.HeartbeatManager(keys[0]).state.fire_count == 0
    assert hb.HeartbeatManager(keys[1]).state.fire_count == 1
    busy.pop(keys[0])
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    await asyncio.sleep(0)
    assert seen == ['idle-thread', 'busy-thread']
    assert hb.HeartbeatManager(keys[0]).state.fire_count == 1
