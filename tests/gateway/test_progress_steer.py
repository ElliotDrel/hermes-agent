"""Accepted mid-run steering appears in the ordered Discord progress timeline."""

import queue

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.progress_compositor import ProgressCompositor
from tests.gateway.test_progress_compositor import CaptureAdapter


class SteerAgent:
    def __init__(self, accepted=True):
        self.accepted = accepted

    def steer(self, text):
        return self.accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["command", "normal", "priority"])
async def test_accepted_steer_follows_prior_tool_and_precedes_next_tool(route):
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread", user_id="user")
    event = MessageEvent(text="/steer check the edge case" if route == "command" else "check the edge case", source=source)
    state = runner._session_state("key")
    state.turn.agent = SteerAgent()
    state.turn.progress_queue = queue.Queue()
    state.turn.progress_queue.put("🔧 previous tool")

    if route == "command":
        await runner._busy_steer_command(event, "key", source)
    elif route == "normal":
        outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", state.turn.agent)
        assert outcome.steered
    else:
        runner._hm_busy_steer(event, state.turn.agent, "key")
    state.turn.progress_queue.put("🔧 next tool")

    adapter = CaptureAdapter()
    compositor = ProgressCompositor(adapter, "thread")
    await compositor.start()
    while not state.turn.progress_queue.empty():
        compositor.absorb(state.turn.progress_queue.get_nowait())
    await compositor.flush(force=True)
    rendered = adapter.edits[-1][2]
    assert rendered.index("previous tool") < rendered.index("⏩ Steer received: check the edge case") < rendered.index("next tool")
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_rejected_steer_and_non_discord_do_not_publish_marker():
    for platform, accepted in ((Platform.DISCORD, False), (Platform.TELEGRAM, True)):
        runner = GatewayRunner(config=GatewayConfig())
        source = SessionSource(platform=platform, chat_id="thread")
        state = runner._session_state("key")
        state.turn.agent = SteerAgent(accepted)
        state.turn.progress_queue = queue.Queue()
        await runner._busy_steer_command(MessageEvent(text="/steer private detail", source=source), "key", source)
        assert state.turn.progress_queue.empty()


def test_turn_reset_drops_stale_progress_queue():
    runner = GatewayRunner(config=GatewayConfig())
    state = runner._session_state("key")
    state.turn.progress_queue = queue.Queue()
    state.turn.clear()
    assert state.turn.progress_queue is None


def test_steer_preview_is_bounded_and_dedup_does_not_erase_it():
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread")
    state = runner._session_state("key")
    state.turn.agent = agent = SteerAgent()
    state.turn.progress_queue = queue.Queue()
    runner._publish_steer_progress("key", MessageEvent(text="message", source=source), agent,
                                   "first line\n" + "x" * 200)
    marker = state.turn.progress_queue.get_nowait()
    assert marker[0] == "__steer__"
    assert "\n" not in marker[1]
    assert marker[1].endswith("…")
    assert len(marker[1].removeprefix("⏩ Steer received: ")) == 120

    compositor = ProgressCompositor(CaptureAdapter(), "thread")
    compositor.absorb("🔧 repeated tool")
    compositor.absorb(marker)
    compositor.absorb(("__dedup__", "🔧 repeated tool", 1))
    assert list(compositor.activity_items) == [
        "🔧 repeated tool", marker[1], "🔧 repeated tool (×2)",
    ]


def test_stale_agent_cannot_publish_to_replacement_turn():
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread")
    state = runner._session_state("key")
    old_agent = SteerAgent()
    state.turn.agent = SteerAgent()
    state.turn.progress_queue = queue.Queue()
    runner._publish_steer_progress("key", MessageEvent(text="message", source=source), old_agent, "stale")
    assert state.turn.progress_queue.empty()
