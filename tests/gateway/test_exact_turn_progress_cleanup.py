"""Offline orchestration: presentation ownership never follows a successor's final."""
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from tests.gateway.test_composed_progress_lifecycle import (
    LifecycleAdapter, composed_config, install_configured_runner, make_context,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_state", ["success", "interrupted", "refused", "cap", "cancelled"])
async def test_actual_turn_chain_owns_progress_and_preserves_input(monkeypatch, tmp_path, first_state):
    inputs = []
    history = [{"role": "user", "content": "prefix"}, {"role": "assistant", "content": "unchanged"}]
    original = copy.deepcopy(history)

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, conversation_history=None, **kwargs):
            inputs.append((message, copy.deepcopy(conversation_history)))
            result = {"final_response": "first answer" if len(inputs) == 1 else "second answer",
                      "messages": copy.deepcopy(history), "completed": True}
            if len(inputs) == 1 and first_state == "interrupted":
                result["interrupted"] = True
            if first_state == "cancelled":
                result["cancelled"] = True
                result["completed"] = False
            return result

    runner, adapter = install_configured_runner(monkeypatch, tmp_path, composed_config(), Agent)
    runner._draining = False
    key = "key"
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread")
    adapter._active_sessions[key] = asyncio.Event()
    adapter._active_sessions[key]._hermes_run_generation = 7
    adapter._pending_messages[key] = MessageEvent(text="second input", source=source, message_id="second")
    if first_state == "refused":
        runner._deliver_queued_first_response = AsyncMock(return_value=False)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="second input")
    runner._session_key_for_source = lambda source: key
    runner._refresh_agent_cache_message_count = AsyncMock()
    callbacks = []
    releases = []
    from gateway.run_turn_runner import TurnRunner
    def review_callbacks(turn):
        owner = turn._ctx._delivery_owner
        return lambda message: None, lambda: releases.append(owner)
    monkeypatch.setattr(TurnRunner, "_make_bg_review_callbacks", review_callbacks)
    adapter.register_post_delivery_callback(key, lambda: callbacks.append("ownerless"), generation=7)
    runner._goal_notice_adapter = lambda source: adapter
    runner._send_goal_status_notice = AsyncMock()

    async def handler(event):
        result = await runner._run_agent(
            message=event.text, context_prompt="stable context", history=history, source=source,
            session_id="session", session_key=key, run_generation=7,
            _interrupt_depth=runner._MAX_INTERRUPT_DEPTH if first_state == "cap" else 0,
        )
        if first_state == "cap":
            # Do not run the restaged event: this assertion targets terminal cap delivery.
            adapter._pending_messages.clear()
        await runner._defer_goal_status_notice_after_delivery(source, "terminal goal")
        return None if result.get("already_sent") else result["final_response"]

    adapter._message_handler = handler
    await adapter._process_message_background(MessageEvent(text="first input", source=source, message_id="first"), key)
    assert history == original
    assert inputs[0] == ("first input", original)
    assert callbacks == []
    if first_state != "cancelled":
        runner._send_goal_status_notice.assert_awaited_once_with(source, "terminal goal")
    assert all(item[1] == original for item in inputs)
    progress = [s["message_id"] for s in adapter.sent if (s["metadata"] or {}).get("non_conversational")]
    retained_owners = [k[2] for k in adapter._post_delivery_callbacks if k[2] is not None]
    if first_state in {"interrupted", "refused"}:
        assert not any(owner in releases for owner in retained_owners)
    assert len(releases) == (2 if first_state in {"success", "cancelled"} else 1)
    deleted = [d["message_id"] for d in adapter.deleted]
    if first_state in {"success", "cap"}:
        assert progress and set(progress) == set(deleted)
    else:
        assert progress[0] not in deleted
        assert any(k[2] is not None for k in adapter._post_delivery_callbacks)
        if first_state != "cancelled":
            assert len(inputs) == 2 and len(progress) == 2
            assert deleted == [progress[1]]
    if first_state == "success":
        first_delete = next(i for i, op in enumerate(adapter.operations) if op[0] == "delete")
        next_progress = next(i for i, op in enumerate(adapter.operations) if op[0] == "send" and op[1] == progress[1])
        assert first_delete < next_progress


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_success", [True, False, "exception"])
async def test_adapter_handoff_waits_for_exact_owner_and_retains_failed_deletion(delete_success):
    adapter = LifecycleAdapter()
    from tests.gateway.test_run_cleanup_progress import _make_runner
    runner = _make_runner(adapter)
    ctx, _ = make_context(runner, adapter, _cleanup_progress=True, _cleanup_msg_ids=["owned"])
    ctx.run_generation = 11
    runner._run_agent_schedule_bubble_cleanup({"final_response": "first"}, adapter, ctx)
    entered, release, successor_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []
    async def delete(chat_id, mid):
        attempts.append(mid)
        entered.set()
        await release.wait()
        if delete_success == "exception":
            raise OSError("transport disconnected")
        return delete_success
    adapter.delete_message = delete
    shared = asyncio.Event()
    adapter._active_sessions["key"] = shared
    sources = SessionSource(platform=Platform.DISCORD, chat_id="thread")
    calls = []
    async def handler(event):
        calls.append(event.text)
        if event.text == "first":
            shared._hermes_run_generation = 11
            adapter._post_delivery_owner.set((11, ctx._delivery_owner))
            adapter._pending_messages["key"] = MessageEvent(text="second", source=sources)
            return "first final"
        shared._hermes_run_generation = 12
        successor_started.set()
        adapter.register_post_delivery_callback("key", lambda: calls.append("second callback"), generation=12)
        return "second final"
    adapter._message_handler = handler
    task = asyncio.create_task(adapter._process_message_background(MessageEvent(text="first", source=sources), "key"))
    adapter._track_session_task("key", task)
    await asyncio.wait_for(entered.wait(), 5)
    assert not successor_started.is_set()
    # Deliberately mutate the shared event while predecessor deletion is blocked.
    shared._hermes_run_generation = 99
    adapter.register_post_delivery_callback("key", lambda: calls.append("unrelated"), generation=99)
    release.set()
    await task
    await asyncio.wait_for(successor_started.wait(), 5)
    successor = adapter._session_tasks.get("key")
    if successor is not None:
        await successor
    assert "unrelated" not in calls
    assert "second callback" in calls
    assert ctx._cleanup_msg_ids == ([] if delete_success is True else ["owned"])
    assert len(attempts) == (1 if delete_success is True else 2)
    if delete_success is not True:
        assert ("key", 11, ctx._delivery_owner) in adapter._post_delivery_callbacks
        await adapter._fire_post_delivery_callback("key", 11, ctx._delivery_owner)
        assert len(attempts) == 2  # bounded budget is not reset by a later callback
    runner._hmwa_discard_stale_result(ctx.source, "key", 11)
    if delete_success is not True:
        assert ("key", 11, ctx._delivery_owner) in adapter._post_delivery_callbacks
    assert callable(adapter.pop_post_delivery_callback("key", generation=99))
