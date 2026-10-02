"""Offline joint integration: production executor, cached AIAgent and adapter.

Only provider credentials/config and wire I/O are faked. TurnRunner.run_sync,
agent cache lookup, AIAgent's tool execution, registry dispatch and adapter
background-turn lifecycle execute together, rather than separate to_thread tests.
"""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore
from tests.gateway.test_run_cleanup_progress import CleanupCaptureAdapter, _make_runner
from tests.gateway.test_session_messaging import source


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('destination_path', ['idle', 'fifo_followup'])
@pytest.mark.asyncio
async def test_production_worker_cached_agent_tool_dispatch_and_destination(monkeypatch, tmp_path, cache, destination_path):
    import gateway.run as gateway_run
    import run_agent
    import tools.tool_search as tool_search
    from agent.prompt_caching import strip_anthropic_cache_control
    real_agent = run_agent.AIAgent
    monkeypatch.setenv('HERMES_AGENT_TIMEOUT', '0')
    monkeypatch.setenv('HERMES_TOOL_PROGRESS_MODE', 'off')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(gateway_run, '_hermes_home', tmp_path)
    monkeypatch.setattr(gateway_run, '_resolve_gateway_model', lambda: 'test/model')
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: {
        'compression': {'enabled': False}, 'display': {'tool_progress': False},
        'memory': {'enabled': False}, 'agent': {'gateway_timeout': 0}})
    # Supported tool-search-off configuration keeps the actual registered schema
    # directly callable; no model_tools or dispatcher replacement is involved.
    monkeypatch.setattr(tool_search, 'load_config', lambda: SimpleNamespace(enabled='off'))
    adapter = CleanupCaptureAdapter(Platform.DISCORD)
    runner = _make_runner(adapter)
    runner.config = GatewayConfig()
    runner._init_lifecycle_state()
    runner._init_runtime_caches()
    runner._profile_adapters = {}
    runner._fallback_model = None
    runner._resolve_session_agent_runtime = lambda **kw: ('test/model', {
        'api_key': 'offline', 'base_url': 'https://openrouter.ai/api/v1'})
    runner._resolve_enabled_toolsets_for_source = lambda *args: []
    runner._get_proxy_url = lambda: None
    runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
    a, b = source('1'), source('2')
    sender = runner.session_store.get_or_create_session(a)
    target = runner.session_store.get_or_create_session(b)
    # Stabilize production registry generation before computing cached signatures.
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    import socket
    def refuse_network(*args, **kwargs):
        raise AssertionError('Offline integration attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', refuse_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse_network)
    requests, built, received, results, destination_runtime = [], [], [], [], []
    loop_thread = threading.get_ident()
    destination_done = asyncio.Event()

    def build_agent(**kwargs):
        kwargs.update(skip_memory=True, skip_context_files=True)
        agent = real_agent(**kwargs)
        agent._cached_system_prompt = 'offline stable system'
        agent._cached_system_prompt_static = 'offline stable system'
        agent._use_prompt_caching = cache
        agent._use_native_cache_layout = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._save_trajectory = lambda *a, **k: None
        agent._cleanup_task_resources = lambda *a, **k: None
        agent.client = MagicMock()
        stage = [0]
        def complete(**payload):
            assert threading.get_ident() != loop_thread
            requests.append((kwargs['session_id'], copy.deepcopy(payload)))
            current = strip_anthropic_cache_control(copy.deepcopy(payload['messages']))[-1]
            from gateway.session_messaging import _CURRENT
            runtime_turn = _CURRENT.get()
            assert runtime_turn is not None and runtime_turn.active
            if kwargs['session_id'] == target.session_id and 'Internal agent-origin message' in str(current['content']):
                destination_runtime.append((runtime_turn.chain, runtime_turn.hops))
            tool_calls = None
            if kwargs['session_id'] == sender.session_id and current['role'] == 'user':
                stage[0] += 1
                tool_calls = [SimpleNamespace(id=f'call-{stage[0]}', type='function',
                    function=SimpleNamespace(name='send_session_message', arguments=json.dumps({
                        'target_session_id': target.session_id, 'message': f'peer {stage[0]}'})))]
            message = SimpleNamespace(content=None if tool_calls else 'offline answer',
                tool_calls=tool_calls, reasoning=None, reasoning_content=None, reasoning_details=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message,
                finish_reason='tool_calls' if tool_calls else 'stop')], model='test/model', usage=None)
        agent.client.chat.completions.create.side_effect = complete
        built.append(agent)
        return agent
    class OfflineAgent(real_agent):
        def __new__(cls, **kwargs):
            return build_agent(**kwargs)
    monkeypatch.setattr(run_agent, 'AIAgent', OfflineAgent)
    monkeypatch.setattr('agent.process_bootstrap.OpenAI', MagicMock())

    async def destination_handler(event):
        received.append(event)
        try:
            if destination_path == 'fifo_followup':
                # Exercise the real pending slot/drain/recursive turn boundary.
                # The normal adapter still admits and owns this background task.
                runner._queue_or_replace_pending_event(target.session_key, event)
                result = await runner._run_agent('human destination', '', [], event.source,
                    target.session_id, session_key=target.session_key, inbound_message_id='destination-human')
            else:
                result = await runner._run_agent(event.text, '', [], event.source, target.session_id,
                    session_key=target.session_key, inbound_message_id=event.message_id,
                    session_message_event=event)
            results.append(result)
            return result['final_response']
        finally:
            destination_done.set()
    adapter.set_message_handler(destination_handler)
    try:
        history = []
        for turn in range(2):
            destination_done.clear()
            result = await asyncio.wait_for(runner._run_agent(f'human {turn}', '', history, a,
                sender.session_id, session_key=sender.session_key, inbound_message_id=f'human-{turn}'), 15)
            assert result['completed'], result
            history = result['messages']
            tool_results = [json.loads(m['content']) for m in history if m['role'] == 'tool']
            assert tool_results[-1]['accepted'] is True, tool_results
            await asyncio.wait_for(destination_done.wait(), 15)
            # Join the actual adapter task before starting the cached next turn.
            task = adapter._session_tasks.get(target.session_key)
            if task:
                await asyncio.wait_for(task, 15)
        for event, (chain, hops) in zip(received, destination_runtime):
            assert chain is event._session_message_delivery.chain and hops == 1, 'Destination production worker lost trusted chain budget'
        assert len(destination_runtime) == len(received) == 2
        assert len(built) == 2, 'Both conversations must reuse their actual cached AIAgent'
        assert len(received) == 2 and all(not e.allow_gateway_control for e in received)
        assert all(r['completed'] for r in results)
        assert not getattr(runner._session_messenger, 'pending', {})
        assert all('send_session_message' in [t['function']['name'] for t in agent.tools] for agent in built)
        destination_requests = [payload for sid, payload in requests if sid == target.session_id
            and 'Internal agent-origin message' in str(payload['messages'][-1]['content'])]
        assert len(destination_requests) == 2
        for event, payload in zip(received, destination_requests):
            canonical = strip_anthropic_cache_control(copy.deepcopy(payload['messages']))
            assert canonical[-1]['role'] == 'user'
            assert canonical[-1]['content'] == event.text
            assert event.metadata['gateway_session_strict'] is True
            assert '_session_message_delivery' not in event.metadata
        assert requests[0][1]['tools'] == requests[-1][1]['tools']
        assert all(payload['messages'][0]['content'] == requests[0][1]['messages'][0]['content']
                   for _, payload in requests)
    finally:
        for task in list(adapter._session_tasks.values()):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        runner._shutdown_executor(drain_timeout=2)
