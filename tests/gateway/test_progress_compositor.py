"""Focused contract tests for the one-message progress compositor."""

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_compositor import ProgressCompositor


class CaptureAdapter:
    MAX_MESSAGE_LENGTH: int = 180
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
    visible = rendered.split("\n\n", 1)[1].splitlines()
    assert f"hidden: 0 tools / {12 - len(visible)} messages" in rendered.splitlines()[0]
    assert len(compositor.activity_items) == 12
    assert list(compositor.activity_kinds) == ["message"] * 12
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


def test_bus_items_form_an_append_only_typed_log():
    adapter = CaptureAdapter()
    adapter.MAX_MESSAGE_LENGTH = 2000
    compositor = ProgressCompositor(adapter, "thread-1")
    compositor.absorb(("__status__", "first status"))
    compositor.absorb(("__tool__", "search"))
    compositor.absorb(("__dedup__", "search", 1))
    compositor.absorb(("__interim__", "interim"))
    compositor.absorb(("__steer__", "steering"))
    compositor.absorb(("__reset__",))
    compositor.absorb(("__status__", "latest status"))
    compositor.publish_activity("direct tool", kind="tool")
    compositor.publish_activity("search")

    assert list(compositor.activity_items) == [
        "search", "search (×2)", "interim", "steering", "direct tool", "search",
    ]
    assert list(compositor.activity_kinds) == [
        "tool", "message", "message", "message", "tool", "message",
    ]
    assert compositor._fit_render() == (
        "latest status\n\nsearch\nsearch (×2)\ninterim\nsteering\ndirect tool\nsearch"
    )


def test_rendering_preserves_full_log_and_recovers_it_when_budget_grows():
    compositor = ProgressCompositor(CaptureAdapter(), "thread-1")
    compositor.publish_status("current status")
    for index in range(20):
        compositor.publish_activity(f"event-{index} " + "x" * 40, kind="tool")
    original = list(compositor.activity_items)
    kinds = list(compositor.activity_kinds)

    first = compositor._fit_render()
    assert compositor._fit_render() == first
    compositor.publish_status("new status")
    assert compositor._fit_render().startswith("new status")
    assert list(compositor.activity_items) == original
    assert list(compositor.activity_kinds) == kinds
    assert compositor.status_line == "new status"

    compositor._text_limit = 2000
    restored = compositor._fit_render()
    assert restored == "new status\n\n" + "\n".join(original)
    assert "hidden:" not in restored


def test_hidden_counts_use_event_kinds_not_message_text_or_line_count():
    compositor = ProgressCompositor(CaptureAdapter(), "thread-1")
    compositor.publish_status("status")
    compositor.absorb(("__tool__", "plain tool\n" + "x" * 100))
    compositor.absorb(("__interim__", "🔍 Searching docs\n" + "x" * 100))
    compositor.absorb(("__steer__", "steer\n" + "x" * 100))
    compositor.absorb(("__tool__", "newest tool"))

    assert compositor._fit_render() == (
        "status · hidden: 1 tools / 2 messages\n\nnewest tool"
    )


def test_oversized_newest_event_is_clipped_but_not_counted_as_hidden():
    compositor = ProgressCompositor(CaptureAdapter(), "thread-1")
    compositor.publish_status("status")
    compositor.publish_activity("old tool", kind="tool")
    compositor.publish_activity("old message")
    oversized = "start " + "x" * 1000 + " END"
    compositor.publish_activity(oversized, kind="tool")

    rendered = compositor._fit_render()
    assert rendered.startswith("status · hidden: 1 tools / 1 messages\n\nstart ")
    assert " … " in rendered
    assert rendered.endswith(" END")
    assert compositor._formatted_len(rendered) <= compositor._text_limit
    assert list(compositor.activity_items) == ["old tool", "old message", oversized]
    assert compositor._fit_render() == rendered


def test_overflow_keeps_a_contiguous_suffix_not_short_older_entries():
    compositor = ProgressCompositor(CaptureAdapter(), "thread-1")
    compositor.publish_activity("old short")
    compositor.publish_activity("x" * 1000)
    compositor.publish_activity("new short")

    assert compositor._fit_render() == (
        "⏳ Working… · hidden: 0 tools / 2 messages\n\nnew short"
    )


@pytest.mark.parametrize("limit", [40, 48, 64])
def test_small_budget_preserves_complete_counts_before_status_or_activity(limit):
    adapter = CaptureAdapter()
    adapter.MAX_MESSAGE_LENGTH = limit
    compositor = ProgressCompositor(adapter, "thread-1")
    compositor.publish_status("large status " * 100)
    compositor.publish_activity("old", kind="tool")
    compositor.publish_activity("large event " * 100 + "END")

    rendered = compositor._fit_render()
    assert "hidden: 1 tools / 0 messages" in rendered.splitlines()[0]
    assert " … " in rendered
    assert compositor._formatted_len(rendered) <= compositor._text_limit


@pytest.mark.parametrize("limit", [1, 2, 4, 8, 16, 32, 64, 128, 180])
@pytest.mark.parametrize("with_activity", [False, True])
@pytest.mark.asyncio
async def test_initial_send_and_edits_bound_pathological_status(limit, with_activity):
    adapter = CaptureAdapter()
    adapter.MAX_MESSAGE_LENGTH = limit
    compositor = ProgressCompositor(adapter, "thread-1")
    status = "huge status " * 100
    compositor.publish_status(status)
    if with_activity:
        compositor.publish_activity("old", kind="tool")
        compositor.publish_activity("large activity " * 100 + "END")
    original = list(compositor.activity_items)
    await compositor.start()
    compositor.publish_status("replacement " * 100)
    await compositor.flush(force=True)

    for rendered in [adapter.sent[0][1], *(edit[2] for edit in adapter.edits)]:
        assert compositor._formatted_len(rendered) <= compositor._text_limit
        assert not rendered.startswith("\n")
        assert "\n\n\n" not in rendered
    assert list(compositor.activity_items) == original
    assert compositor.status_line == ("replacement " * 100).strip()
    assert len(adapter.sent) == 1


def test_fitting_honors_adapter_formatting_and_custom_length_function():
    class InflatingAdapter(CaptureAdapter):
        MAX_MESSAGE_LENGTH = 200

        def format_message(self, content):
            return "[" + content.replace("*", "\\*") + "]"

        def message_len_fn_for_chat(self, _chat_id):
            return lambda text: len(text.encode("utf-16-le")) // 2

    compositor = ProgressCompositor(InflatingAdapter(), "thread-1")
    compositor.publish_status("⏳ Status " + "*" * 200)
    compositor.publish_activity("old", kind="tool")
    newest = "*😀" * 200 + " END"
    compositor.publish_activity(newest)

    rendered = compositor._fit_render()
    assert compositor._formatted_len(rendered) <= compositor._text_limit
    assert "hidden: 1 tools / 0 messages" in rendered.splitlines()[0]
    assert " … " in rendered
    assert list(compositor.activity_items) == ["old", newest]


@pytest.mark.asyncio
async def test_metadata_is_preserved_on_send_and_edit_without_a_second_send():
    adapter = CaptureAdapter()
    metadata = {"_interim_send": True, "_nonconversational": True}
    compositor = ProgressCompositor(adapter, "thread-1", metadata=metadata, generation=7)
    await compositor.start()
    await compositor.start()
    compositor.publish_activity("tool", kind="tool")
    await compositor.flush(force=True)

    assert len(adapter.sent) == 1
    assert adapter.sent[0][3] == metadata
    assert adapter.edits[0][3] == {"metadata": metadata}
    assert compositor.generation == 7


@pytest.mark.asyncio
async def test_retryable_edit_backoff_keeps_canonical_state_and_newest_render():
    now = [0.0]
    adapter = CaptureAdapter([
        SendResult(success=False, retryable=True, retry_after=5),
        SendResult(success=True, message_id="progress-1"),
    ])
    compositor = ProgressCompositor(adapter, "thread-1", clock=lambda: now[0])
    await compositor.start()
    compositor.publish_activity("first")
    assert await compositor.flush() is False
    compositor.publish_activity("latest")
    now[0] = 4.0
    assert await compositor.flush() is False
    assert len(adapter.edits) == 1
    now[0] = 5.0
    assert await compositor.flush() is True
    assert adapter.edits[-1][2].endswith("first\nlatest")
    assert list(compositor.activity_items) == ["first", "latest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("already_started", [False, True])
async def test_closed_compositor_never_sends_or_edits(already_started):
    adapter = CaptureAdapter()
    compositor = ProgressCompositor(adapter, "thread-1")
    assert compositor.closed is False
    if already_started:
        await compositor.start()
    compositor.publish_activity("pending tool", kind="tool")
    compositor.closed = True
    compositor.publish_status("late status")
    compositor.publish_activity("late message")

    assert await compositor.start() is None
    assert await compositor.flush() is None
    assert await compositor.flush(force=True) is None
    assert len(adapter.sent) == int(already_started)
    assert adapter.edits == []
    assert compositor.message_id == ("progress-1" if already_started else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["send", "edit"])
async def test_transport_timeouts_do_not_create_replacement_messages(during):
    import asyncio

    class HangingAdapter(CaptureAdapter):
        async def send(self, *args, **kwargs):
            if during == "send":
                self.sent.append(args)
                await asyncio.Event().wait()
            return await super().send(*args, **kwargs)

        async def edit_message(self, *args, **kwargs):
            if during == "edit":
                self.edits.append(kwargs)
                await asyncio.Event().wait()
            return await super().edit_message(*args, **kwargs)

    adapter = HangingAdapter()
    compositor = ProgressCompositor(adapter, "thread-1")
    compositor.TRANSPORT_TIMEOUT_SECONDS = 0.01
    result = await compositor.start()
    if during == "send":
        assert result.success is False
    else:
        compositor.publish_activity("new")
        assert await compositor.flush(force=True) is False
    await compositor.start()
    assert compositor.editing_disabled is True
    assert len(adapter.sent) == 1