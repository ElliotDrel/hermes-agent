"""Delivery metadata stays outside SQLite and provider requests (no live I/O)."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.response_metadata import strip_response_metadata_footer, transform_discord_response
from gateway.run_turn import GatewayTurnMixin
from tests.agent.test_transform_llm_output_persistence import db_agent, _fake_completion


FOOTER = "\n\n*Model: test · Provider: nous · Context used: 15 tokens (1% of compaction trigger) · Compactions: 3*"


def install_hook(monkeypatch):
    calls = []
    def hook(name, **kwargs):
        if name == "transform_gateway_response":
            calls.append(copy.deepcopy(kwargs))
            return [strip_response_metadata_footer(kwargs["response_text"]) + FOOTER]
        return []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    return calls


@pytest.mark.parametrize("cache", [False, True])
def test_actual_sqlite_and_next_provider_payload_unchanged(db_agent, monkeypatch, cache):
    agent, db = db_agent
    agent.platform = "discord"
    agent._use_prompt_caching = cache
    agent._use_native_cache_layout = False
    agent.compression_enabled = False
    calls = install_hook(monkeypatch)
    requests = []
    monkeypatch.setattr("agent.title_generator.auto_title_session", lambda *a, **k: None)
    def complete(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        return _fake_completion("reply " + str(len(requests)))(**kwargs)
    agent.client.chat.completions.create = complete
    first = agent.run_conversation("first")
    before = copy.deepcopy(first)
    display = transform_discord_response(first["final_response"], first, Platform.DISCORD)
    assert display == "reply 1" + FOOTER
    assert first == before
    second = agent.run_conversation("second", conversation_history=first["messages"])
    assert [m["content"] for m in db.get_messages(agent.session_id) if m["role"] == "assistant"] == ["reply 1", "reply 2"]
    assert "of compaction trigger" not in str(requests)
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    assert requests[0].get("tools") == requests[1].get("tools")
    assert second["final_response"] == "reply 2"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_stream_edit_display_copy_and_failed_edit_fallback(monkeypatch, success):
    install_hook(monkeypatch)
    runner = GatewayTurnMixin()
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="offline")
    response = {"final_response": "answer", "messages": [{"role": "assistant", "content": "answer"}]}
    before = copy.deepcopy(response["messages"])
    adapter = SimpleNamespace(edit_message=AsyncMock(return_value=SimpleNamespace(success=success)))
    consumer = SimpleNamespace(adapter=adapter, message_id="offline-id", final_content_delivered=True,
                               delivered_final_matches=lambda text: False, stream_deltas_enabled=True)
    ctx = SimpleNamespace(source=source, session_key="offline", stream_consumer_holder=[consumer])
    runner._run_agent_stream_confirmed_final_delivery = lambda *a, **k: False
    runner._run_agent_prepare_delivery_response(response, source)
    await runner._run_agent_mark_streamed_delivery(response, ctx)
    assert adapter.edit_message.await_args.kwargs["content"] == "answer" + FOOTER
    assert bool(response.get("already_sent")) == success
    assert response["final_response"] == "answer"
    assert response["messages"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id,split", [("__no_edit__", False), ("last-chunk", True)])
async def test_noneditable_or_split_stream_keeps_normal_send_fallback(monkeypatch, message_id, split):
    install_hook(monkeypatch)
    runner = GatewayTurnMixin()
    runner._run_agent_stream_confirmed_final_delivery = lambda *a, **k: False
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="offline")
    response = {"final_response": "answer"}
    adapter = SimpleNamespace(edit_message=AsyncMock())
    consumer = SimpleNamespace(adapter=adapter, message_id=message_id, _turn_split_delivery=split,
                               final_content_delivered=False, stream_deltas_enabled=True)
    ctx = SimpleNamespace(source=source, session_key="offline", stream_consumer_holder=[consumer])
    runner._run_agent_prepare_delivery_response(response, source)
    await runner._run_agent_mark_streamed_delivery(response, ctx)
    adapter.edit_message.assert_not_awaited()
    assert not response.get("already_sent")
    assert response["delivery_response"] == "answer" + FOOTER


@pytest.mark.parametrize("platform", [Platform.TELEGRAM, "cli"])
def test_other_transports_do_not_invoke(monkeypatch, platform):
    calls = install_hook(monkeypatch)
    assert transform_discord_response("answer", {}, platform) == "answer"
    assert not calls


def test_missing_plugin_and_silent_are_fail_open(monkeypatch):
    calls = install_hook(monkeypatch)
    assert transform_discord_response("no change [SILENT]", {}, "discord") == "no change [SILENT]"
    assert not calls
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *a, **k: [])
    assert transform_discord_response("answer", {}, "discord") == "answer"


@pytest.mark.parametrize("suffix", [FOOTER, "\n\n*model: old*\n*provider: old*\n*context used: ~2k*"])
def test_duplicate_strip_and_backfill_copy(suffix):
    assert strip_response_metadata_footer("answer" + suffix) == "answer"
    assert strip_response_metadata_footer("answer" + suffix + suffix) == "answer"
    assert strip_response_metadata_footer("answer\n\n*ordinary italic prose*") == "answer\n\n*ordinary italic prose*"
