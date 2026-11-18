"""Offline orchestration (HERMES-FORK-017): temporary progress follows its own turn's delivery.

Real ``_run_agent`` turns, the real queued-follow-up handoff and the adapter's delivery finally run
against fakes; no live Discord. Progress cleanup never changes what the model receives."""
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_turn_bubble_cleanup import register_bubble_cleanup
from gateway.session import SessionSource
from tests.gateway.test_composed_progress_lifecycle import (
    LifecycleAdapter, composed_config, install_configured_runner, make_context, start_compositor,
)
from tests.gateway.test_run_cleanup_progress import _make_runner


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

    async def handler(event):
        result = await runner._run_agent(
            message=event.text, context_prompt="stable context", history=history, source=source,
            session_id="session", session_key=key, run_generation=7,
            _interrupt_depth=runner._MAX_INTERRUPT_DEPTH if first_state == "cap" else 0,
        )
        if first_state == "cap":
            # Do not run the restaged event: this assertion targets terminal cap delivery.
            adapter._pending_messages.clear()
        return None if result.get("already_sent") else result["final_response"]

    adapter._message_handler = handler
    await adapter._process_message_background(MessageEvent(text="first input", source=source, message_id="first"), key)
    # Presentation ownership never reaches the model: the same history prefix on every input.
    assert history == original
    assert inputs[0] == ("first input", original)
    assert all(item[1] == original for item in inputs)
    progress = [s["message_id"] for s in adapter.sent if (s["metadata"] or {}).get("non_conversational")]
    deleted = [d["message_id"] for d in adapter.deleted]
    if first_state in {"success", "cap"}:
        assert progress and set(progress) == set(deleted)
    else:
        # An undelivered, interrupted or cancelled predecessor keeps its own breadcrumb.
        assert progress[0] not in deleted
        if first_state != "cancelled":
            assert len(inputs) == 2 and len(progress) == 2
            assert deleted == [progress[1]]
    if first_state == "success":
        first_delete = next(i for i, op in enumerate(adapter.operations) if op[0] == "delete")
        next_progress = next(i for i, op in enumerate(adapter.operations) if op[0] == "send" and op[1] == progress[1])
        assert first_delete < next_progress


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_success", [True, False, "exception"])
async def test_failed_deletion_retains_the_owned_id_after_two_attempts(delete_success):
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter, _cleanup_progress=True, _cleanup_adapter=adapter)
    ctx.run_generation = 11
    compositor = await start_compositor(turn)
    attempts = []

    async def delete(chat_id, mid):
        attempts.append(mid)
        if delete_success == "exception":
            raise OSError("transport disconnected")
        return delete_success

    adapter.delete_message = delete
    register_bubble_cleanup({"final_response": "first"}, ctx)
    await adapter._fire_post_delivery_callback("key", 11, delivered=True)
    owned = compositor.message_id
    assert compositor.closed
    assert ctx._cleanup_msg_ids == ([] if delete_success is True else [owned])
    assert attempts == [owned] * (1 if delete_success is True else 2)


@pytest.mark.asyncio
async def test_unconfirmed_delivery_keeps_progress_while_sibling_callbacks_still_fire():
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter, _cleanup_progress=True, _cleanup_adapter=adapter)
    await start_compositor(turn)
    register_bubble_cleanup({"final_response": "answer"}, ctx)
    fired = []
    adapter.register_post_delivery_callback("key", lambda: fired.append("sibling"), generation=1)
    await adapter._fire_post_delivery_callback("key", 1, delivered=False)
    assert fired == ["sibling"]
    assert adapter.deleted == []


def _authority_with_slot(callback):
    from types import SimpleNamespace
    slots = {}
    return SimpleNamespace(native_waiters=set(), post_delivery={"turn": (slots, "route", callback)},
                           pending_deliveries={}, runner=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False, None])
async def test_authority_drain_reports_its_delivery_verdict_to_post_delivery_callbacks(monkeypatch, delivered):
    """A refused/failed or never-owed final must reach the cleanup callback as such, not as 'unknown'."""
    from contextlib import nullcontext
    from gateway import session_ingress
    from gateway.platforms.base_post_delivery import delivery_confirmed

    async def no_scope(authority, home):
        return nullcontext()
    monkeypatch.setattr(session_ingress, "_execution_scope", no_scope)
    seen = []
    authority = _authority_with_slot(lambda: seen.append(delivery_confirmed.get()))
    await session_ingress.fire_post_delivery(authority, "turn", delivered=delivered)
    assert seen == [delivered] and delivery_confirmed.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [True, False, "raises"])
async def test_settled_delivery_reports_whether_the_final_reached_the_chat(monkeypatch, sent):
    from types import SimpleNamespace
    from gateway import session_ingress
    from contextlib import nullcontext

    async def no_scope(authority, home):
        return nullcontext()

    async def deliver(adapter, event, session_key, response):
        if sent == "raises":
            raise RuntimeError("send refused")
        return sent

    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(session_ingress, "_execution_scope", no_scope)
    monkeypatch.setattr(session_ingress, "deliver_response", deliver)
    monkeypatch.setattr(session_ingress, "_release_turn_marker", noop)
    monkeypatch.setattr(session_ingress, "close_producer_turn", noop)
    authority = SimpleNamespace(native_waiters=set(), runner=None, held_turn_markers={},
                                pending_deliveries={"turn": ("adapter", "event", "key", "reply", None)})
    assert await session_ingress.deliver_settled(authority, "turn") is (sent is True)
