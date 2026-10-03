"""Channel-bound skill titles keep the real opener, never rewrite main-model input."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import turn_context
from agent.skill_commands import _build_skill_message
from agent.title_generator import build_title_input, generate_title
from gateway.run_turn import GatewayTurnMixin


def _bound_event(text, names=("coding-prefs",)):
    event = SimpleNamespace(text=text, message_id="opening-message")
    payloads = {name: ({"content": "SKILL_BODY_ONLY " + "s" * 6200}, None, name) for name in names}
    with patch("agent.skill_commands._load_skill_payload", side_effect=lambda name, **kw: payloads.get(name)):
        GatewayTurnMixin()._hmwa_auto_load_skills(event, names, "test-key", "test-session")
    expected = []
    for name in names:
        expected.append(_build_skill_message(payloads[name][0], None,
            f'[IMPORTANT: The "{name}" skill is auto-loaded. Follow its instructions for this session.]'))
    return event, "\n\n".join([*expected, text])


@pytest.mark.parametrize("names", [("coding-prefs",), ("food", "journal")])
def test_bound_skill_keeps_main_input_identical_and_titles_only_the_request(names):
    original = "Set Up Universal Spell Check"
    event, baseline_message = _bound_event(original, names)
    assert event.text == baseline_message
    metadata = GatewayTurnMixin._hmwa_title_display_metadata(event)
    assert metadata["title_user_message"] == original
    message = {"role": "user", "content": event.text, "display_metadata": metadata}
    before = copy.deepcopy(message)
    agent = SimpleNamespace(platform="discord", session_id="title-test", _session_db=MagicMock(),
        _session_db_created=True, model="main/model", provider="openai", base_url=None,
        api_key=None, api_mode="chat_completions")
    with patch("agent.title_generator.maybe_auto_title") as title:
        turn_context._maybe_title_session_at_turn_start(agent, [message])
    assert title.call_args.args[2] == original
    assert message == before
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"title":"Set Up Universal Spell Check"}'))])
    with patch("agent.title_generator.call_llm", return_value=response) as call:
        assert generate_title(title.call_args.args[2]) == original
    assert call.call_args.kwargs["messages"][1]["content"] == original
    assert "SKILL_BODY_ONLY" not in json.dumps(call.call_args.kwargs["messages"])
    assert call.call_args.kwargs["task"] == "title_generation"


def test_empty_original_does_not_title_the_injected_skill():
    event, _ = _bound_event("")
    agent = SimpleNamespace(platform="telegram", session_id="image-test", _session_db=MagicMock())
    message = {"role": "user", "content": event.text,
               "display_metadata": GatewayTurnMixin._hmwa_title_display_metadata(event)}
    with patch("agent.title_generator.maybe_auto_title") as title:
        turn_context._maybe_title_session_at_turn_start(agent, [message])
    title.assert_not_called()


def test_missing_skill_does_not_create_title_metadata():
    event = SimpleNamespace(text="Ordinary user question")
    with patch("agent.skill_commands._load_skill_payload", return_value=None):
        GatewayTurnMixin()._hmwa_auto_load_skills(event, ["missing"], "key", "session")
    assert event.text == "Ordinary user question"
    assert GatewayTurnMixin._hmwa_title_display_metadata(event) == {}


def test_fallback_transcript_row_retains_original_title_input_without_rewriting_content():
    event, expanded = _bound_event("Set Up Universal Spell Check")
    prepared = SimpleNamespace(persist_user_message=expanded, message_text=expanded,
        persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner="owner")
    row = GatewayTurnMixin._hmwa_user_transcript_entry(event, prepared, 123.0)
    assert row["content"] == expanded
    assert row["display_metadata"]["gateway_input_owner"] == "owner"
    assert json.loads(json.dumps(row))["display_metadata"]["title_user_message"] == "Set Up Universal Spell Check"


def test_global_title_budget_includes_character_5000_not_5001():
    original = "a" * 4999 + "B" + "C"
    assert build_title_input(original) == original[:5000]


@pytest.mark.parametrize("cache", [False, True])
def test_real_main_requests_ignore_title_metadata_across_resume_and_cache(cache):
    from agent.prompt_caching import strip_anthropic_cache_control
    from run_agent import AIAgent
    event, expanded = _bound_event("Set Up Universal Spell Check")
    metadata = GatewayTurnMixin._hmwa_title_display_metadata(event)
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="offline-test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "stable system instructions"
    agent._cached_system_prompt_static = "stable system instructions"
    agent._use_prompt_caching = cache
    agent._use_native_cache_layout = False
    agent._cache_ttl = "5m"
    agent.compression_enabled = False
    agent.save_trajectories = False
    requests = []
    def complete(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        message = SimpleNamespace(content="offline answer", tool_calls=None, reasoning=None,
                                  reasoning_content=None, reasoning_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               model="test/model", usage=None)
    agent.client.chat.completions.create.side_effect = complete
    history = [{"role": "user", "content": "prior user"}, {"role": "assistant", "content": "prior assistant"}]
    unchanged = copy.deepcopy(history)
    with (patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"),
          patch("agent.title_generator.maybe_auto_title")):
        agent.run_conversation(expanded, conversation_history=history)
        with_metadata = agent.run_conversation(expanded, conversation_history=history,
                                                persist_user_display_metadata=metadata)
        assert with_metadata["completed"]
        agent.run_conversation("next request", conversation_history=with_metadata["messages"])
    assert requests[0] == requests[1]
    assert history == unchanged
    canonical = [strip_anthropic_cache_control(copy.deepcopy(r["messages"])) for r in requests]
    assert canonical[1] == canonical[2][:len(canonical[1])]
    assert all("display_metadata" not in message for request in requests for message in request["messages"])
    assert all(r.get("tools") == requests[0].get("tools") for r in requests)
    assert agent._cached_system_prompt == "stable system instructions"
