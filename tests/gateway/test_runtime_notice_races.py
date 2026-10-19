"""Native SQLite transfer races; route admission is covered by test_runtime_notices."""
import asyncio
import json
import sqlite3
import threading

import pytest

from agent import runtime_notices
from gateway import session_runtime_notices as notices
from gateway.session_queued_controls import steer_transfers
from gateway.session_runtime_workers import mutation_tasks
from hermes_state_runtime import (
    RuntimeStoreError, admit_session_input, begin_runtime_epoch, claim_session_input,
    get_session_admission, recover_session_inputs, settle_session_input,
)
from tests.gateway.test_prompt_queued_transfer import BufferedAgent
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority
from tests.waiting import async_wait_until


@pytest.fixture
def claim(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    # This storage harness has no local/messaging route; production route coverage is separate.
    monkeypatch.setattr(notices, '_check_route', lambda *args: None)
    admit_session_input(db, epoch=authority.epoch, principal_id='human', session_id='s',
                        request_id='running', payload={'text': 'running'})
    row = claim_session_input(db, epoch=authority.epoch, session_id='s')
    authority.executing['s'] = row['generation']
    agent = BufferedAgent()
    runtime_notices.open_turn(agent)
    authority.adopt_agent('s', row['generation'], agent)
    try:
        yield db, authority, row, agent
    finally:
        db.close()


def _notice(db, authority, name):
    identity = json.dumps(['completion', name, 1.0], separators=(',', ':'))
    row = admit_session_input(
        db, epoch=authority.epoch, principal_id='automation:process', session_id='s',
        request_id=identity, payload={'text': name, 'local_automation_v1': {'identity': identity}})
    return row


def _settle(claim, outcome="completed"):
    db, authority, running, _ = claim
    transfers = steer_transfers(authority, 's', running['generation'])
    return settle_session_input(db, epoch=authority.epoch, admission_id=running['admission_id'],
                                generation=running['generation'], outcome=outcome, _transfers=transfers)


@pytest.mark.asyncio
async def test_an_earlier_transfer_keeps_a_concurrent_notice_in_its_original_fifo_place(claim, monkeypatch):
    db, authority, _, agent = claim
    first, second = _notice(db, authority, 'first'), _notice(db, authority, 'second')
    native, committed, release = notices.steer_session_input, threading.Event(), threading.Event()

    def hold_first_reply(*args, **kwargs):
        result = native(*args, **kwargs)
        if kwargs['admission_id'] == first['admission_id']:
            committed.set()
            assert release.wait(30), 'test must release the committed write'
        return result
    monkeypatch.setattr(notices, 'steer_session_input', hold_first_reply)
    pending = asyncio.create_task(notices.offer_runtime_notice(authority, REF, first['admission_id']))
    try:
        await async_wait_until(committed.is_set, timeout=30, what='first transfer committed')
        assert await notices.offer_runtime_notice(authority, REF, second['admission_id']) is False
        assert get_session_admission(db, admission_id=second['admission_id'])['status'] == 'queued'
    finally:
        release.set()
        await asyncio.wait_for(pending, 30)
    messages = [{'role': 'tool', 'content': 'result', 'tool_call_id': 't'}]
    assert runtime_notices.drain(agent, messages) is True
    assert 'first' in messages[-1]['content'] and 'second' not in messages[-1]['content']
    _settle(claim)
    next_row = claim_session_input(db, epoch=authority.epoch, session_id='s')
    assert (next_row['admission_id'], next_row['seq']) == (second['admission_id'], second['seq'])


@pytest.mark.asyncio
async def test_the_transfer_rechecks_stored_provenance_inside_its_transaction(claim, monkeypatch):
    db, authority, _, agent = claim
    notice = _notice(db, authority, 'result')
    native = notices.steer_session_input

    def revoke_before_write(*args, **kwargs):
        db._execute_write(lambda conn: conn.execute(
            'UPDATE session_admissions SET principal_id=? WHERE admission_id=?',
            ('human', notice['admission_id'])))
        return native(*args, **kwargs)
    monkeypatch.setattr(notices, 'steer_session_input', revoke_before_write)
    assert await notices.offer_runtime_notice(authority, REF, notice['admission_id']) is False
    assert get_session_admission(db, admission_id=notice['admission_id'])['status'] == 'queued'
    assert not runtime_notices.drain(agent, [{'role': 'tool', 'content': 'result'}])
    assert notice['admission_id'] not in authority.steer_transfers['s'][1]


@pytest.mark.asyncio
async def test_an_uncertain_committed_transfer_refunds_to_its_own_fifo_place(claim, monkeypatch):
    db, authority, _, agent = claim
    notice = _notice(db, authority, 'result')
    native = notices.steer_session_input

    def commit_then_fail(*args, **kwargs):
        native(*args, **kwargs)
        raise sqlite3.OperationalError('lost commit response')
    monkeypatch.setattr(notices, 'steer_session_input', commit_then_fail)
    with pytest.raises(sqlite3.OperationalError):
        await notices.offer_runtime_notice(authority, REF, notice['admission_id'])
    assert get_session_admission(db, admission_id=notice['admission_id'])['outcome'] == 'steering'
    assert not runtime_notices.drain(agent, [{'role': 'tool', 'content': 'result'}])
    _settle(claim)
    restored = get_session_admission(db, admission_id=notice['admission_id'])
    assert (restored['status'], restored['seq']) == ('queued', notice['seq'])


@pytest.mark.asyncio
async def test_cancelling_the_offer_cannot_orphan_the_inflight_transfer(claim, monkeypatch):
    db, authority, _, agent = claim
    notice = _notice(db, authority, 'result')
    native, entered, release = notices.steer_session_input, threading.Event(), threading.Event()

    def hold_write(*args, **kwargs):
        entered.set()
        assert release.wait(30), 'test must release the writer'
        return native(*args, **kwargs)
    monkeypatch.setattr(notices, 'steer_session_input', hold_write)
    task = asyncio.create_task(notices.offer_runtime_notice(authority, REF, notice['admission_id']))
    try:
        await async_wait_until(entered.is_set, timeout=30, what='transfer write started')
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await async_wait_until(lambda: not mutation_tasks(authority), timeout=30, what='shielded write finished')
    assert get_session_admission(db, admission_id=notice['admission_id'])['outcome'] == 'steering'
    assert not runtime_notices.drain(agent, [{'role': 'tool', 'content': 'result'}])
    _settle(claim)
    restored = get_session_admission(db, admission_id=notice['admission_id'])
    assert (restored['status'], restored['seq']) == ('queued', notice['seq'])


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before_transfer', 'after_transfer'])
async def test_an_owner_change_fences_transfers_and_never_replays_an_uncertain_notice(claim, when):
    db, authority, running, agent = claim
    notice = _notice(db, authority, 'result')
    if when == 'after_transfer':
        assert await notices.offer_runtime_notice(authority, REF, notice['admission_id']) is True
    new_epoch = begin_runtime_epoch(db, instance_id='replacement')
    if when == 'before_transfer':
        assert await notices.offer_runtime_notice(authority, REF, notice['admission_id']) is False
        assert not runtime_notices.drain(agent, [{'role': 'tool', 'content': 'result'}])
    recover_session_inputs(db, epoch=new_epoch)
    restored = get_session_admission(db, admission_id=notice['admission_id'])
    assert get_session_admission(db, admission_id=running['admission_id'])['status'] == 'unknown'
    if when == 'after_transfer':
        assert (restored['status'], restored['outcome']) == ('terminal', 'interrupted')
    else:
        assert (restored['status'], restored['seq']) == ('queued', notice['seq'])
    with pytest.raises(RuntimeStoreError, match='stale_epoch'):
        _settle(claim)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['sealed', 'claim_settled'])
async def test_a_turn_closing_between_commit_and_handoff_refunds_the_unread_notice(claim, monkeypatch, ending):
    db, authority, _, agent = claim
    notice = _notice(db, authority, 'result')
    native = notices.steer_session_input

    def close_after_commit(*args, **kwargs):
        committed = native(*args, **kwargs)
        if ending == 'sealed':
            runtime_notices.seal(agent)
        else:
            _settle(claim)
            with authority.sessions['s'].event_stream.lock:
                authority.executing.pop('s')
        return committed
    monkeypatch.setattr(notices, 'steer_session_input', close_after_commit)
    assert await notices.offer_runtime_notice(authority, REF, notice['admission_id']) is False
    restored = get_session_admission(db, admission_id=notice['admission_id'])
    assert (restored['status'], restored['seq']) == ('queued', notice['seq'])
    assert not runtime_notices.drain(agent, [{'role': 'tool', 'content': 'result'}])
    assert notice['admission_id'] not in authority.steer_transfers['s'][1]


@pytest.mark.asyncio
async def test_a_consumed_notice_in_a_failed_turn_is_interrupted_and_never_replayed(claim):
    db, authority, _, agent = claim
    notice = _notice(db, authority, 'result')
    assert await notices.offer_runtime_notice(authority, REF, notice['admission_id']) is True
    assert runtime_notices.drain(agent, [{'role': 'tool', 'content': 'result'}]) is True
    _settle(claim, outcome='failed')
    row = get_session_admission(db, admission_id=notice['admission_id'])
    assert (row['status'], row['outcome']) == ('terminal', 'interrupted')
    assert claim_session_input(db, epoch=authority.epoch, session_id='s') is None
