"""Offline provider-wire snapshots for Discord peer input, resume and caching."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.prompt_caching import strip_anthropic_cache_control
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_messaging import SessionMessenger, bind_session_messenger
from run_agent import AIAgent
from tools.session_message_tool import SESSION_MESSAGE_SCHEMA


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.asyncio
async def test_actual_requests_peer_user_role_resume_stable_tools_options_and_ordinary_traffic(cache):
    from model_tools import get_tool_definitions
    # Production registry/schema, not a made-up snapshot schema.
    schemas = get_tool_definitions(enabled_toolsets=['session_messaging'])
    raw = get_tool_definitions(enabled_toolsets=['session_messaging'], skip_tool_search_assembly=True)
    assert [s['function']['name'] for s in raw] == ['send_session_message']
    assert raw[0]['function'] == SESSION_MESSAGE_SCHEMA
    # Preserve the existing tool-search policy: this surface can be deferred.
    assert schemas
    src = SessionSource(platform=Platform.DISCORD, chat_id='1', user_id='owner')
    dst = SessionSource(platform=Platform.DISCORD, chat_id='2', user_id='owner')
    entries = {'a': SimpleNamespace(session_id='a', session_key='a', origin=src),
               'b': SimpleNamespace(session_id='b', session_key='b', origin=dst)}
    events = []
    async def accept(event):
        event._gateway_accepted = True
        events.append(event)
    adapter = SimpleNamespace(_active_sessions=set(), handle_message=accept)
    async def visible(*args, **kwargs):
        return SimpleNamespace(success=True)
    adapter.send = visible
    runner = SimpleNamespace(session_store=SimpleNamespace(lookup_by_session_id=entries.get),
        _draining=False, _delivery_adapter_for=lambda source: adapter,
        _session_key_for_source=lambda source: 'a' if source.chat_id == '1' else 'b',
        _is_session_running=lambda key: False, _thread_metadata_for_source=lambda source: {})
    messenger = SessionMessenger(runner)
    # Use the actual admission text. A fake wire below exercises the real AIAgent request builder.
    with bind_session_messenger(messenger, src, 'a', None):
        import gateway.session_messaging as messaging
        assert (await messenger.send(messaging._CURRENT.get(), 'b', '/reset is peer data'))['accepted']
    peer = events[0].text
    assert not events[0].allow_gateway_control
    with (patch('model_tools.get_tool_definitions', return_value=schemas),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(api_key='offline-test-key', base_url='https://openrouter.ai/api/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = 'stable system instructions\n\nstable destination session'
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
    history = [{'role': 'user', 'content': 'prior user'}, {'role': 'assistant', 'content': 'prior assistant'}]
    original = copy.deepcopy(history)
    with patch.object(agent, '_save_trajectory'), patch.object(agent, '_cleanup_task_resources'):
        agent.run_conversation('ordinary human', conversation_history=history)
        received = agent.run_conversation(peer, conversation_history=history)
        assert received['completed']
        agent.run_conversation('real user follow-up', conversation_history=received['messages'])
        agent.run_conversation('ordinary human', conversation_history=history)
    canonical = [strip_anthropic_cache_control(copy.deepcopy(r['messages'])) for r in requests]
    assert history == original
    assert [r['role'] for r in canonical[1]] == ['system', 'user', 'assistant', 'user']
    assert canonical[1][-1]['content'] == peer
    assert canonical[1] == canonical[2][:len(canonical[1])]
    assert canonical[0][:-1] == canonical[1][:-1]
    assert requests[0] == requests[3]  # Unaffected traffic is byte/data-identical.
    assert all(r['tools'] == requests[0]['tools'] for r in requests)
    assert all({k:v for k,v in r.items() if k != 'messages'} ==
               {k:v for k,v in requests[0].items() if k != 'messages'} for r in requests)
    assert agent._cached_system_prompt == 'stable system instructions\n\nstable destination session'
