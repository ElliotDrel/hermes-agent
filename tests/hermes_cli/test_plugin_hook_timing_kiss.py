"""Routing snapshot, scalar privacy and native owner attribution regressions."""
import asyncio
from contextlib import closing
import json
import queue
import sqlite3
import threading

import pytest

from hermes_cli import plugins
from hermes_cli import plugin_hook_timing as timing


class MemoryWriter:
    def __init__(self):
        self.records = []

    def offer(self, record):
        self.records.append(record)


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, 'load_config_readonly', lambda: {'plugins': {'hook_timing': {'enabled': True}}})
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: 0)
    mgr = plugins.PluginManager(scope_key=str(tmp_path))
    mgr._hook_timing_writer = MemoryWriter()
    return mgr


def idle_writer(home):
    """Deterministic writer-owned checks without a forever-running daemon."""
    writer = object.__new__(timing.Writer)
    writer.home = home
    writer.queue = queue.Queue(maxsize=1024)
    writer.dropped = writer.failed = writer.excluded = writer.written = 0
    writer._routes = {}
    writer._routes_expire = writer._routes_retry = writer._status_due = 0.0
    return writer


def route_db(home, entries):
    with closing(sqlite3.connect(home / 'state.db')) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS gateway_routing(entry_json TEXT)')
        db.execute('DELETE FROM gateway_routing')
        db.executemany('INSERT INTO gateway_routing VALUES (?)', [(json.dumps(entry),) for entry in entries])


def route(session='s', guild=timing.APPROVED_GUILD):
    return {'session_id': session, 'origin': {'platform': 'discord', 'scope_id': guild}}


def test_route_snapshot_reused_then_rotated_and_conflicting_fail_closed(tmp_path, monkeypatch):
    route_db(tmp_path, [route(), route()])
    writer = idle_writer(tmp_path)
    clock = [10.0]
    monkeypatch.setattr(timing.time, 'monotonic', lambda: clock[0])
    connect = sqlite3.connect
    reads = []

    def counted(*args, **kwargs):
        reads.append(threading.get_ident())
        return connect(*args, **kwargs)

    monkeypatch.setattr(timing.sqlite3, 'connect', counted)
    record = {'hook': 'post_llm_call', 'session_id': 's'}
    assert writer._scoped(record)
    for _ in range(30):
        assert writer._scoped(record)
    assert len(reads) == 1
    route_db(tmp_path, [route(), route(guild='other')])
    clock[0] = 10.5
    assert writer._scoped(record)  # Bounded snapshot window, not per-event freshness.
    clock[0] = 11.0
    assert not writer._scoped(record)
    route_db(tmp_path, [route('new')])
    clock[0] = 12.0
    assert not writer._scoped(record)
    assert writer._scoped(dict(record, session_id='new'))
    assert not writer._scoped(dict(record, session_id='unknown', guild_id=timing.APPROVED_GUILD))
    assert writer._scoped(dict(record, hook='subagent_stop', parent_session_id='new'))


@pytest.mark.parametrize('failure', ['sqlite', 'malformed', 'missing'])
def test_expired_route_errors_clear_trust_and_throttle_retries(tmp_path, monkeypatch, failure):
    route_db(tmp_path, [route()])
    writer = idle_writer(tmp_path)
    clock = [10.0]
    monkeypatch.setattr(timing.time, 'monotonic', lambda: clock[0])
    record = {'session_id': 's'}
    assert writer._scoped(record)
    if failure == 'sqlite':
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('PRIVATE_SQL_ERROR')
        monkeypatch.setattr(timing.sqlite3, 'connect', fail)
    elif failure == 'malformed':
        with closing(sqlite3.connect(tmp_path / 'state.db')) as db, db:
            db.execute("UPDATE gateway_routing SET entry_json = '{PRIVATE_BAD_JSON'")
    else:
        (tmp_path / 'state.db').unlink()
    clock[0] = 11.0
    assert not writer._scoped(record)
    assert writer._routes == {} and writer.failed == 1
    for _ in range(30):
        assert not writer._scoped(record)
    assert writer.failed == 1
    clock[0] = 12.0
    assert not writer._scoped(record)
    assert writer.failed == 2


def test_slow_refresh_does_not_grant_expired_snapshot(tmp_path, monkeypatch):
    route_db(tmp_path, [route()])
    writer = idle_writer(tmp_path)
    clock = [10.0]
    monkeypatch.setattr(timing.time, 'monotonic', lambda: clock[0])
    connect = sqlite3.connect

    def slow(*args, **kwargs):
        clock[0] += 2
        return connect(*args, **kwargs)

    monkeypatch.setattr(timing.sqlite3, 'connect', slow)
    assert not writer._scoped({'session_id': 's'})


def test_status_cadence_under_load_idle_and_failure(tmp_path, monkeypatch):
    writer = idle_writer(tmp_path)
    clock = [10.0]
    calls = []
    monkeypatch.setattr(timing.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(writer, '_status', lambda: calls.append(clock[0]))
    for now in [10.0, 10.1, 10.9, 11.0, 11.2, 12.0]:
        clock[0] = now
        writer._maybe_status()
    assert calls == [10.0, 11.0, 12.0]

    def fail():
        raise OSError('PRIVATE_STATUS')

    monkeypatch.setattr(writer, '_status', fail)
    clock[0] = 13.0
    for _ in range(30):
        writer._maybe_status()
    assert writer.failed == 1


@pytest.mark.parametrize('value', ['', ' body ', 'private\ntext', 'secret=value', '{"secret":1}',
                                  'https://secret', '/private/path', '\\private', '\ud800', 'é',
                                  'a' * 257, None, 123, True, {'secret': 'value'}])
def test_malformed_identifiers_omitted_without_changing_payload(manager, value):
    seen = []
    manager._hooks['subagent_stop'] = [lambda **kwargs: seen.append(kwargs)]
    manager.invoke_hook('subagent_stop', session_id=value, parent_session_id='parent', turn_id=value)
    assert seen[0]['session_id'] is value
    assert timing.identifier(value) is None
    assert all('session_id' not in r and 'turn_id' not in r for r in manager._hook_timing_writer.records)


@pytest.mark.parametrize('value', ['s', '20261009_120000_a1b2c3', 'a-b_c.d',
                                  'session:123e4567-e89b-12d3-a456-426614174000:abcdef12', 'a' * 256])
def test_raw_native_tokens_preserved(manager, value):
    manager.invoke_hook('subagent_stop', session_id=value, turn_id=value)
    event = manager._hook_timing_writer.records[0]
    assert event['session_id'] == event['turn_id'] == value


def test_queue_identifier_projection_and_no_caller_routing_io(tmp_path, monkeypatch):
    writer = idle_writer(tmp_path)
    monkeypatch.setattr(timing.sqlite3, 'connect', lambda *a, **kw: pytest.fail('routing on caller'))
    writer.offer({'session_id': 's', 'turn_id': 'private text', 'parent_session_id': '\ud800',
                  'plugin_id': '../private/path', 'callback_id': '0xdead', 'args': {'secret': 'body'}})
    assert writer.queue.get_nowait() == {'session_id': 's', 'plugin_id': 'unknown'}


@pytest.mark.parametrize('mode', ['sync', 'async'])
@pytest.mark.parametrize('owner', ['stable-plugin', 'food/nutrition', 'bad owner'])
def test_native_ledger_plugin_identity_disposal_reload_and_unknown(manager, mode, owner):
    def invoke():
        if mode == 'async':
            return asyncio.run(manager.ainvoke_hook('subagent_stop', session_id='s'))
        return manager.invoke_hook('subagent_stop', session_id='s')

    def register(owner, callback):
        context = plugins.PluginContext(plugins.PluginManifest(name='Display Name', key=owner), manager)
        return context.register_hook('subagent_stop', callback)

    def cb(**kwargs):
        return 42

    def waits():
        return [r for r in manager._hook_timing_writer.records if r['event'] == 'callback_wait']

    expected = owner if owner != 'bad owner' else 'unknown'
    handle = register(owner, cb)
    assert handle.callback is cb and handle.plugin_key == owner
    manager._hooks['subagent_stop'].append(lambda **kw: 8)  # Config/shell bypass has no owner.
    assert invoke() == [42, 8]
    assert [(r['plugin_id'], r['callback_index']) for r in waits()[-2:]] == [(expected, 0), ('unknown', 1)]
    assert all('callback_id' not in r for r in waits())
    handle.dispose()
    assert invoke() == [8]
    assert waits()[-1]['plugin_id'] == 'unknown'
    register(owner, lambda **kw: 42)
    assert invoke() == [8, 42]
    assert waits()[-1]['plugin_id'] == expected
    # The same object registered by two owners is ambiguous, never a guessed winner.
    register('other-plugin', manager._hooks['subagent_stop'][-1])
    assert invoke() == [8, 42, 42]
    assert all(r['plugin_id'] == 'unknown' for r in waits()[-3:])


def test_dispatch_and_writer_route_reads_only_on_daemon(manager, tmp_path, monkeypatch):
    route_db(tmp_path, [route()])
    caller = threading.get_ident()
    connect = sqlite3.connect
    readers = []

    def observed(*args, **kwargs):
        readers.append(threading.get_ident())
        return connect(*args, **kwargs)

    monkeypatch.setattr(timing.sqlite3, 'connect', observed)
    writer = timing.Writer(tmp_path)
    manager._hook_timing_writer = writer
    manager._hooks['subagent_stop'] = [lambda **kw: 42]
    for _ in range(5):
        assert manager.invoke_hook('subagent_stop', session_id='s') == [42]
    writer.queue.join()
    assert readers == [writer.thread.ident] and writer.thread.ident != caller
    output = tmp_path / 'runtime/hermes-timing/plugin-hook-events.jsonl'
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 15
    assert all(r['session_id'] == 's' for r in rows)
