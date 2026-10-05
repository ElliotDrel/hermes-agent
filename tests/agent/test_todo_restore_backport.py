"""Stable backport of #125161: resume, request invariants and hostile history."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools.todo_tool import TODO_SCHEMA, TodoStore, todo_tool


def make_agent(cache=False):
    with (patch('model_tools.get_tool_definitions', return_value=[]),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(api_key='offline-test-key', base_url='https://openrouter.ai/api/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = 'stable system instructions'
    agent._cached_system_prompt_static = 'stable system instructions'
    agent._use_prompt_caching = cache
    agent._use_native_cache_layout = False
    agent._cache_ttl = '5m'
    agent.compression_enabled = False
    agent.save_trajectories = False
    requests = []
    def complete(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        message = SimpleNamespace(content='offline answer', tool_calls=None, reasoning=None,
                                  reasoning_content=None, reasoning_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')],
                               model='test/model', usage=None)
    agent.client.chat.completions.create.side_effect = complete
    return agent, requests


def history_for(name='tool_call', arguments=None, call_id='c1'):
    if arguments is None:
        arguments = {'calls': [{'name': 'todo_list', 'arguments': {}}]} if name == 'tool_call' else {}
    store = TodoStore()
    result = todo_tool(store=store, todos=[
        {'id': 'setup', 'content': 'Install dependencies', 'status': 'pending'},
        {'id': 'verify', 'content': 'Run tests', 'status': 'pending'},
    ])
    return [
        {'role': 'user', 'content': 'Set up project'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': call_id, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'content': result},
        {'role': 'assistant', 'content': 'Dependencies need approval'},
    ]


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('name', ['todo', 'todo_list', 'tool_call'])
def test_resume_preserves_tasks_and_request_bytes(cache, name):
    history = history_for(name)
    original = copy.deepcopy(history)
    schema = copy.deepcopy(TODO_SCHEMA)
    # JSON roundtrip exercises transcript reload without touching live session storage.
    loaded = json.loads(json.dumps(history))
    fresh, requests = make_agent(cache)
    prehydrated, control_requests = make_agent(cache)
    snapshot = json.loads(history[2]['content'])
    prehydrated._todo_store.restore(snapshot['todos'], revision=snapshot['revision'])
    for agent in (fresh, prehydrated):
        with patch.object(agent, '_save_trajectory'), patch.object(agent, '_cleanup_task_resources'):
            result = agent.run_conversation('Update documentation', conversation_history=loaded)
        assert result['completed']
        assert agent._todo_store.snapshot() == prehydrated._todo_store.snapshot()
        # The second turn must not manufacture a new plan from an empty store.
        with patch.object(agent, '_save_trajectory'), patch.object(agent, '_cleanup_task_resources'):
            agent.run_conversation('Continue', conversation_history=result['messages'])
    assert requests == control_requests
    assert history == loaded == original
    assert TODO_SCHEMA == schema
    assert fresh._cached_system_prompt == 'stable system instructions'
    todo_tool(store=fresh._todo_store, merge=True, todos=[
        {'id': 'docs', 'content': 'Update documentation', 'status': 'in_progress'}])
    assert {item['id'] for item in fresh._todo_store.read()} == {'setup', 'verify', 'docs'}
    injection = fresh._todo_store.format_for_injection()
    assert 'Install dependencies' in injection and 'Run tests' in injection


@pytest.mark.parametrize('case', ['unpaired', 'wrong-id', 'user-boundary', 'other-tool',
                                  'malformed-bridge', 'multiple-calls', 'missing-id'])
def test_restore_rejects_untrusted_or_ambiguous_results(case):
    history = history_for()
    if case == 'unpaired':
        history.pop(1)
    elif case == 'wrong-id':
        history[2]['tool_call_id'] = 'different'
    elif case == 'user-boundary':
        history.insert(2, {'role': 'user', 'content': 'Unrelated boundary'})
    elif case == 'other-tool':
        history[1]['tool_calls'][0]['function']['name'] = 'terminal'
    elif case == 'malformed-bridge':
        history[1]['tool_calls'][0]['function']['arguments'] = '{invalid'
    elif case == 'multiple-calls':
        history[1]['tool_calls'][0]['function']['arguments'] = json.dumps({'calls': [
            {'name': 'todo_list', 'arguments': {}}, {'name': 'terminal', 'arguments': {}}]})
    elif case == 'missing-id':
        history[2].pop('tool_call_id')
    agent, _ = make_agent()
    agent._hydrate_todo_store(history)
    assert agent._todo_store.snapshot() == {'todos': [], 'revision': 0}
