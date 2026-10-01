"""Opt-in daily windows preserve legacy payloads and elapsed scheduling."""
import json
import queue
import os
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime

import pytest

from hermes_cli import heartbeat as hb


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


def test_prefix_parser_preserves_prompt():
    interval, prompt, options = hb.parse_heartbeat_spec(
        'every 15m --windows 07:15-09:00,23:00-00:30 --timezone America/New_York --include-time '
        'Ask "which set?"\nKeep --windows literal  ')
    assert interval == 900
    assert prompt == 'Ask "which set?"\nKeep --windows literal  '
    assert options == dict(windows=['07:15-09:00', '23:00-00:30'], timezone='America/New_York', include_time=True)
    assert hb.parse_heartbeat_spec('10m literal --include-time text')[1] == 'literal --include-time text'
    assert hb.parse_heartbeat_spec('10m -- --windows literal')[1] == '--windows literal'


@pytest.mark.parametrize('spec', [
    '10m --windows p', '10m --windows 24:00-09:00 p', '10m --windows 09:00-09:00 p',
    '10m --windows 07:00-09:00,08:00-10:00 --timezone UTC p',
    '10m --windows 07:00-09:00 p', '10m --timezone Not/AZone p',
    '10m --timezone UTC --timezone UTC p', '10m --include-time --include-time p',
    '10m --unknown p', '10m --windows 07:00-09:00, --timezone UTC p',
    '10m --include-time', '10m --windows 07:00-09:00 --timezone UTC',
])
def test_bad_options_rejected(spec):
    with pytest.raises(ValueError):
        hb.parse_heartbeat_spec(spec)


@pytest.mark.parametrize('text,eligible', [
    ('2026-09-30T07:14:59-04:00', False), ('2026-09-30T07:15:00-04:00', True),
    ('2026-09-30T09:00:59-04:00', True), ('2026-09-30T09:01:00-04:00', False),
    ('2026-09-30T23:00:00-04:00', True), ('2026-10-01T00:30:59-04:00', True),
    ('2026-10-01T00:31:00-04:00', False),
])
def test_windows_include_ending_minute(text, eligible):
    state = hb.HeartbeatState('ask', 900, created_at=1, windows=['07:15-09:00', '23:00-00:30'], timezone='America/New_York')
    assert state.is_due(epoch(text)) is eligible


@pytest.mark.parametrize('text', ['2026-11-01T01:30:00-04:00', '2026-11-01T01:30:00-05:00'])
def test_dst_fold_uses_actual_offset(text):
    state = hb.HeartbeatState('ask', 900, created_at=1, windows=['01:00-01:45'], timezone='America/New_York', include_time=True)
    assert state.is_due(epoch(text))
    assert text in state.render_prompt(now=epoch(text))
    assert 'America/New_York' in state.render_prompt(now=epoch(text))


def test_dst_gap_and_elapsed_interval():
    state = hb.HeartbeatState('ask', 900, windows=['01:00-03:30'], timezone='America/New_York', created_at=epoch('2026-03-08T01:55:00-05:00'))
    assert not state.is_due(epoch('2026-03-08T03:05:00-04:00'))
    assert state.is_due(epoch('2026-03-08T03:10:00-04:00'))


def test_real_persistence_refund_restart_and_compression(monkeypatch):
    now = epoch('2026-09-30T08:01:00-04:00')
    monkeypatch.setattr(hb.time, 'time', lambda: now)
    manager = hb.HeartbeatManager('window-parent')
    state = manager.set('specific question  ', 900, windows=['07:15-09:00'], timezone='America/New_York', include_time=True)
    state.created_at = now - 3600
    hb.save_heartbeat(manager.session_id, state)
    assert hb.HeartbeatState.from_json(state.to_json()) == state
    assert manager.due_prompt(now)
    assert manager.due_prompt(now) is None
    assert manager.abandon_fire()
    assert not manager.abandon_fire()
    restored = hb.HeartbeatManager('window-parent')
    assert restored.state.fire_count == 0 and restored.state.prompt == 'specific question  '
    assert hb.migrate_heartbeat_to_session('window-parent', 'window-child')
    child = hb.HeartbeatManager('window-child')
    assert child.state.windows == ['07:15-09:00'] and child.state.include_time
    assert hb.load_heartbeat('window-parent') is None
    assert child.due_prompt(now) is not None
    assert child.due_prompt(now + 900) is not None  # 08:16, not quarter-hour aligned
    assert child.due_prompt(epoch('2026-09-30T12:00:00-04:00')) is None
    assert child.due_prompt(epoch('2026-10-01T07:15:00-04:00')) is not None
    assert child.state.fire_count == 3  # closed-window intervals never become a backlog


@pytest.mark.parametrize('condition', ['open', 'closed', 'queued-user', 'reset'])
def test_cli_preparation_drops_delayed_or_preempted_ticks(monkeypatch, condition):
    from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin

    clock = SimpleNamespace(now=epoch('2026-09-30T09:00:05-04:00'))
    monkeypatch.setattr(hb, 'time', SimpleNamespace(time=lambda: clock.now))
    mgr = hb.HeartbeatManager('cli-window')
    state = mgr.set('question', 900, windows=['07:15-09:00'], timezone='America/New_York', include_time=True)
    state.created_at = clock.now - 3600
    hb.save_heartbeat(mgr.session_id, state)
    assert mgr.due_prompt()
    tick = hb.HeartbeatTick(mgr)
    cli = SimpleNamespace(session_id=mgr.session_id, _pending_input=queue.Queue())
    if condition == 'queued-user':
        cli._pending_input.put('real user')
    elif condition == 'reset':
        cli.session_id = 'new-session'
    clock.now = epoch('2026-09-30T09:01:00-04:00' if condition == 'closed' else '2026-09-30T09:00:59-04:00')
    payload, voice, seeded = CLIProcessNotificationsMixin._tui_unwrap_input(cli, tick)
    assert not voice and not seeded
    if condition == 'open':
        assert '2026-09-30T09:00:59-04:00' in payload
        assert '09:00:05' not in payload
    else:
        assert payload is None
        assert hb.HeartbeatManager(mgr.session_id).state.fire_count == 0
    if condition == 'queued-user':
        assert cli._pending_input.get_nowait() == 'real user'


def test_old_record_and_legacy_payload_bytes():
    old = dict(prompt='legacy', interval_seconds=600, status='active', created_at=1, last_fired_at=0, fire_count=0)
    state = hb.HeartbeatState.from_json(json.dumps(old))
    assert state.windows == [] and state.timezone is None and not state.include_time
    assert state.render_prompt() == hb.HEARTBEAT_PROMPT_TEMPLATE.format(interval='10m', prompt='legacy')
    assert json.loads(state.to_json()) == old  # no new default fields in old records


def test_fresh_process_loads_windowed_persistence():
    manager = hb.HeartbeatManager('fresh-process-window')
    manager.set('retained question', 900, windows=['23:00-00:30'], timezone='America/New_York', include_time=True)
    # The test fixture sets HERMES_HOME to temp storage. The child opens the real database afresh.
    probe = subprocess.run([sys.executable, '-c',
        "from hermes_cli.heartbeat import load_heartbeat; print(load_heartbeat('fresh-process-window').to_json())"],
        env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=True)
    loaded = hb.HeartbeatState.from_json(probe.stdout.strip())
    assert loaded == manager.state


@pytest.mark.parametrize('options', [dict(windows=None), dict(windows=['']), dict(include_time='false'),
                                     dict(timezone=''), dict(windows=['07:00-09:00'])])
def test_invalid_persisted_options_fail_closed(options):
    with pytest.raises(ValueError):
        hb.HeartbeatState.from_json(json.dumps(dict(prompt='p', interval_seconds=900, **options)))


@pytest.mark.parametrize('control', ['replacement', 'pause-resume'])
def test_cli_stale_control_drops_tick_without_rewinding_current_state(monkeypatch, control):
    clock = SimpleNamespace(now=epoch('2026-09-30T08:01:00-04:00'))
    monkeypatch.setattr(hb, 'time', SimpleNamespace(time=lambda: clock.now))
    mgr = hb.HeartbeatManager('cli-control-' + control)
    state = mgr.set('old question', 900, include_time=True)
    state.created_at = clock.now - 3600
    state.last_fired_at = clock.now - 1800
    state.fire_count = 2
    hb.save_heartbeat(mgr.session_id, state)
    assert mgr.due_prompt()
    tick = hb.HeartbeatTick(mgr)
    clock.now += 60
    if control == 'replacement':
        mgr.set('replacement question', 900, include_time=True)
    else:
        mgr.pause()
        mgr.resume()
    current = mgr.state.to_json()
    assert tick.prepare(mgr.session_id) is None, 'controls must cancel the previously queued instruction'
    assert hb.load_heartbeat(mgr.session_id).to_json() == current, 'discard must not rewind replacement/resumed state'
    assert mgr.state.to_json() == current


def test_cli_timezone_only_uses_revalidated_queue_token(monkeypatch):
    from hermes_cli import cli_loops_mixin as loops
    clock = SimpleNamespace(now=epoch('2026-09-30T08:01:00-04:00'))
    monkeypatch.setattr(hb, 'time', SimpleNamespace(time=lambda: clock.now))
    manager = hb.HeartbeatManager('cli-timezone-only')
    state = manager.set('question', 900, timezone='UTC')
    state.created_at = clock.now - 3600
    hb.save_heartbeat(manager.session_id, state)
    cli = SimpleNamespace(_should_exit=False, _agent_running=False)
    class StopQueue(queue.Queue):
        def put(self, value):
            super().put(value)
            cli._should_exit = True
    cli._pending_input = StopQueue()
    cli._get_heartbeat_manager = lambda: manager
    monkeypatch.setattr(loops, 'time', SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(loops.threading, 'Thread', lambda **kwargs: SimpleNamespace(start=kwargs['target']))
    loops.CLILoopsMixin._start_heartbeat_watchdog(cli)
    assert isinstance(cli._pending_input.get_nowait(), hb.HeartbeatTick)
