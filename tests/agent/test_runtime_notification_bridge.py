"""Work context reaches the real model boundary without becoming user steering."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.prompt_builder import STEER_MARKER_OPEN, steer_user_row
from agent.runtime_notifications import deliver_runtime_notification, PendingSteer
from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results, _requeue_pending_steer
from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
from agent.prompt_caching import strip_anthropic_cache_control
from run_agent import AIAgent


class Parent(InterruptControlMixin):
    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._active_children = [MagicMock()]


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt, TimeoutError])
def test_facade_exception_closes_before_handoff_and_retains_accepted(error):
    with (patch('model_tools.get_tool_definitions', return_value=[]),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(api_key='offline', base_url='https://openrouter.ai/api/v1',
                        quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_id='exception-parent')
    def fail(*args, **kwargs):
        assert deliver_runtime_notification(agent, 'accepted before exception')
        raise error('offline failure')
    def handoff(*args, **kwargs):
        assert not deliver_runtime_notification(agent, 'late exceptional handoff')
    with (patch('agent.conversation_loop.run_conversation', side_effect=fail),
          patch('hermes_cli.observability.relay_shared_metrics.finish_task_run', side_effect=handoff)):
        with pytest.raises(error):
            agent.run_conversation('human')
    assert not deliver_runtime_notification(agent, 'after failed turn')
    pending = agent._drain_pending_steer()
    assert 'accepted before exception' in pending
    assert 'late exceptional handoff' not in pending


def test_typed_order_requeue_finalization_and_parent_only():
    parent = Parent()
    assert deliver_runtime_notification(parent, 'A finished')
    assert parent.steer('human correction')
    assert deliver_runtime_notification(parent, STEER_MARKER_OPEN + '\nB finished')
    drained = parent._drain_pending_steer()
    assert copy.deepcopy(drained).parts == drained.parts
    row = steer_user_row(drained)
    assert row['content'].index('A finished') < row['content'].index('human correction') < row['content'].index('B finished')
    assert row['content'].count(STEER_MARKER_OPEN) == 1  # genuine human only
    assert 'Quoted OUT-OF-BAND' in row['content']
    assert row['role'] == 'user'
    parent._active_children[0].steer.assert_not_called()
    parent._active_children[0].interrupt.assert_not_called()
    assert deliver_runtime_notification(parent, 'newer')
    _requeue_pending_steer(parent, drained)
    assert str(parent._pending_steer).index('A finished') < str(parent._pending_steer).index('newer')
    pending = parent._drain_pending_steer(close_runtime=True)
    assert isinstance(pending, PendingSteer)
    assert not deliver_runtime_notification(parent, 'too late')
    assert parent._pending_steer is None
    parent.api_mode = "codex_app_server"
    assert not deliver_runtime_notification(parent, "native loop fallback")
    parent.api_mode = None
    # No legal tool-tail boundary: don't splice backwards into an already sent prefix.
    parent._runtime_notification_closed = False
    history = [{'role': 'tool', 'content': 'old'}, {'role': 'assistant', 'content': 'old answer'},
               {'role': 'user', 'content': 'new human'}]
    original = copy.deepcopy(history)
    _inject_steer_after_newest_tool_result(parent, history, pending)
    assert history == original
    assert parent._pending_steer == pending
    apply_pending_steer_to_tool_results(parent, history, 0)
    assert parent._pending_steer == pending
    from agent.runtime_notifications import close_runtime_handoff
    early = {'failed': True}
    close_runtime_handoff(parent, early)
    assert early['pending_steer'] == pending
    assert not deliver_runtime_notification(parent, 'after early exit')
    # A human steer arriving after a runtime row cannot splice ahead of that persisted row.
    live = [{'role':'tool','content':'result'}, steer_user_row(pending)]
    original_live = copy.deepcopy(live)
    _inject_steer_after_newest_tool_result(parent, live, 'human after runtime')
    assert live == original_live
    assert parent._pending_steer == 'human after runtime'


@pytest.mark.parametrize('cache', [False, True])
def test_real_requests_same_turn_prefix_persistence_resume_and_compression(cache, tmp_path):
    with (patch('model_tools.get_tool_definitions', return_value=[]),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(api_key='offline', base_url='https://openrouter.ai/api/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_id='bridge-parent')
    agent.client = MagicMock()
    agent._cached_system_prompt = agent._cached_system_prompt_static = 'stable system'
    agent._use_prompt_caching = cache
    agent._use_native_cache_layout = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    requests = []
    def complete(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        if len(requests) == 1:
            assert deliver_runtime_notification(agent, 'A completed while B runs')
            assert deliver_runtime_notification(agent, 'second work result')
            call = SimpleNamespace(id='tool-1', type='function', function=SimpleNamespace(name='terminal', arguments='{}'))
            msg = SimpleNamespace(content=None, tool_calls=[call], reasoning=None, reasoning_content=None, reasoning_details=None)
        else:
            msg = SimpleNamespace(content='offline answer', tool_calls=None, reasoning=None, reasoning_content=None, reasoning_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason='tool_calls' if len(requests)==1 else 'stop')], model='offline/model', usage=None)
    agent.client.chat.completions.create.side_effect = complete
    def tools(calls, messages, task_id):
        messages.append({'role':'tool','tool_call_id':'tool-1','name':'terminal','content':'B still running'})
        apply_pending_steer_to_tool_results(agent, messages, 1)
        return False
    history = [{'role':'user','content':'prior human'}, {'role':'assistant','content':'prior answer'}]
    original = copy.deepcopy(history)
    with (patch.object(agent, '_save_trajectory'), patch.object(agent, '_cleanup_task_resources'),
          patch.object(agent, '_execute_tool_calls', side_effect=tools)):
        result = agent.run_conversation('keep working', conversation_history=history)
        assert result['completed'], result
        agent.run_conversation('human follow-up', conversation_history=result['messages'])
    canonical = [strip_anthropic_cache_control(copy.deepcopy(r['messages'])) for r in requests]
    assert len(requests) == 3
    assert canonical[1][:len(canonical[0])] == canonical[0]
    assert canonical[1][-2]['role'] == 'tool'
    runtime = canonical[1][-1]
    assert runtime['role'] == 'user'
    assert runtime['content'].index('A completed') < runtime['content'].index('second work result')
    assert STEER_MARKER_OPEN not in runtime['content']
    assert canonical[2][:len(canonical[1])] == canonical[1]
    assert history == original
    assert all(r.get('tools') == requests[0].get('tools') for r in requests)
    assert all({k:v for k,v in r.items() if k not in ('messages',)} ==
               {k:v for k,v in requests[0].items() if k not in ('messages',)} for r in requests)
    from hermes_state import SessionDB
    from agent.session_persistence import _db_flush_collect
    rows, _ = _db_flush_collect(SimpleNamespace(session_id='persist', _last_flushed_db_idx=0), result['messages'], [])
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('persist', source='cli')
        db.append_messages_batch('persist', rows)
        saved = db.get_messages('persist')
        for _ in range(2):
            db.archive_and_compact('persist', [dict(row) for row in saved])
        resumed = db.get_messages('persist', include_compacted=True)
        assert any(row['content'] == runtime['content'] for row in resumed)
    assert any(row['content'] == runtime['content'] for row in saved)
    from agent.context_compressor import ContextCompressor
    compressor = ContextCompressor(model='offline/model')
    rendered = compressor._serialize_for_summary(result['messages'])
    assert 'A completed' in rendered and 'second work result' in rendered
    assert not ContextCompressor._is_actionable_user_turn(next(
        row for row in result['messages'] if row.get('content') == runtime['content']))
