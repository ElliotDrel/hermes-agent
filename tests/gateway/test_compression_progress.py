"""Pre-agent hygiene uses the same Discord breadcrumb as normal tool progress."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner
from gateway.run import GatewayRunner, HygieneTurnHoldExceeded
from gateway.session_state import TurnState
from tests.gateway.test_progress_compositor import CaptureAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [12, 95, None])
async def test_hygiene_progress_wait_and_handoff(monkeypatch, finish):
    import gateway.run_turn as turn_module
    clock = [0.0]
    adapter = CaptureAdapter()
    adapter.MAX_MESSAGE_LENGTH = 2000
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread", chat_type="group")
    entry = SimpleNamespace(session_id="session")
    event = SimpleNamespace(message_id="user", source=source)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(6)]
    original = copy.deepcopy(history)
    summary = [{"role": "assistant", "content": "summary"}, history[-1]]
    hs = SimpleNamespace(compression_enabled=True, hard_msg_limit=200, data={"display": {"platforms": {"discord": {"progress_compositor": "single_message"}}}}, timeout_seconds=30, total_ceiling_seconds=600, max_turn_hold_seconds=120)
    runner._hmwa_hygiene_settings = AsyncMock(return_value=hs)
    runner._hmwa_hygiene_plan = AsyncMock(return_value=SimpleNamespace(needs_compress=True))
    runner._resolve_session_agent_runtime = lambda **kw: ("unchanged", {"api_key": "fake"})
    runner._delivery_adapter_for = lambda s: adapter
    runner._event_thread_metadata = lambda *a: {"thread_id": "thread"}
    runner._is_session_run_current = lambda *a: True
    runner._hmwa_hygiene_notify = AsyncMock()
    runner._hmwa_hygiene_stamp = lambda *a: None
    runner._hmwa_hygiene_defer_cleanup = lambda attempt, context: setattr(attempt, "cleanup_deferred", True)
    fence = SimpleNamespace(is_cancelled=False, commit_watermark_fenced=True, seconds_since_progress=lambda: 0)
    future = asyncio.get_running_loop().create_future()
    attempts = []
    real_wait = asyncio.wait_for

    async def virtual_wait(awaitable, timeout):
        clock[0] = round(clock[0] + timeout, 6)
        # Let the independent display task observe the virtual elapsed time.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if finish is not None and clock[0] >= finish:
            future.set_result((summary, None))
            return await real_wait(awaitable, timeout=1)
        awaitable.cancel()  # cancel the shield, never the underlying worker
        raise asyncio.TimeoutError

    from gateway.progress_compositor import ProgressCompositor
    original_init = ProgressCompositor.__init__
    def virtual_init(self, *args, **kwargs):
        original_init(self, *args, clock=lambda: clock[0], **kwargs)
    monkeypatch.setattr(ProgressCompositor, "__init__", virtual_init)
    monkeypatch.setattr(turn_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(turn_module.asyncio, "wait_for", virtual_wait)

    async def detached(attempt, *args):
        attempts.append(attempt)
        attempt.future, attempt.commit_fence, attempt.wait_started = future, fence, clock[0]
        attempt.agent = SimpleNamespace(session_id="session", _last_compaction_in_place=False)
        try:
            compressed = await runner._hmwa_hygiene_wait_for_summary(attempt, hs, entry)
        except HygieneTurnHoldExceeded:
            await runner._hmwa_hygiene_on_turn_hold(attempt, hs, entry, "key", source)
        else:
            attempt.history = compressed
    runner._hmwa_hygiene_detached_attempt = detached
    result = await runner._hmwa_run_session_hygiene(event, source, entry, "key", history, "key", 1)
    # Restore the real timeout implementation before exercising delivery cleanup.
    monkeypatch.setattr(turn_module.asyncio, "wait_for", real_wait)

    assert adapter.sent[0][1] == "⏳ Compacting context"
    rendered = [edit[2] for edit in adapter.edits]
    expected = [f"⏳ Compacting context ({sec}s elapsed)" for sec in (30, 60, 90) if finish is None or sec < finish]
    assert rendered[:len(expected)] == expected
    assert history == original
    if finish is None:
        assert clock[0] == 120
        assert result is history
        assert not fence.is_cancelled and not future.cancelled()
        assert rendered[-1] == "Compression still running; continuing with existing context"
        runner._hmwa_hygiene_notify.assert_not_awaited()
        attempts[0].agent._last_compaction_in_place = True
        future.set_result((summary, None))
        await asyncio.sleep(0)
    else:
        assert result == summary

    # Real TurnRunner handoff must track exactly the owned ID for delivery-gated cleanup.
    ctx = TurnContext(source=source, session_key="key", run_generation=1)
    ctx._cleanup_progress = True
    ctx._cleanup_msg_ids = []
    ctx._progress_reply_to = "user"
    ctx._progress_metadata = {"thread_id": "thread"}
    turn = TurnRunner(runner, ctx)
    compositor = await turn.start_progress_compositor()
    if finish is None:
        assert compositor.status_line == "Compression still running; continuing with existing context"
    compositor.publish_activity("tool update")
    assert compositor.status_line != "Compression still running; continuing with existing context"
    await compositor.flush(force=True)
    assert len(adapter.sent) == 1
    assert ctx._cleanup_msg_ids == ["progress-1"]
    assert {edit[1] for edit in adapter.edits} == {"progress-1"}
    callbacks = []
    adapter.register_post_delivery_callback = lambda key, callback, **kw: callbacks.append(callback)
    adapter.delete_message = AsyncMock(return_value=True)
    runner._run_agent_schedule_bubble_cleanup({"completed": True}, adapter, ctx)
    adapter.delete_message.assert_not_awaited()
    assert len(callbacks) == 1
    await callbacks.pop()()  # represents confirmed final delivery, never hygiene completion
    adapter.delete_message.assert_awaited_once_with("thread", "progress-1")
    runner._session_state("key").turn.clear()
    assert runner._session_state("key").turn.progress_compositor is None


def test_turn_clear_releases_preagent_compositor():
    state = TurnState()
    state.progress_compositor = object()
    state.clear()
    assert state.progress_compositor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [False, True])
async def test_real_detached_adoption_and_model_requests_ignore_display(monkeypatch, cache):
    from unittest.mock import MagicMock, patch
    from run_agent import AIAgent
    from agent.prompt_caching import strip_anthropic_cache_control
    import gateway.run as gateway_run

    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "prior " + str(i)} for i in range(6)]
    before = copy.deepcopy(history)
    compressed = [{"role": "user", "content": "compressed summary"}, history[-1]]
    all_requests = []
    schemas = [{"type": "function", "function": {"name": "web_search", "description": "offline", "parameters": {"type": "object", "properties": {}}}}]
    for platform, mode in [(Platform.DISCORD, "single_message"), (Platform.DISCORD, "off"), (Platform.TELEGRAM, "single_message")]:
        runner = object.__new__(GatewayRunner)
        adapter = CaptureAdapter()
        runner._delivery_adapter_for = lambda s: adapter
        runner._event_thread_metadata = lambda *a: {"thread_id": "thread"}
        runner._is_session_run_current = lambda *a: True
        hs = SimpleNamespace(compression_enabled=True, hard_msg_limit=200, data={"display": {"progress_compositor": mode}}, timeout_seconds=30, total_ceiling_seconds=600, max_turn_hold_seconds=120, failure_cooldown_seconds=-1)
        plan = SimpleNamespace(needs_compress=True, msg_count=6, approx_tokens=100, warn_token_threshold=1000)
        runner._hmwa_hygiene_settings = AsyncMock(return_value=hs)
        runner._hmwa_hygiene_plan = AsyncMock(return_value=plan)
        runner._resolve_session_agent_runtime = lambda **kw: ("offline", {"api_key": "fake"})
        worker_agent = SimpleNamespace(session_id="session", context_compressor=SimpleNamespace(_last_compress_aborted=False, _last_aux_model_failure_model=None))
        def compress(messages, *a, commit_fence, **kw):
            assert messages == before
            commit_fence.mark_commit_watermark_fenced()
            assert commit_fence.begin_commit()
            worker_agent._last_compaction_in_place = True
            commit_fence.finish_commit()
            return copy.deepcopy(compressed), None
        worker_agent._compress_context = compress
        runner._hmwa_hygiene_build_agent = AsyncMock(return_value=(worker_agent, None))
        runner._evict_cached_agent = lambda key: None
        runner._cleanup_agent_resources_off_loop = AsyncMock()
        monkeypatch.setattr(gateway_run, "_reset_hygiene_failure_streak", lambda *a: None)
        entry = SimpleNamespace(session_id="session", last_prompt_tokens=100)
        source = SessionSource(platform=platform, chat_id="thread", chat_type="group")
        event = SimpleNamespace(message_id="user")
        adopted = await runner._hmwa_run_session_hygiene(event, source, entry, "key", history, "key", 1)
        assert adopted == compressed and history == before
        assert entry.last_prompt_tokens == 0
        runner._cleanup_agent_resources_off_loop.assert_awaited_once()
        assert len(adapter.sent) == (1 if platform == Platform.DISCORD and mode == "single_message" else 0)

        with (patch("model_tools.get_tool_definitions", return_value=schemas),
              patch("model_tools.check_toolset_requirements", return_value={}),
              patch("agent.process_bootstrap.OpenAI")):
            agent = AIAgent(api_key="offline", base_url="https://openrouter.ai/api/v1", quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.client = MagicMock()
        agent._cached_system_prompt = "stable system instructions\n\nstable session context"
        agent._cached_system_prompt_static = "stable system instructions"
        agent._use_prompt_caching = cache
        agent._use_native_cache_layout = False
        agent._cache_ttl = "5m"
        agent.compression_enabled = False
        agent.save_trajectories = False
        requests = []
        def complete(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            msg = SimpleNamespace(content="offline answer", tool_calls=None, reasoning=None, reasoning_content=None, reasoning_details=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="offline", usage=None)
        agent.client.chat.completions.create.side_effect = complete
        with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
            result = agent.run_conversation("new question", conversation_history=adopted)
            assert result["completed"]
            again = agent.run_conversation("resume question", conversation_history=copy.deepcopy(result["messages"]))
            assert again["completed"]
        canonical = [strip_anthropic_cache_control(copy.deepcopy(r["messages"])) for r in requests]
        assert canonical[0] == canonical[1][:len(canonical[0])]
        assert requests[0]["messages"][0] == requests[1]["messages"][0]
        assert {k: v for k, v in requests[0].items() if k != "messages"} == {k: v for k, v in requests[1].items() if k != "messages"}
        assert adopted == compressed and history == before
        all_requests.append(requests)
    assert all_requests[0] == all_requests[1] == all_requests[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_progress_keeps_idle_timeout_and_cancellation_guards(monkeypatch, cancel):
    import gateway.run_turn as turn_module
    from gateway.progress_compositor import ProgressCompositor
    clock = [0.0]
    adapter = CaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner._is_session_run_current = lambda *a: True
    compositor = ProgressCompositor(adapter, "thread", session_key="key", generation=1, clock=lambda: clock[0])
    compositor.publish_status("⏳ Compacting context")
    await compositor.start()
    fence = SimpleNamespace(is_cancelled=False, seconds_since_progress=lambda: clock[0], revoke_commit_admission=lambda: setattr(fence, "is_cancelled", True))
    future = asyncio.get_running_loop().create_future()
    attempt = SimpleNamespace(commit_fence=fence, future=future, wait_started=0, progress_compositor=compositor, cleanup_deferred=False)
    hs = SimpleNamespace(timeout_seconds=30, total_ceiling_seconds=600, max_turn_hold_seconds=120, failure_cooldown_seconds=-1)
    monkeypatch.setattr(turn_module.time, "monotonic", lambda: clock[0])
    async def tick(awaitable, timeout):
        awaitable.cancel()
        if cancel:
            raise asyncio.CancelledError
        clock[0] = round(clock[0] + timeout, 6)
        raise asyncio.TimeoutError
    monkeypatch.setattr(turn_module.asyncio, "wait_for", tick)
    runner._hmwa_hygiene_defer_cleanup = lambda a, context: setattr(a, "cleanup_deferred", True)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            try:
                await runner._hmwa_hygiene_wait_for_summary(attempt, hs, SimpleNamespace(session_id="session"))
            except BaseException:
                runner._hmwa_hygiene_on_unwind(attempt, hs, SimpleNamespace(session_id="session"), "key")
                raise
        assert fence.is_cancelled and attempt.cleanup_deferred
    else:
        with pytest.raises(asyncio.TimeoutError):
            await runner._hmwa_hygiene_wait_for_summary(attempt, hs, SimpleNamespace(session_id="session"))
        assert clock[0] == 30
    assert not future.cancelled()
    assert len(adapter.sent) == 1
    future.set_result(([], None))


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_result", [SendResult(success=False, retryable=True, retry_after=0), SendResult(success=False, error_kind="permanent")])
async def test_failed_hygiene_edit_never_replaces_owned_message(edit_result):
    from gateway.progress_compositor import ProgressCompositor
    adapter = CaptureAdapter([edit_result])
    runner = object.__new__(GatewayRunner)
    compositor = ProgressCompositor(adapter, "thread", generation=1)
    compositor.publish_status("⏳ Compacting context")
    await compositor.start()
    compositor.publish_status("⏳ Compacting context (30s elapsed)")
    await compositor.flush(force=True)
    runner._session_state("key").turn.progress_compositor = compositor
    ctx = TurnContext(source=SessionSource(platform=Platform.DISCORD, chat_id="thread"), session_key="key", run_generation=1)
    ctx._cleanup_progress = True
    inherited = await TurnRunner(runner, ctx).start_progress_compositor()
    assert inherited is compositor
    inherited.publish_activity("tool")
    await inherited.flush(force=True)
    assert len(adapter.sent) == 1
    assert {edit[1] for edit in adapter.edits} == {"progress-1"}


@pytest.mark.asyncio
async def test_slow_edit_cannot_extend_hold_or_leave_updater_running(monkeypatch):
    import gateway.run_turn as turn_module
    from gateway.progress_compositor import ProgressCompositor
    clock = [0.0]
    adapter = CaptureAdapter()
    cancelled = asyncio.Event()
    async def slow_edit(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    adapter.edit_message = slow_edit
    runner = object.__new__(GatewayRunner)
    runner._is_session_run_current = lambda *a: True
    compositor = ProgressCompositor(adapter, "thread", session_key="key", generation=1, clock=lambda: clock[0])
    compositor.publish_status("⏳ Compacting context")
    await compositor.start()
    future = asyncio.get_running_loop().create_future()
    attempt = SimpleNamespace(commit_fence=SimpleNamespace(is_cancelled=False, seconds_since_progress=lambda: 0), future=future, wait_started=0, progress_compositor=compositor)
    hs = SimpleNamespace(timeout_seconds=30, total_ceiling_seconds=600, max_turn_hold_seconds=120)
    async def tick(awaitable, timeout):
        clock[0] += timeout
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        awaitable.cancel()
        raise asyncio.TimeoutError
    monkeypatch.setattr(turn_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(turn_module.asyncio, "wait_for", tick)
    with pytest.raises(HygieneTurnHoldExceeded):
        await runner._hmwa_hygiene_wait_for_summary(attempt, hs, SimpleNamespace(session_id="session"))
    assert clock[0] == 120
    assert cancelled.is_set()
    assert not future.cancelled()
    assert len(adapter.sent) == 1
    future.set_result(([], None))


@pytest.mark.asyncio
async def test_successor_does_not_inherit_displaced_hygiene_message():
    from gateway.progress_compositor import ProgressCompositor
    adapter = CaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda s: adapter
    old = ProgressCompositor(adapter, "thread", generation=1)
    await old.start()
    runner._session_state("key").turn.progress_compositor = old
    ctx = TurnContext(source=SessionSource(platform=Platform.DISCORD, chat_id="thread"), session_key="key", run_generation=2)
    new = await TurnRunner(runner, ctx).start_progress_compositor()
    assert new is not old
    assert len(adapter.sent) == 2  # exactly one per generation, no old-ID handoff
    assert old.status_line is None


from tests.gateway.test_discord_free_response import adapter as discord_adapter_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_hygiene_breadcrumb_excluded_from_real_discord_backfill(discord_adapter_fixture, cached):
    from tests.gateway.test_discord_free_response import FakeHistoryChannel, make_history_message
    adapter = discord_adapter_fixture
    adapter.config.extra["history_backfill_limit"] = 50
    bot = adapter._client.user
    human = SimpleNamespace(id=56, display_name="Alice", name="Alice", bot=False)
    messages = [make_history_message(author=bot, content="⏳ Compacting context", msg_id=222),
                make_history_message(author=human, content="retain prior human", msg_id=200),
                make_history_message(author=bot, content="prior answer", msg_id=111)]
    class WireChannel(FakeHistoryChannel):
        async def send(self, content, reference=None):
            return SimpleNamespace(id=222)
    channel = WireChannel(messages, channel_id=777)
    adapter._client.get_channel = lambda channel_id: channel
    adapter._client.fetch_channel = AsyncMock(return_value=channel)
    if cached:
        adapter._last_self_message_id["777"] = "111"
    runner, args = hygiene_display_runner(adapter)
    await runner._hmwa_run_session_hygiene(*args)
    assert adapter._last_self_message_id.get("777") == ("111" if cached else None)
    assert "222" in adapter._nonconversational_messages
    before = SimpleNamespace(id=300)
    primary = await adapter._fetch_channel_context(channel, before=before)
    assert "retain prior human" in primary and "Compacting context" not in primary
    replied = await adapter._fetch_channel_context(channel, before=before, reply_target=messages[0])
    assert "retain prior human" in replied and "Compacting context" not in replied


def hygiene_display_runner(adapter):
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.DISCORD, chat_id="777")
    history = [{"role": "user", "content": str(i)} for i in range(4)]
    hs = SimpleNamespace(compression_enabled=True, hard_msg_limit=200, data={"display": {"progress_compositor": "single_message"}})
    runner._hmwa_hygiene_settings = AsyncMock(return_value=hs)
    runner._hmwa_hygiene_plan = AsyncMock(return_value=SimpleNamespace(needs_compress=True))
    runner._resolve_session_agent_runtime = lambda **kw: ("offline", {"api_key": "fake"})
    runner._delivery_adapter_for = lambda s: adapter
    runner._event_thread_metadata = lambda *a: {"thread_id": "777"}
    runner._is_session_run_current = lambda *a: True
    runner._hmwa_hygiene_detached_attempt = AsyncMock()
    args = (SimpleNamespace(message_id=None), source, SimpleNamespace(session_id="session"), "key", history, "key", 1)
    return runner, args


@pytest.mark.asyncio
async def test_pending_initial_send_is_bounded_and_joined(monkeypatch):
    from gateway.progress_compositor import ProgressCompositor
    monkeypatch.setattr(ProgressCompositor, "TRANSPORT_TIMEOUT_SECONDS", .01, raising=False)
    adapter = CaptureAdapter()
    cancelled = asyncio.Event()
    async def pending(*a, **kw):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    adapter.send = AsyncMock(side_effect=pending)
    runner, args = hygiene_display_runner(adapter)
    await asyncio.wait_for(runner._hmwa_run_session_hygiene(*args), .2)
    assert cancelled.is_set()
    runner._hmwa_hygiene_detached_attempt.assert_awaited_once()
    compositor = runner._session_state("key").turn.progress_compositor
    assert compositor.editing_disabled and compositor.message_id is None
    assert not (await compositor.start()).success
    adapter.send.assert_awaited_once()
    ctx = TurnContext(source=args[1], session_key="key", run_generation=1)
    assert await TurnRunner(runner, ctx).start_progress_compositor() is compositor


@pytest.mark.asyncio
async def test_displacement_during_initial_send_refuses_worker():
    adapter = CaptureAdapter()
    runner, args = hygiene_display_runner(adapter)
    current = [True]
    successor = object()
    original_send = adapter.send
    async def displace(*a, **kw):
        current[0] = False
        runner._session_state("key").turn.progress_compositor = successor
        return await original_send(*a, **kw)
    adapter.send = displace
    runner._is_session_run_current = lambda *a: current[0]
    assert await runner._hmwa_run_session_hygiene(*args) is args[4]
    runner._hmwa_hygiene_detached_attempt.assert_not_awaited()
    assert runner._session_state("key").turn.progress_compositor is successor


@pytest.mark.asyncio
async def test_pending_deferral_edit_is_bounded_and_handoff_keeps_status(monkeypatch):
    from gateway.progress_compositor import ProgressCompositor
    monkeypatch.setattr(ProgressCompositor, "TRANSPORT_TIMEOUT_SECONDS", .01, raising=False)
    adapter = CaptureAdapter()
    runner, args = hygiene_display_runner(adapter)
    await runner._hmwa_run_session_hygiene(*args)
    compositor = runner._session_state("key").turn.progress_compositor
    cancelled = asyncio.Event()
    async def pending(**kw):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    adapter.edit_message = pending
    runner._hmwa_hygiene_stamp = lambda *a: None
    runner._hmwa_hygiene_defer_cleanup = lambda *a, **kw: None
    future = SimpleNamespace(add_done_callback=lambda callback: None)
    attempt = SimpleNamespace(progress_compositor=compositor, agent=SimpleNamespace(session_id="session"), meta={}, wait_started=0,
        future=future, commit_fence=SimpleNamespace(commit_watermark_fenced=True, is_cancelled=False))
    hs = SimpleNamespace(failure_cooldown_seconds=-1)
    async def expired():
        try:
            raise HygieneTurnHoldExceeded()
        except HygieneTurnHoldExceeded:
            await runner._hmwa_hygiene_on_turn_hold(attempt, hs, args[2], "key", args[1])
    with pytest.raises(HygieneTurnHoldExceeded):
        await asyncio.wait_for(expired(), .2)
    assert cancelled.is_set()
    ctx = TurnContext(source=args[1], session_key="key", run_generation=1)
    inherited = await TurnRunner(runner, ctx).start_progress_compositor()
    assert inherited.status_line == "Compression still running; continuing with existing context"
    assert len(adapter.sent) == 1
