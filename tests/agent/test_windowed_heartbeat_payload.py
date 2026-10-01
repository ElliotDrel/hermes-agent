"""Offline real agent requests: heartbeat changes only the new user message."""
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.heartbeat import HeartbeatState, HEARTBEAT_PROMPT_TEMPLATE
from run_agent import AIAgent
from agent.prompt_caching import strip_anthropic_cache_control


@pytest.mark.parametrize('cache', [False, True])
def test_heartbeat_requests_preserve_prefix_schemas_route_and_resume(cache):
    schemas = [{'type': 'function', 'function': {'name': 'web_search', 'description': 'offline tool',
                'parameters': {'type': 'object', 'properties': {}}}}]
    with (patch('model_tools.get_tool_definitions', return_value=schemas),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(api_key='offline-test-key', base_url='https://openrouter.ai/api/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = 'stable system instructions\n\nstable session context'
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
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')], model='test/model', usage=None)

    agent.client.chat.completions.create.side_effect = complete
    state = HeartbeatState('specific outstanding question', 900, windows=['07:15-09:00'],
                           timezone='America/New_York', include_time=True)
    history = [{'role': 'user', 'content': 'prior user'}, {'role': 'assistant', 'content': 'prior assistant'}]
    before = copy.deepcopy(history)
    first = state.render_prompt(now=datetime.fromisoformat('2026-09-30T08:01:00-04:00').timestamp())
    second = state.render_prompt(now=datetime.fromisoformat('2026-09-30T08:16:00-04:00').timestamp())
    with (patch.object(agent, '_save_trajectory'),
          patch.object(agent, '_cleanup_task_resources')):
        result = agent.run_conversation(first, conversation_history=history)
        assert result['completed'] and result['final_response'] == 'offline answer'
        resumed = copy.deepcopy(result['messages'])
        again = agent.run_conversation(second, conversation_history=resumed)
        assert again['completed']
        state.include_time = False
        plain = state.render_prompt()
        assert plain == HEARTBEAT_PROMPT_TEMPLATE.format(interval='15m', prompt=state.prompt)
        agent.run_conversation(plain, conversation_history=history)
        legacy = HeartbeatState(state.prompt, 900).render_prompt()
        agent.run_conversation(legacy, conversation_history=history)
    assert len(requests) == 4
    assert history == before
    assert [row['role'] for row in requests[0]['messages']] == ['system', 'user', 'assistant', 'user']
    assert requests[0]['messages'][0] == requests[1]['messages'][0]
    # Existing rolling cache markers move to the newest turn; canonical past bytes don't.
    canonical = [strip_anthropic_cache_control(copy.deepcopy(r['messages'])) for r in requests]
    assert canonical[0] == canonical[1][:len(canonical[0])]
    assert requests[0]['tools'] == requests[1]['tools'] == requests[2]['tools'] == requests[3]['tools']
    assert {k: v for k, v in requests[0].items() if k != 'messages'} == {k: v for k, v in requests[1].items() if k != 'messages'}
    assert requests[2] == requests[3]  # window-only and default requests byte/data identical
    assert agent._cached_system_prompt == 'stable system instructions\n\nstable session context'
    assert '2026-09-30T08:16:00-04:00' in str(requests[1]['messages'][-1]['content'])
