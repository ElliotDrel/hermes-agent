"""Active-parent runtime notices end to end: a background process completes while the session's turn is
running. The real producer path (``_deliver_completion_notification``), session authority, SQLite FIFO
and agent turn loop hand it to that turn at its next tool boundary; only the model call is scripted.
The turn reads it once and the admission settles with it, an earlier queued input keeps it in its FIFO
place, and a notice the turn never read runs as its own turn from its original place."""
import asyncio
import json

import pytest
import pytest_asyncio

from agent.interrupt_control import InterruptControlMixin
from agent.prompt_builder import RUNTIME_NOTICE_OPEN
from gateway.run import GatewayRunner
from hermes_state_runtime import list_session_admissions
from tests.agent.test_runtime_notices import _script, _text
from tests.agent.test_turn_user_intervened import _DURING_TOOL, _loop_agent, _run
from tests.gateway.test_local_producer_admission import _Gateway
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def gateway(tmp_path, monkeypatch):
    gateway = _Gateway(tmp_path, monkeypatch)
    gateway.scripted, gateway.texts = [], []

    async def model(runner, message, context_prompt, history, source, session_id, **_kwargs):
        gateway.texts.append(message)
        if gateway.scripted:
            await gateway.scripted.pop(0)(session_id)
        return {'final_response': 'ack', 'messages': [], 'api_calls': 1, 'tools': [],
                'history_offset': len(history), 'session_id': session_id}
    monkeypatch.setattr(GatewayRunner, '_run_agent', model)
    await gateway.start()
    _DURING_TOOL.clear()
    try:
        yield gateway
    finally:
        _DURING_TOOL.clear()
        await gateway.stop()


def _adopt(gateway, session_id, agent):
    authority = gateway.primary
    owner = authority.logical_owner(session_id)
    authority.adopt_agent(owner, authority.executing[owner], agent)


def _real_turn(gateway, tool_rounds, requests, during_final=None):
    """The running turn: the real agent loop, adopted by the claim exactly as ``run_turn_runner`` does."""
    async def turn(session_id):
        agent = _loop_agent()
        _adopt(gateway, session_id, agent)
        result = await asyncio.to_thread(_run, agent, _script(tool_rounds, requests, during_final))
        gateway.results.append(result)
    gateway.results = []
    return turn


def _on_loop(loop, coro):
    """From the tool's worker thread: run ``coro`` on the gateway loop and wait for it."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(30)


def _ledger(gateway, sid):
    return [(row['request_id'], row['status'], row['outcome'], row['seq'])
            for row in list_session_admissions(gateway.primary.db, session_id=sid, pending_only=False)]


def _completion_id(process):
    return json.dumps(['completion', process, 1.0], separators=(',', ':'))


async def _submit(viewer, sid, input_id, text):
    reply = await viewer.dispatch({'id': input_id, 'method': 'prompt.submit', 'params': {
        'session_id': sid, 'input_id': input_id, 'text': text}})
    assert 'result' in reply, reply
    return reply['result']


@pytest.mark.asyncio
async def test_a_completion_for_the_running_turn_is_read_by_it_and_settles_with_it(gateway):
    primary, sid, loop = gateway.primary, gateway.launch.session_id, asyncio.get_running_loop()
    viewer = await gateway.watch(primary, sid)
    requests, acks, replays = [], [], []
    done = gateway.process_event(primary, sid, 'completion', process='proc_a', exit_code=0)

    def complete_while_the_tool_runs():
        acks.append(_on_loop(loop, gateway.runner._deliver_completion_notification('[proc_a finished]', done)))
        [row] = [r for r in list_session_admissions(primary.db, session_id=sid, pending_only=False)
                 if r['request_id'] == _completion_id('proc_a')]
        replays.append(_on_loop(loop, viewer.dispatch({'id': 'x', 'method': 'prompt.steer_queued', 'params': {
            'session_id': sid, 'admission_id': row['admission_id']}})))
    _DURING_TOOL.append(complete_while_the_tool_runs)
    gateway.scripted.append(_real_turn(gateway, 1, requests))
    await _submit(viewer, sid, 'human-1', 'build it')
    await gateway.settled(primary, sid, 2)

    assert acks == [True], 'the producer ACK still means committed input'
    assert replays[0].get('error', {}).get('data', {}).get('reason') == 'unsupported_control', \
        "a notice's transfer is never replayable as the user's steer"
    first, second = requests
    assert second[:len(first)] == first
    assert [m['role'] for m in second[len(first):]] == ['assistant', 'tool', 'user']
    assert _text(second[-1]).startswith(RUNTIME_NOTICE_OPEN) and '[proc_a finished]' in _text(second[-1])
    assert gateway.results[0]['user_intervened'] is False
    outcomes = {request_id: outcome for request_id, _, outcome, _ in _ledger(gateway, sid)}
    assert outcomes == {'human-1': 'completed', _completion_id('proc_a'): 'steered'}
    assert gateway.texts == ['build it'], 'a notice the running turn read never runs as its own turn'


@pytest.mark.asyncio
async def test_an_earlier_queued_human_message_keeps_the_completion_behind_it(gateway):
    primary, sid, loop = gateway.primary, gateway.launch.session_id, asyncio.get_running_loop()
    viewer = await gateway.watch(primary, sid)
    requests, acks = [], []
    done = gateway.process_event(primary, sid, 'completion', process='proc_b', exit_code=0)

    def human_then_completion():
        _on_loop(loop, _submit(viewer, sid, 'human-2', 'and then this'))
        acks.append(_on_loop(loop, gateway.runner._deliver_completion_notification('[proc_b finished]', done)))
    _DURING_TOOL.append(human_then_completion)
    gateway.scripted.append(_real_turn(gateway, 1, requests))
    await _submit(viewer, sid, 'human-1', 'build it')
    await gateway.settled(primary, sid, 3)

    assert acks == [True]
    assert not any('[proc_b finished]' in _text(m) for request in requests for m in request)
    ledger = _ledger(gateway, sid)
    assert [(request_id, outcome) for request_id, _, outcome, _ in ledger] == [
        ('human-1', 'completed'), ('human-2', 'completed'), (_completion_id('proc_b'), 'completed')]
    assert gateway.texts[:2] == ['build it', 'and then this'] and '[proc_b finished]' in gateway.texts[2]


@pytest.mark.asyncio
async def test_a_completion_the_turn_never_read_runs_from_its_own_queue_place(gateway):
    primary, sid, loop = gateway.primary, gateway.launch.session_id, asyncio.get_running_loop()
    viewer = await gateway.watch(primary, sid)
    requests, seqs = [], []
    done = gateway.process_event(primary, sid, 'completion', process='proc_c', exit_code=0)

    def complete_during_the_final_request():
        assert _on_loop(loop, gateway.runner._deliver_completion_notification('[proc_c finished]', done)) is True
        seqs.extend(seq for request_id, _, _, seq in _ledger(gateway, sid) if request_id == _completion_id('proc_c'))
    gateway.scripted.append(_real_turn(gateway, 0, requests, during_final=complete_during_the_final_request))
    await _submit(viewer, sid, 'human-1', 'answer briefly')
    await gateway.settled(primary, sid, 2)

    assert not any('[proc_c finished]' in _text(m) for request in requests for m in request)
    ledger = {request_id: (outcome, seq) for request_id, _, outcome, seq in _ledger(gateway, sid)}
    assert ledger[_completion_id('proc_c')] == ('completed', seqs[0]), 'never steered; same FIFO place'
    assert len(gateway.texts) == 2 and '[proc_c finished]' in gateway.texts[1]


class _CodexTurn(InterruptControlMixin):
    """A Codex app-server turn: its own model loop, with no typed nonhuman boundary."""
    api_mode = 'codex_app_server'

    def __init__(self):
        import threading
        self._pending_steer, self._pending_steer_lock = None, threading.Lock()

    def clear_interrupt(self, *args, **kwargs):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize('running', ['idle', 'codex'])
async def test_an_idle_session_or_a_codex_turn_keeps_the_native_next_turn(gateway, running):
    primary, sid = gateway.primary, gateway.launch.session_id
    viewer = await gateway.watch(primary, sid)
    done = gateway.process_event(primary, sid, 'completion', process='proc_d', exit_code=0)
    queued = []

    async def codex_turn(session_id):
        from agent import runtime_notices
        agent = _CodexTurn()
        runtime_notices.open_turn(agent)
        _adopt(gateway, session_id, agent)
        assert await gateway.runner._deliver_completion_notification('[proc_d finished]', done) is True
        queued.extend(status for request_id, status, _, _ in _ledger(gateway, sid) if request_id == _completion_id('proc_d'))
    if running == 'codex':
        gateway.scripted.append(codex_turn)
        await _submit(viewer, sid, 'human-1', 'go')
    else:
        assert await gateway.runner._deliver_completion_notification('[proc_d finished]', done) is True
    await gateway.settled(primary, sid, 2 if running == 'codex' else 1)

    if running == 'codex':
        assert queued == ['queued']
    outcomes = [(request_id, outcome) for request_id, _, outcome, _ in _ledger(gateway, sid)]
    assert (_completion_id('proc_d'), 'completed') in outcomes
    await async_wait_until(lambda: any('[proc_d finished]' in text for text in gateway.texts), timeout=30,
                           what='the completion ran as its own turn')
