"""Focused contract tests for the one-message progress compositor."""

from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_compositor import ProgressCompositor


class CaptureAdapter:
    MAX_MESSAGE_LENGTH = 180
    name = "discord"

    def __init__(self, edit_results=None):
        self.sent = []
        self.edits = []
        self.edit_results = list(edit_results or [])

    def format_message(self, content):
        return content

    def message_len_fn_for_chat(self, _chat_id):
        return len

    def max_message_length_for_chat(self, _chat_id):
        return self.MAX_MESSAGE_LENGTH

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="progress-1")

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edits.append((chat_id, message_id, content, kwargs))
        if self.edit_results:
            return self.edit_results.pop(0)
        return SendResult(success=True, message_id=message_id)


@pytest.mark.asyncio
async def test_start_posts_immediate_owned_reply():
    adapter = CaptureAdapter()
    compositor = ProgressCompositor(adapter, "thread-1", reply_to="user-1", metadata={"thread_id": "thread-1"})

    result = await compositor.start()

    assert result.success is True
    assert compositor.message_id == "progress-1"
    assert adapter.sent == [("thread-1", "⏳ Working…", "user-1", {"thread_id": "thread-1"})]


@pytest.mark.asyncio
async def test_all_updates_edit_the_one_owned_message():
    adapter = CaptureAdapter()
    compositor = ProgressCompositor(adapter, "thread-1", reply_to="user-1")
    await compositor.start()

    compositor.publish_activity("🔍 Searching docs")
    await compositor.flush(force=True)
    compositor.publish_status("⏳ Working — 3 min — iteration 6/20")
    compositor.publish_activity("🖥 Reading result details")
    await compositor.flush(force=True)

    assert len(adapter.sent) == 1
    assert {edit[1] for edit in adapter.edits} == {"progress-1"}
    assert "⏳ Working — 3 min — iteration 6/20" in adapter.edits[-1][2]
    assert "🖥 Reading result details" in adapter.edits[-1][2]


@pytest.mark.asyncio
async def test_overflow_keeps_newest_updates_without_continuations():
    adapter = CaptureAdapter()
    compositor = ProgressCompositor(adapter, "thread-1")
    await compositor.start()

    for idx in range(12):
        compositor.publish_activity(f"🔍 update-{idx} " + ("x" * 24))
    await compositor.flush(force=True)

    rendered = adapter.edits[-1][2]
    assert len(rendered) <= adapter.MAX_MESSAGE_LENGTH
    assert "update-11" in rendered
    assert "earlier updates omitted" in rendered
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_retryable_edit_retries_same_id_with_latest_state(monkeypatch):
    adapter = CaptureAdapter([
        SendResult(success=False, error="rate limited", retryable=True, retry_after=0),
        SendResult(success=True, message_id="progress-1"),
    ])
    compositor = ProgressCompositor(adapter, "thread-1")
    await compositor.start()

    compositor.publish_activity("first")
    assert await compositor.flush(force=True) is False
    compositor.publish_activity("latest")
    assert await compositor.flush(force=True) is True

    assert len(adapter.sent) == 1
    assert [edit[1] for edit in adapter.edits] == ["progress-1", "progress-1"]
    assert "latest" in adapter.edits[-1][2]


@pytest.mark.asyncio
async def test_permanent_edit_failure_disables_updates_without_replacement():
    adapter = CaptureAdapter([
        SendResult(success=False, error="Unknown Message", retryable=False, error_kind="permanent"),
    ])
    compositor = ProgressCompositor(adapter, "thread-1")
    await compositor.start()

    compositor.publish_activity("first")
    assert await compositor.flush(force=True) is False
    compositor.publish_activity("second")
    assert await compositor.flush(force=True) is False

    assert compositor.editing_disabled is True
    assert len(adapter.sent) == 1
    assert len(adapter.edits) == 1