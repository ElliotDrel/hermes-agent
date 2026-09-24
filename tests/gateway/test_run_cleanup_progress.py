"""Tests for opt-in cleanup of temporary progress bubbles.

When ``display.platforms.<plat>.cleanup_progress: true`` is set for a
platform whose adapter supports message deletion (e.g. Telegram), the
tool-progress bubble, "⏳ Working — N min" heartbeats, and status-callback
messages sent during a run are deleted after the final response is
delivered.

Failed runs skip cleanup so the bubbles remain as breadcrumbs.
Adapters without ``delete_message`` silently no-op.
"""

import asyncio
import importlib
import inspect as _inspect
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig


async def _fire_post_delivery_cb(cb):
    """Invoke a popped post-delivery callback, awaiting if it's async.

    Chained registrations return an async wrapper; single registrations
    return the raw sync callable. Either way, await any awaitable result.
    """
    result = cb()
    if _inspect.isawaitable(result):
        await result
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from gateway.turn_context import TurnContext


# ---------------------------------------------------------------------------
# Test fakes — mirror those in test_run_progress_topics.py but add a
# delete_message implementation that records ids instead of hitting a bot.
# ---------------------------------------------------------------------------


class CleanupCaptureAdapter(BasePlatformAdapter):
    """Adapter that records every delete_message call for inspection."""

    _next_mid = 100

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []
        self.edits = []
        self.deleted = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    def _mint_id(self) -> str:
        CleanupCaptureAdapter._next_mid += 1
        return str(CleanupCaptureAdapter._next_mid)

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        mid = self._mint_id()
        self.sent.append(
            {"chat_id": chat_id, "content": content, "message_id": mid, "metadata": metadata}
        )
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": str(message_id)})
        return True

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class NoDeleteAdapter(CleanupCaptureAdapter):
    """Adapter that inherits the base no-op delete_message (used to prove
    the cleanup path skips adapters without deletion support)."""

    async def delete_message(self, chat_id, message_id) -> bool:  # type: ignore[override]
        # Pretend to be an adapter whose platform doesn't support deletion:
        # match the base class behavior exactly. gateway/run.py checks
        # ``type(adapter).delete_message is BasePlatformAdapter.delete_message``
        # to detect this, so we re-assign at class body level below.
        raise AssertionError("should not be called — cleanup must skip this adapter")


# Re-bind so the class's delete_message identity equals the base's.
NoDeleteAdapter.delete_message = BasePlatformAdapter.delete_message


class ProgressAgent:
    """Emits two tool-progress events and returns a normal final response."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.2)
            cb("tool.started", "terminal", "ls", {})
            time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class FailingAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.2)
        # Empty final_response + failed=True is the shape the gateway
        # actually returns on provider errors (see gateway/run.py where
        # failed keys are only propagated when final_response is empty).
        return {
            "final_response": "",
            "messages": [],
            "api_calls": 1,
            "failed": True,
            "error": "simulated provider failure",
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def _install_fakes(
    monkeypatch,
    agent_cls,
    *,
    cleanup_on: bool,
    cleanup_platform: Platform = Platform.TELEGRAM,
):
    """Wire up the module stubs every _run_agent test needs."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 — register tool emoji

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    # Wire the per-platform cleanup_progress flag via the config loader the
    # gateway actually reads (``_load_gateway_config`` returns user config).
    cfg = {
        "display": {
            "platforms": {
                cleanup_platform.value: {"cleanup_progress": True},
            }
        }
    } if cleanup_on else {}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    return gateway_run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_messaging_agent_forwards_checkpoint_config(monkeypatch, tmp_path):
    """Writable gateway agents must receive the configured checkpoint limits."""
    captured = {}

    class CheckpointCaptureAgent(ProgressAgent):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(
        monkeypatch, CheckpointCaptureAgent, cleanup_on=False,
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "checkpoints": {
                "enabled": True,
                "max_snapshots": 9,
                "max_total_size_mb": 444,
                "max_file_size_mb": 6,
            }
        },
    )

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-checkpoints",
        session_key="agent:main:telegram:group:-1001",
    )

    assert result["final_response"] == "done"
    assert captured["checkpoints_enabled"] is True
    assert captured["checkpoint_max_snapshots"] == 9
    assert captured["checkpoint_max_total_size_mb"] == 444
    assert captured["checkpoint_max_file_size_mb"] == 6


@pytest.mark.asyncio
async def test_cleanup_chains_with_existing_callback(monkeypatch, tmp_path):
    """When a bg-review-style callback is already registered, the cleanup
    callback chains with it — both fire, neither clobbers the other."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    pre_existing_fired = []

    def _preexisting_callback() -> None:
        pre_existing_fired.append(True)

    # Pre-register a callback with the same generation the run will use
    # (run_generation=None in this test path — matches the default slot).
    adapter.register_post_delivery_callback(session_key, _preexisting_callback)

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break

    # Both effects land: the pre-existing callback fires AND the cleanup
    # deletes at least one progress bubble.
    assert pre_existing_fired == [True]
    assert len(adapter.deleted) >= 1


@pytest.mark.asyncio
async def test_compositor_cleanup_deletes_unique_owned_id_after_confirmed_delivery():
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    session_key = "agent:main:discord:thread:123:123"
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key=session_key,
        run_generation=7,
        _cleanup_progress=True,
        _cleanup_msg_ids=["progress-1", "progress-1"],
    )

    runner._run_agent_schedule_bubble_cleanup(
        {"final_response": "done", "completed": True}, adapter, ctx
    )
    callback = adapter.pop_post_delivery_callback(session_key, generation=7)
    assert callable(callback)
    await _fire_post_delivery_cb(callback)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break

    assert adapter.deleted == [{"chat_id": "123", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_cleanup_callback_waits_for_delete_before_returning():
    """A gateway drain immediately after final delivery must not outrun deletion."""
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key="agent:main:discord:thread:123:123", run_generation=7,
        _cleanup_progress=True, _cleanup_msg_ids=["progress-1"],
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_delete(chat_id, message_id):
        started.set()
        await release.wait()
        adapter.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return True

    adapter.delete_message = delayed_delete
    runner._run_agent_schedule_bubble_cleanup({"final_response": "done"}, adapter, ctx)
    callback = adapter.pop_post_delivery_callback(ctx.session_key, generation=7)
    task = asyncio.create_task(_fire_post_delivery_cb(callback))
    await asyncio.wait_for(started.wait(), 1)
    assert not task.done(), "cleanup callback returned before Discord deletion finished"
    release.set()
    await asyncio.wait_for(task, 1)
    assert adapter.deleted == [{"chat_id": "123", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_queued_first_answer_cleans_progress_after_confirmed_delivery():
    """The early queued-turn return still owns its progress message."""
    from unittest.mock import AsyncMock

    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    runner._deliver_queued_first_response = AsyncMock(return_value=True)
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key="agent:main:discord:thread:123:123", run_generation=7,
        _cleanup_progress=True, _cleanup_msg_ids=["progress-1"],
    )
    await runner._run_agent_deliver_first_response(
        ctx, adapter, {"final_response": "first", "completed": True},
        {"final_response": "first", "completed": True}, None,
    )
    assert adapter.deleted == [{"chat_id": "123", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_queued_failed_first_answer_keeps_progress():
    """A refused queued final must not delete the only visible breadcrumb."""
    from unittest.mock import AsyncMock

    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    runner._deliver_queued_first_response = AsyncMock(return_value=False)
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key="agent:main:discord:thread:123:123", run_generation=7,
        _cleanup_progress=True, _cleanup_msg_ids=["progress-1"],
    )
    await runner._run_agent_deliver_first_response(
        ctx, adapter, {"final_response": "first", "completed": True},
        {"final_response": "first", "completed": True}, None,
    )
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_cleanup_retries_a_refused_delete(caplog):
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key="agent:main:discord:thread:123:123", run_generation=7,
        _cleanup_progress=True, _cleanup_msg_ids=["progress-1"],
    )
    attempts = []

    async def delete(chat_id, message_id):
        attempts.append(message_id)
        if len(attempts) == 1:
            return False
        adapter.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return True

    adapter.delete_message = delete
    runner._run_agent_schedule_bubble_cleanup({"final_response": "done"}, adapter, ctx)
    callback = adapter.pop_post_delivery_callback(ctx.session_key, generation=7)
    await _fire_post_delivery_cb(callback)
    assert attempts == ["progress-1", "progress-1"]
    assert adapter.deleted == [{"chat_id": "123", "message_id": "progress-1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"final_response": "failed", "failed": True},
        {"final_response": "interrupted", "interrupted": True},
        {"final_response": "cancelled", "cancelled": True},
        {"final_response": "partial", "completed": False},
    ],
)
async def test_cleanup_is_not_registered_for_nonfinal_turns(response):
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    session_key = "agent:main:discord:thread:123:123"
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key=session_key,
        run_generation=9,
        _cleanup_progress=True,
        _cleanup_msg_ids=["progress-1"],
    )

    runner._run_agent_schedule_bubble_cleanup(response, adapter, ctx)

    assert adapter.pop_post_delivery_callback(session_key, generation=9) is None
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_cleanup_has_time_for_every_id_or_logs_unattempted_ids(monkeypatch, caplog):
    """Several hung deletes cannot silently outlive the post-delivery callback."""
    import gateway.platforms.base as base

    monkeypatch.setattr(base, "_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS", 1.3)
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="123", thread_id="123"),
        session_key="agent:main:discord:thread:123:123", run_generation=7,
        _cleanup_progress=True, _cleanup_msg_ids=["progress-1", "progress-2"],
    )
    attempts = []

    async def never_delete(chat_id, message_id):
        attempts.append(message_id)
        await asyncio.Event().wait()

    adapter.delete_message = never_delete
    runner._run_agent_schedule_bubble_cleanup({"final_response": "done"}, adapter, ctx)
    callback = adapter.pop_post_delivery_callback(ctx.session_key, generation=7)
    with caplog.at_level("WARNING"):
        await asyncio.wait_for(_fire_post_delivery_cb(callback), timeout=0.9)
    assert attempts == ["progress-1"]
    assert "progress-2" in caplog.text


@pytest.mark.asyncio
async def test_refused_stream_reconciliation_does_not_confirm_delivery():
    """A refused final edit must leave the normal delivery lane eligible."""
    from unittest.mock import AsyncMock

    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)
    adapter.edit_message = AsyncMock(return_value=SendResult(success=False, error="503"))
    runner = _make_runner(adapter)
    response = {"final_response": "transformed final", "response_transformed": True}
    stream = SimpleNamespace(adapter=adapter, message_id="preview-1")
    await runner._run_agent_edit_streamed_message(
        stream, SessionSource(platform=Platform.DISCORD, chat_id="123"), response,
        "transformed final", _sk="session-1", ok=("edited %s", "preview-1"),
        fail_result=None, fail_exc="edit failed for %s: %s",
    )
    assert response.get("already_sent") is not True


@pytest.mark.asyncio
async def test_streamed_final_delivery_cleans_progress_without_duplicate_send():
    """A confirmed stream returns None from the handler, but still completed delivery."""
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)

    async def handler(event):
        event._streamed_final_response = "final answer already sent"
        return None

    adapter.set_message_handler(handler)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread")
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source)
    session_key = build_session_key(source)

    async def cleanup():
        await adapter.delete_message("123", "progress-1")

    adapter.register_post_delivery_callback(session_key, cleanup)
    await adapter._process_message_background(event, session_key)
    assert adapter.deleted == [{"chat_id": "123", "message_id": "progress-1"}]
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_failed_final_delivery_preserves_registered_progress_breadcrumb():
    """A failed adapter send must not fire cleanup registered for final delivery."""
    adapter = CleanupCaptureAdapter(platform=Platform.DISCORD)

    async def failed_send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="simulated delivery failure")

    async def handler(_event):
        return "final answer"

    adapter.send = failed_send
    adapter.set_message_handler(handler)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread")
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source)
    session_key = build_session_key(source)

    async def cleanup():
        await adapter.delete_message("123", "progress-1")

    adapter.register_post_delivery_callback(session_key, cleanup)
    await adapter._process_message_background(event, session_key)

    assert adapter.deleted == []
    assert adapter.pop_post_delivery_callback(session_key) is not None
