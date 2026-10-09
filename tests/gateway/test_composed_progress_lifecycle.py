"""Presentation-only Discord progress through the real turn/configuration consumers."""
import asyncio
import copy
import importlib
import queue
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from gateway.config import Platform
from gateway.display_config import resolve_display_setting
from gateway.platforms.base import SendResult
from gateway.progress_compositor import ProgressCompositor
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from tests.gateway.test_run_cleanup_progress import CleanupCaptureAdapter, _install_fakes, _make_runner


class LifecycleAdapter(CleanupCaptureAdapter):
    MAX_MESSAGE_LENGTH = 2000
    supports_code_blocks = True

    def __init__(self, platform=Platform.DISCORD):
        super().__init__(platform)
        self.operations = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        result = await super().send(chat_id, content, reply_to, metadata)
        self.operations.append(("send", result.message_id, content))
        return result

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.operations.append(("edit", message_id, content))
        return await super().edit_message(chat_id, message_id, content)

    async def delete_message(self, chat_id, message_id):
        self.operations.append(("delete", str(message_id), ""))
        return await super().delete_message(chat_id, message_id)


class LifecycleAgent:
    final_text = "[SILENT]"
    captured = []

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = None
        self.status_callback = None
        self.interim_assistant_callback = None
        self.context_compressor = SimpleNamespace(last_prompt_tokens=32000, last_real_prompt_tokens=32000, context_length=128000)

    def get_activity_summary(self):
        return {"api_call_count": 5, "current_tool": "read_file"}

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        self.captured.append((message, copy.deepcopy(conversation_history)))
        command = "python -m pytest tests/gateway/test_composed_progress_lifecycle.py --verbose"
        self.tool_progress_callback("tool.started", "terminal", command, {"command": command})
        self.tool_progress_callback("tool.started", "terminal", command, {"command": command})
        from agent.conversation_compression import COMPACTION_STATUS, COMPACTION_DONE_STATUS
        self.status_callback("info", COMPACTION_STATUS)
        self.status_callback("compacted", COMPACTION_DONE_STATUS)
        if self.interim_assistant_callback:
            self.interim_assistant_callback("Checking the final result.")
        time.sleep(0.2)
        return {"final_response": self.final_text, "messages": [], "api_calls": 5}


def install_configured_runner(monkeypatch, tmp_path, config, agent_cls=LifecycleAgent):
    gateway_run = importlib.import_module("gateway.run")
    real_loader = gateway_run._load_gateway_config
    _install_fakes(monkeypatch, agent_cls, cleanup_on=False, cleanup_platform=Platform.DISCORD)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", real_loader)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    return runner, adapter


def composed_config(**overrides):
    return {"display": {"platforms": {"discord": {
        "progress_compositor": "single_message", "tool_progress": "all", **overrides,
    }}}}


def make_context(runner, adapter, **kwargs):
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread", thread_id="thread")
    ctx = TurnContext(source=source, session_key="key", run_generation=1,
                      progress_compositor_mode="single_message", progress_queue=queue.Queue(),
                      resolve_display_setting=resolve_display_setting, user_config={},
                      _run_still_current=lambda: True, _status_adapter=adapter,
                      _status_chat_id="thread", **kwargs)
    return ctx, TurnRunner(runner, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["[SILENT]", "A material heartbeat update."])
async def test_heartbeat_normal_progress_is_deleted_on_completion(monkeypatch, tmp_path, final):
    class Agent(LifecycleAgent):
        final_text = final
    runner, adapter = install_configured_runner(monkeypatch, tmp_path, composed_config(), Agent)
    history = [{"role": "user", "content": "original"}, {"role": "assistant", "content": "answer"}]
    original = copy.deepcopy(history)
    result = await runner._run_agent(message="heartbeat", context_prompt="", history=history,
                                    source=SessionSource(platform=Platform.DISCORD, chat_id="thread", thread_id="thread"),
                                    session_id="session", session_key="key", scheduled_heartbeat=True,
                                    persist_user_display_kind="internal_notification")
    assert result["final_response"] == final
    assert history == original
    assert len(adapter.sent) == 1
    progress_id = adapter.sent[0]["message_id"]
    assert adapter.deleted == [{"chat_id": "thread", "message_id": progress_id}]
    assert adapter.operations[-1][0] == "delete"
    assert adapter.sent[0]["metadata"]["non_conversational"] is True
    rendered = "\n".join(edit["content"] for edit in adapter.edits)
    assert "terminal:" in rendered and "--verbose" in rendered
    assert "Compacting context" in rendered and "compaction complete" in rendered
    assert "Checking the final result." in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, "off", "false", "0"])
async def test_config_loader_to_heartbeat_consumer_can_hide_progress(monkeypatch, tmp_path, value):
    runner, adapter = install_configured_runner(monkeypatch, tmp_path, composed_config(heartbeat_progress=value))
    result = await runner._run_agent(message="heartbeat", context_prompt="", history=[],
                                    source=SessionSource(platform=Platform.DISCORD, chat_id="thread"),
                                    session_id="session", session_key="key", scheduled_heartbeat=True)
    assert result["final_response"] == "[SILENT]"
    assert adapter.sent == adapter.edits == adapter.deleted == []


@pytest.mark.asyncio
async def test_ordinary_progress_waits_for_actual_final_delivery(monkeypatch, tmp_path):
    class Agent(LifecycleAgent):
        final_text = "Done."
    runner, adapter = install_configured_runner(monkeypatch, tmp_path, composed_config(cleanup_progress=False), Agent)
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread")
    result = await runner._run_agent(message="human", context_prompt="", history=[], source=source,
                                    session_id="session", session_key="key")
    assert result["final_response"] == "Done."
    assert adapter.deleted == []
    await adapter.send("thread", result["final_response"])
    callback = adapter.pop_post_delivery_callback("key")
    assert callback is not None
    await callback()
    assert [op[0] for op in adapter.operations][-2:] == ["send", "delete"]
    assert adapter.deleted[0]["message_id"] == adapter.sent[0]["message_id"]
    assert adapter.deleted[0]["message_id"] != adapter.sent[-1]["message_id"]


@pytest.mark.asyncio
async def test_silent_cleanup_never_fires_unrelated_post_delivery_callbacks(monkeypatch, tmp_path):
    runner, adapter = install_configured_runner(monkeypatch, tmp_path, composed_config())
    calls = []
    adapter.register_post_delivery_callback("key", lambda: calls.append("delivered"))
    await runner._run_agent(message="heartbeat", context_prompt="", history=[],
                            source=SessionSource(platform=Platform.DISCORD, chat_id="thread"),
                            session_id="session", session_key="key", scheduled_heartbeat=True)
    assert calls == []
    assert adapter.deleted
    assert callable(adapter.pop_post_delivery_callback("key"))


@pytest.mark.asyncio
async def test_queued_refusal_keeps_callback_and_owned_progress():
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    runner._deliver_queued_first_response = AsyncMock(return_value=False)
    ctx, _ = make_context(runner, adapter, _cleanup_progress=True, _cleanup_msg_ids=["first-progress"])
    calls = []
    adapter.register_post_delivery_callback("key", lambda: calls.append("delivered"), generation=1)
    result = {"final_response": "first answer"}
    await runner._run_agent_deliver_first_response(ctx, adapter, result, result, None)
    assert calls == [] and adapter.deleted == []
    assert callable(adapter.pop_post_delivery_callback("key", generation=1))


@pytest.mark.asyncio
async def test_two_queued_compositors_cleanup_only_their_own_ids():
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    first, first_runner = make_context(runner, adapter, _cleanup_progress=True)
    await first_runner.start_progress_compositor()
    first.tool_progress_enabled = True
    first.progress_mode = "all"
    first_runner.progress_callback("tool.started", "terminal", "one", {"command": "one"})
    first.progress_compositor.absorb(first.progress_queue.get_nowait())
    await first.progress_compositor.flush(force=True)
    async def deliver(text, **kwargs):
        await adapter.send("thread", text)
        return True
    runner._deliver_queued_first_response = deliver
    await runner._run_agent_deliver_first_response(first, adapter, {"final_response": "first answer"}, {}, None)
    second, second_runner = make_context(runner, adapter, _cleanup_progress=True)
    second.run_generation = 2
    await second_runner.start_progress_compositor()
    first.progress_compositor.publish_activity("late first event")
    await first.progress_compositor.flush(force=True)
    assert first.progress_compositor.closed
    second_result = {"final_response": "second answer"}
    await runner._run_agent_deliver_first_response(second, adapter, second_result, second_result, None)
    sends = [op for op in adapter.operations if op[0] == "send"]
    assert len(sends) == 4
    assert [d["message_id"] for d in adapter.deleted] == [sends[0][1], sends[2][1]]
    assert [op[0] for op in adapter.operations] == ["send", "edit", "send", "delete", "send", "send", "delete"]


@pytest.mark.asyncio
async def test_delivered_terminal_error_cleans_composed_progress():
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter, _cleanup_progress=True)
    await turn.start_progress_compositor()
    runner._run_agent_schedule_bubble_cleanup({"failed": True, "final_response": "Error, retry."}, adapter, ctx)
    assert adapter.deleted == []
    await adapter.send("thread", "Error, retry.")
    callback = adapter.pop_post_delivery_callback("key", generation=1)
    assert callback is not None
    await callback()
    assert adapter.deleted == [{"chat_id": "thread", "message_id": adapter.sent[0]["message_id"]}]


def test_tool_events_keep_useful_preview_and_repeated_calls():
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter)
    ctx.tool_progress_enabled, ctx.progress_mode = True, "new"
    command = "python -m pytest tests/gateway/test_composed_progress_lifecycle.py --verbose"
    for _ in range(2):
        turn.progress_callback("tool.started", "terminal", "truncated", {"command": command})
    events = [ctx.progress_queue.get_nowait(), ctx.progress_queue.get_nowait()]
    assert all(item[0] == "__tool__" and command in item[1] for item in events)
    assert ctx.progress_queue.empty()


def test_status_reads_provider_context_and_marks_estimates(monkeypatch):
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter)
    ctx.agent_holder[0] = LifecycleAgent()
    ctx.progress_started_at = 0
    monkeypatch.setattr("gateway.run_turn_runner.time.monotonic", lambda: 600)
    assert turn._composed_status() == "⏳ Working 10m · iteration 5 · read_file · context 32.0k/128k (25%)"
    ctx.agent_holder[0].context_compressor.last_prompt_tokens = 16000
    assert "context ~16.0k/128k" in turn._composed_status()


@pytest.mark.asyncio
async def test_header_refreshes_each_minute_without_mutating_activity(monkeypatch):
    import gateway.run_turn_runner as module
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter)
    ctx.agent_holder[0] = LifecycleAgent()
    clock = [0.0]
    ctx.progress_started_at = 0
    ctx._run_still_current = lambda: clock[0] < 70
    compositor = ProgressCompositor(adapter, "thread", clock=lambda: clock[0])
    ctx.progress_compositor = compositor
    await compositor.start()
    headers = []
    original_publish = compositor.publish_status
    def publish(text):
        headers.append(text)
        original_publish(text)
    compositor.publish_status = publish
    compositor.publish_activity("Earlier activity")
    real_sleep = asyncio.sleep
    async def tick(_delay):
        clock[0] += 10
        await real_sleep(0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", tick)
    await turn._send_composed_progress(adapter)
    assert len(headers) == 2
    assert "Working 0m" in headers[0] and "Working 1m" in headers[1]
    assert list(compositor.activity_items) == ["Earlier activity"]


@pytest.mark.asyncio
async def test_closed_precompression_compositor_is_not_reused_by_same_generation_successor():
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    first, turn = make_context(runner, adapter, _cleanup_progress=True)
    await turn.start_progress_compositor()
    runner._session_state("key").turn.progress_compositor = first.progress_compositor
    await runner._run_agent_bubble_cleanup(adapter, first)()
    second, successor = make_context(runner, adapter, _cleanup_progress=True)
    await successor.start_progress_compositor()
    assert second.progress_compositor is not first.progress_compositor
    assert not second.progress_compositor.closed
    assert len(adapter.sent) == 2
    assert second._cleanup_msg_ids == [adapter.sent[1]["message_id"]]


@pytest.mark.asyncio
async def test_real_queued_followup_deletes_first_progress_before_starting_second(monkeypatch, tmp_path):
    from tests.gateway.test_run_progress_topics import _run_with_agent
    class Agent(LifecycleAgent):
        calls = 0
        def run_conversation(self, *args, **kwargs):
            type(self).calls += 1
            self.final_text = "first answer" if self.calls == 1 else "second answer"
            return super().run_conversation(*args, **kwargs)
    adapter, result = await _run_with_agent(
        monkeypatch, tmp_path, Agent, session_id="queued-lifecycle", pending_text="follow-up",
        config_data=composed_config(), platform=Platform.DISCORD, chat_id="thread",
        thread_id="thread", adapter_cls=LifecycleAdapter,
    )
    assert Agent.calls == 2 and result["final_response"] == "second answer"
    assert len(adapter.sent) == 3
    assert adapter.sent[1]["content"] == "first answer"
    assert adapter.deleted == [{"chat_id": "thread", "message_id": adapter.sent[0]["message_id"]}]
    first_delete = next(i for i, op in enumerate(adapter.operations) if op[0] == "delete")
    second_send = next(i for i, op in enumerate(adapter.operations) if op[0] == "send" and op[1] == adapter.sent[2]["message_id"])
    assert first_delete < second_send
    await adapter.send("thread", result["final_response"])
    callback = adapter.pop_post_delivery_callback("agent:main:discord:group:thread:thread")
    assert callback is not None
    await callback()
    assert [item["message_id"] for item in adapter.deleted] == [adapter.sent[0]["message_id"], adapter.sent[2]["message_id"]]


@pytest.mark.parametrize("succeeded", [True, False])
def test_compression_savings_require_a_new_completed_compression(succeeded):
    from agent.conversation_compression import COMPACTION_STATUS, COMPACTION_DONE_STATUS
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter)
    ctx.agent_holder[0] = LifecycleAgent()
    compressor = ctx.agent_holder[0].context_compressor
    compressor.compression_count = 1
    compressor._last_compression_savings_pct = 50.0
    turn._status_callback_sync("info", COMPACTION_STATUS)
    if succeeded:
        compressor.compression_count += 1
    turn._status_callback_sync("compacted", COMPACTION_DONE_STATUS)
    assert "Compacting context" in ctx.progress_queue.get_nowait()[1]
    done = ctx.progress_queue.get_nowait()[1]
    assert ("context saved 50%" in done) is succeeded
    assert ctx.progress_compression_count is None


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "git status && python -m pytest --verbose"}),
    ("execute_code", {"code": "from hermes_tools import read_file\nprint(read_file('gateway/run_turn.py'))"}),
    ("web", {"search_query": [{"q": "Hermes progress lifecycle"}]}),
])
def test_previews_preserve_commands_code_and_unknown_tool_arguments(tool, args):
    adapter = LifecycleAdapter()
    runner = _make_runner(adapter)
    ctx, turn = make_context(runner, adapter)
    text = turn._progress_build_message(tool, "shortened upstream preview", args)
    assert tool + ":" in text
    assert "shortened upstream preview" not in text
    expected = next(iter(args.values())) if tool != "web" else "Hermes progress lifecycle"
    assert isinstance(text, str) and expected in text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False, "off"])
async def test_preagent_heartbeat_progress_setting_includes_hold_expiry(enabled):
    from gateway.run import HygieneTurnHoldExceeded
    from tests.gateway.test_compression_progress import hygiene_display_runner
    adapter = LifecycleAdapter()
    runner, args = hygiene_display_runner(adapter)
    hs = runner._hmwa_hygiene_settings.return_value
    hs.data = composed_config(heartbeat_progress=enabled)
    hs.failure_cooldown_seconds = -1
    args[0]._heartbeat_session_id = "session"
    runner._hmwa_hygiene_stamp = lambda *a: None
    runner._hmwa_hygiene_defer_cleanup = lambda *a: None
    runner._hmwa_hygiene_notify = AsyncMock()
    attempts = []
    async def hold(attempt, *unused):
        attempts.append(attempt)
        attempt.agent = SimpleNamespace(session_id="session")
        attempt.future = SimpleNamespace(add_done_callback=lambda cb: None)
        attempt.commit_fence = SimpleNamespace(commit_watermark_fenced=True, is_cancelled=False)
        with pytest.raises(HygieneTurnHoldExceeded):
            await runner._hmwa_hygiene_on_turn_hold(attempt, hs, args[2], "key", args[1])
    runner._hmwa_hygiene_detached_attempt = hold
    history = copy.deepcopy(args[4])
    assert await runner._hmwa_run_session_hygiene(*args) == history
    assert len(attempts) == 1
    if enabled is True:
        assert len(adapter.sent) == 1 and adapter.edits
        assert "Compression still running" in adapter.edits[-1]["content"]
    else:
        assert attempts[0].progress_suppressed is True
        assert adapter.sent == adapter.edits == adapter.deleted == []
    runner._hmwa_hygiene_notify.assert_not_awaited()


@pytest.mark.parametrize("tool,args,expected", [
    ("read_file", {"path": "C:/a/long/project/source.py", "offset": 20, "limit": 30}, "source.py"),
    ("web_fetch", {"url": "https://example.com/a-long-document"}, "https://example.com/a-long-document"),
])
def test_previews_keep_file_url_targets_and_do_not_mutate_arguments(tool, args, expected):
    runner = _make_runner(LifecycleAdapter())
    ctx, turn = make_context(runner, runner.adapters[Platform.DISCORD])
    original = copy.deepcopy(args)
    text = turn._progress_build_message(tool, "short", args)
    assert expected in text and args == original
    ctx.user_config = composed_config(tool_preview_length=12)
    clipped = turn._progress_build_message(tool, "short", args)
    assert clipped.endswith("…") and args == original
