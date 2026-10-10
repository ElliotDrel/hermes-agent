"""Offline native-dispatch acceptance for opt-in hook timing."""
import asyncio
import contextvars
import json
from pathlib import Path
import queue
import sqlite3
import threading
import time

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


def records(manager, event):
    return [r for r in manager._hook_timing_writer.records if r['event'] == event]


@pytest.mark.parametrize('hook', ['pre_llm_call', 'post_llm_call', 'pre_tool_call',
                                 'on_session_finalize', 'on_session_reset', 'subagent_stop',
                                 'transform_llm_output', 'pre_api_request'])
def test_native_payload_result_order_and_thread_invariance(manager, monkeypatch, hook):
    payload = {'session_id': 's', 'turn_id': 't', 'args': {'secret': 'DO_NOT_RECORD'},
               'request': {'messages': [{'role': 'user', 'content': 'PRIVATE_BODY'}]},
               'duration_ms': 900000, 'parent_session_id': 'p', 'parent_turn_id': 'pt'}
    observed = []
    result = {'context': 'PRIVATE_CONTEXT'}
    def first(**kwargs):
        observed.append((kwargs, threading.get_ident()))
        return result
    def second(session_id):
        observed.append((session_id, threading.get_ident()))
    manager._hooks[hook] = [first, second]
    baseline = manager.invoke_hook.__wrapped__(manager, hook, **payload)
    original = list(observed)
    observed.clear()
    assert manager.invoke_hook(hook, **payload) == baseline
    assert observed == original
    assert baseline[0] is result
    assert payload['request']['messages'][0]['content'] == 'PRIVATE_BODY'
    assert len(records(manager, 'callback_execution')) == 2
    waits = records(manager, 'callback_wait')
    assert [r['callback_index'] for r in waits] == [0, 1]
    assert all(r['gap_ms'] >= 0 for r in waits)
    assert all(r['duration_ms'] < 900000 for r in waits)
    raw = json.dumps(manager._hook_timing_writer.records)
    assert not any(v in raw for v in ['DO_NOT_RECORD', 'PRIVATE_BODY', 'PRIVATE_CONTEXT'])
    assert waits[0]['session_id'] == timing.identifier('s')
    assert waits[0]['parent_turn_id'] == timing.identifier('pt')


def test_bounded_timeout_late_completion_suppression_and_cap(manager, monkeypatch):
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: .02)
    hold = threading.Event()
    marker = contextvars.ContextVar('marker', default=None)
    marker.set('caller')
    seen = []
    def cb(**kwargs):
        seen.append((marker.get(), threading.get_ident()))
        hold.wait(3)
        return 'LATE_PRIVATE_RESULT'
    manager._hooks['pre_tool_call'] = [cb]
    try:
        result = manager.invoke_hook('pre_tool_call', session_id='s', turn_id='one')
        assert result[0]['action'] == 'block'
        assert manager.invoke_hook('pre_tool_call', session_id='s', turn_id='one') == result
        # Expire only native backoff; abandoned cap remains native and unchanged.
        for i in range(2):
            manager._hook_timeout_suppressed_until.clear()
            assert manager.invoke_hook('pre_tool_call', session_id='s', turn_id=str(i))[0]['action'] == 'block'
        manager._hook_timeout_suppressed_until.clear()
        assert manager.invoke_hook('pre_tool_call', session_id='s', turn_id='cap') == result
        assert len(seen) == 3
        assert all(value == 'caller' and tid != threading.get_ident() for value, tid in seen)
        outcomes = [r['outcome'] for r in records(manager, 'callback_wait')]
        assert outcomes == ['timeout', 'skipped_suppressed_or_running', 'timeout', 'timeout', 'skipped_worker_cap']
        assert not records(manager, 'callback_execution')
    finally:
        hold.set()
    deadline = time.monotonic() + 2
    while manager._hook_running_callbacks and time.monotonic() < deadline:
        time.sleep(.005)
    execution = records(manager, 'callback_execution')
    assert len(execution) == 3
    first_wait = records(manager, 'callback_wait')[0]
    first_exec = next(r for r in execution if r['dispatch_id'] == first_wait['dispatch_id'])
    assert first_exec['end_ns'] > first_wait['end_ns']
    assert first_exec['duration_ms'] > first_wait['duration_ms']
    assert first_exec['admission_ms'] >= 0
    assert 'LATE_PRIVATE_RESULT' not in json.dumps(execution)


def test_concurrent_calls_do_not_collapse(manager, monkeypatch):
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: 1)
    barrier = threading.Barrier(2)
    def cb(turn_id):
        barrier.wait(2)
        return turn_id
    manager._hooks['post_tool_call'] = [cb]
    results = []
    threads = [threading.Thread(target=lambda v=v: results.append(manager.invoke_hook('post_tool_call', turn_id=v, session_id='s')))
               for v in ('a', 'b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert sorted(results) == [['a'], ['b']]
    assert len({r['dispatch_id'] for r in records(manager, 'dispatch')}) == 2
    assert all(r['outcome'] == 'ok' for r in records(manager, 'callback_wait'))


def test_errors_and_writer_failure_do_not_change_results(manager, monkeypatch):
    def bad(**kwargs):
        raise ValueError('PRIVATE_ERROR_TEXT')
    manager._hooks['pre_tool_call'] = [bad, lambda **kw: {'action': 'allow'}]
    expected = manager.invoke_hook.__wrapped__(manager, 'pre_tool_call', args={'key': 'PRIVATE_ARG'}, session_id='s')
    assert manager.invoke_hook('pre_tool_call', args={'key': 'PRIVATE_ARG'}, session_id='s') == expected
    assert records(manager, 'callback_wait')[0]['outcome'] == 'error'
    assert 'PRIVATE_ERROR_TEXT' not in json.dumps(manager._hook_timing_writer.records)
    def broken(record):
        raise OSError('PRIVATE_WRITE_FAILURE')
    monkeypatch.setattr(manager._hook_timing_writer, 'offer', broken)
    assert manager.invoke_hook('pre_tool_call', args={'key': 'PRIVATE_ARG'}, session_id='s') == expected


@pytest.mark.asyncio
async def test_async_loop_order_timeout_and_result_invariance(manager, monkeypatch):
    loop = asyncio.get_running_loop()
    seen = []
    async def first(request):
        assert asyncio.get_running_loop() is loop
        await asyncio.sleep(0)
        seen.append(request)
        return request
    def second(**kwargs):
        return {'action': 'allow'}
    request = {'messages': [{'role': 'user', 'content': 'PRIVATE_REQUEST'}]}
    manager._hooks['pre_llm_call'] = [first, second]
    baseline = await manager.ainvoke_hook.__wrapped__(manager, 'pre_llm_call', request=request)
    assert await manager.ainvoke_hook('pre_llm_call', session_id='s', request=request) == baseline
    assert seen == [request, request]
    assert len(records(manager, 'callback_execution')) == 2
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: .01)
    cancelled = asyncio.Event()
    async def slow(**kwargs):
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.set()
    manager._hooks['pre_tool_call'] = [slow]
    assert (await manager.ainvoke_hook('pre_tool_call', session_id='s'))[0]['action'] == 'block'
    assert cancelled.is_set()
    assert records(manager, 'callback_wait')[-1]['outcome'] == 'timeout'
    assert records(manager, 'callback_execution')[-1]['outcome'] == 'cancelled'
    assert 'PRIVATE_REQUEST' not in json.dumps(manager._hook_timing_writer.records)


def test_worker_start_failure(manager, monkeypatch):
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: .01)
    def fail_start(self):
        raise RuntimeError('PRIVATE_OS_ERROR')
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    manager._hooks['post_tool_call'] = [lambda **kwargs: 7]
    assert manager.invoke_hook('post_tool_call', session_id='s') == []
    assert records(manager, 'callback_wait')[0]['outcome'] == 'skipped_worker_start'
    assert manager._hook_running_callbacks == {}


def test_default_disabled_and_config_rollback(manager, monkeypatch):
    manager._hooks['subagent_stop'] = [lambda **kwargs: 42]
    for config in ({}, {'plugins': {'hook_timing': {'enabled': False}}}, {'plugins': {'hook_timing': {'enabled': 'true'}}}):
        monkeypatch.setattr(plugins, 'load_config_readonly', lambda: config)
        assert manager.invoke_hook('subagent_stop', session_id='s') == [42]
    assert not manager._hook_timing_writer.records


def test_real_writer_verified_native_route_privacy_drop_and_failure(tmp_path, monkeypatch):
    from datetime import datetime
    from gateway.session import SessionEntry, SessionSource
    from gateway.config import Platform
    source = SessionSource(platform=Platform.DISCORD, chat_id='c', guild_id=timing.APPROVED_GUILD)
    entry = SessionEntry(session_key='route', session_id='s', created_at=datetime.now(), updated_at=datetime.now(), origin=source)
    with sqlite3.connect(tmp_path / 'state.db') as db:
        db.execute('CREATE TABLE gateway_routing(entry_json TEXT)')
        db.execute('INSERT INTO gateway_routing VALUES (?)', (json.dumps(entry.to_dict()),))
    writer = timing.Writer(tmp_path, capacity=2)
    record = {'schema': 1, 'event': 'dispatch', 'hook': 'subagent_stop',
              'session_id': timing.identifier('child'), 'parent_session_id': timing.identifier('s'),
              'args': {'secret': 'PRIVATE_SECRET'}, 'error': 'PRIVATE_ERROR'}
    writer.offer(record)
    writer.queue.join()
    output = tmp_path / 'runtime/hermes-timing/plugin-hook-events.jsonl'
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]['scope'] == 'verified_discord_guild'
    assert 'PRIVATE' not in output.read_text()
    writer.offer(dict(record, parent_session_id=timing.identifier('unknown')))
    writer.queue.join()
    assert writer.excluded == 1
    # A contradictory native route must exclude even when one route matches.
    with sqlite3.connect(tmp_path / 'state.db') as db:
        db.execute('INSERT INTO gateway_routing VALUES (?)', (json.dumps({'session_id': 's', 'origin': {'platform': 'discord', 'guild_id': 'other'}}),))
    assert not writer._scoped(record)
    # Queue failure/drop without a consumer races neither the test nor real writer.
    stopped = object.__new__(timing.Writer)
    stopped.queue = queue.Queue(maxsize=1)
    stopped.dropped = 0
    stopped.offer(record)
    stopped.offer(record)
    assert stopped.dropped == 1 and stopped.queue.qsize() == 1
    assert set(stopped.queue.get_nowait()) <= timing._FIELDS
    monkeypatch.setattr(writer, '_write', lambda record: (_ for _ in ()).throw(OSError('PRIVATE_FAILURE')))
    writer.offer(record)
    writer.queue.join()
    assert writer.failed >= 1
    writer._status()
    status = json.loads(next(output.parent.glob('plugin-hook-status-*.json')).read_text())
    assert status['failed'] >= 1
    assert 'PRIVATE' not in json.dumps(status)


def test_real_config_set_activation_disable_and_unidentified(tmp_path, monkeypatch):
    from hermes_cli.config import set_config_value, load_config_readonly
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    set_config_value('plugins.hook_timing.enabled', 'true')
    assert load_config_readonly()['plugins']['hook_timing']['enabled'] is True
    manager = plugins.PluginManager(scope_key=str(tmp_path))
    manager._hook_timing_writer = MemoryWriter()
    manager._hooks['subagent_stop'] = [lambda **kw: 3]
    assert manager.invoke_hook('subagent_stop', parent_session_id='s') == [3]
    assert records(manager, 'dispatch')
    manager._hook_timing_writer.records.clear()
    assert manager.invoke_hook('subagent_stop', guild_id=timing.APPROVED_GUILD) == [3]
    assert not records(manager, 'dispatch')
    set_config_value('plugins.hook_timing.enabled', 'false')
    assert load_config_readonly()['plugins']['hook_timing']['enabled'] is False
    assert manager.invoke_hook('subagent_stop', parent_session_id='s') == [3]
    assert not records(manager, 'dispatch')


@pytest.mark.asyncio
async def test_async_failure_cancellation_and_nested_dispatch(manager):
    async def bad(**kw):
        raise ValueError('PRIVATE_ERROR')
    manager._hooks['pre_llm_call'] = [bad, lambda **kw: 5]
    assert await manager.ainvoke_hook('pre_llm_call', session_id='s') == [5]
    assert records(manager, 'callback_wait')[0]['outcome'] == 'error'
    entered = asyncio.Event()
    async def wait(**kw):
        entered.set()
        await asyncio.sleep(10)
    manager._hooks['post_llm_call'] = [wait]
    task = asyncio.create_task(manager.ainvoke_hook('post_llm_call', session_id='s'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records(manager, 'dispatch')[-1]['outcome'] == 'aborted'
    assert timing._current.get() is None
    manager._hooks['on_session_end'] = [lambda **kw: 8]
    def nested(**kw):
        return manager.invoke_hook('on_session_end', session_id='s')
    manager._hooks['subagent_stop'] = [nested]
    assert manager.invoke_hook('subagent_stop', session_id='s') == [[8]]
    assert len({r['dispatch_id'] for r in records(manager, 'dispatch')[-2:]}) == 2


def test_sync_baseexception_propagates_and_finishes_span(manager):
    def interrupt(**kw):
        raise KeyboardInterrupt()
    manager._hooks['subagent_stop'] = [interrupt]
    with pytest.raises(KeyboardInterrupt):
        manager.invoke_hook('subagent_stop', parent_session_id='s')
    assert records(manager, 'callback_wait')[0]['outcome'] == 'aborted'
    assert records(manager, 'callback_execution')[0]['outcome'] == 'error'
    assert records(manager, 'dispatch')[0]['outcome'] == 'aborted'
    assert timing._current.get() is None
