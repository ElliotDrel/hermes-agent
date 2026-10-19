"""Runtime notices through the real turn loop: a background result handed to a running turn reaches the
very next request as one typed nonhuman row after the newest tool results, never as the user's steer,
and leaves the cached prefix of the requests before it untouched. A notice no boundary took reads
unconsumed, so the gateway returns it to its own queue place."""

from __future__ import annotations

import copy
from itertools import pairwise
from types import SimpleNamespace

import pytest

from agent.prompt_builder import RUNTIME_NOTICE_CLOSE, RUNTIME_NOTICE_OPEN, STEER_MARKER_OPEN
from tests.agent.test_run_agent import _mock_response
# The probe tool and its in-tool hooks: the real tool executor runs it while the turn executes tools.
from tests.agent.test_turn_user_intervened import _DURING_TOOL, _PROBE, _loop_agent as _agent, _run


@pytest.fixture(autouse=True)
def _clear_tool_hooks():
    _DURING_TOOL.clear()
    yield
    _DURING_TOOL.clear()


def _script(tool_rounds: int, requests: list, during_final=None):
    """``tool_rounds`` probe calls, then a final reply; every request's wire messages are recorded."""
    def model_call(api_kwargs):
        requests.append(copy.deepcopy(api_kwargs["messages"]))
        if len(requests) <= tool_rounds:
            call = SimpleNamespace(id=f"c{len(requests)}", type="function",
                                   function=SimpleNamespace(name=_PROBE, arguments="{}"))
            return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[call])
        if during_final is not None:
            during_final()
        return _mock_response(content="all done", finish_reason="stop")
    return model_call


def _text(message):
    content = message.get("content")
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content or [])


def test_a_notice_reaches_the_next_request_as_a_typed_nonhuman_row_after_the_tool_results():
    agent, requests = _agent(), []
    _DURING_TOOL.append(lambda: agent.notify_runtime("[proc_1 finished: exit 0]", ticket="adm-1"))
    result = _run(agent, _script(1, requests))

    assert result["completed"] is True
    assert result["user_intervened"] is False, "a runtime notice is not the user stepping in"
    first, second = requests
    # Append-only: the request before the notice is a byte-equal prefix of the one carrying it.
    assert second[:len(first)] == first
    tail = second[len(first):]
    assert [m["role"] for m in tail] == ["assistant", "tool", "user"]
    notice = tail[-1]
    assert _text(notice) == f"{RUNTIME_NOTICE_OPEN}\n[proc_1 finished: exit 0]\n{RUNTIME_NOTICE_CLOSE}"
    assert STEER_MARKER_OPEN not in _text(notice), "never framed as the user's own words"
    assert "display_kind" not in notice, "presentation typing never reaches the provider"
    # Persisted typed, and consumed: the gateway settles the transfer with the turn.
    [stored] = [m for m in result["messages"] if m.get("display_kind") == "runtime_notice"]
    assert stored["role"] == "user"
    from agent.context_compressor import ContextCompressor, split_user_originated_turn
    assert ContextCompressor._is_actionable_user_turn(stored) is False
    assert split_user_originated_turn(stored) == (None, None)
    assert agent.withdraw_steer("adm-1") is False


def test_a_notice_after_the_last_boundary_is_never_sent_and_reads_unconsumed():
    agent, requests = _agent(), []
    offered = []
    result = _run(agent, _script(0, requests, during_final=lambda: offered.append(
        agent.notify_runtime("[late result]", ticket="adm-late"))))

    assert offered == [True]
    assert not any("[late result]" in _text(m) for request in requests for m in request)
    assert not any(m.get("display_kind") == "runtime_notice" for m in result["messages"])
    assert result["user_intervened"] is False and not result.get("pending_steer")
    # The finished turn is sealed: nothing more is taken until the next turn starts.
    assert agent.notify_runtime("[after the turn]", ticket="adm-after") is False
    assert agent.withdraw_steer("adm-late") is True, "unread: it goes back to its own queue place"


def test_a_notice_waits_behind_a_steer_at_the_same_boundary_instead_of_stacking_user_rows():
    agent, requests = _agent(), []

    def both():
        if len(requests) == 1:
            assert agent.steer("also check the logs") is True
            assert agent.notify_runtime("[proc_2 finished]", ticket="adm-2") is True
    _DURING_TOOL.append(both)
    result = _run(agent, _script(2, requests))

    assert result["completed"] is True
    second, third = requests[1], requests[2]
    assert second[-1]["role"] == "user" and STEER_MARKER_OPEN in _text(second[-1])
    assert not any("[proc_2 finished]" in _text(m) for m in second)
    assert third[:len(second)] == second
    assert [m["role"] for m in third[len(second):]] == ["assistant", "tool", "user"]
    assert "[proc_2 finished]" in _text(third[-1])
    for request in requests:
        roles = [m["role"] for m in request]
        assert ("user", "user") not in pairwise(roles), roles


def test_a_hard_stop_returns_unread_notices_and_a_codex_turn_takes_none():
    agent = _agent()
    from agent import runtime_notices
    runtime_notices.open_turn(agent)
    assert agent.notify_runtime("[result]", ticket="adm-3") is True
    agent.clear_interrupt(hard_cancel=True)
    assert agent.notify_runtime("[another]", ticket="adm-4") is False
    assert agent.withdraw_steer("adm-3") is True

    codex = _agent()
    codex.api_mode = "codex_app_server"
    runtime_notices.open_turn(codex)
    assert codex.notify_runtime("[result]", ticket="adm-5") is False


def test_late_human_steer_preserves_the_notice_request_and_its_unread_ticket():
    """Rebuilding a request with an existing notice cannot insert a later steer into its prefix."""
    from agent import runtime_notices
    from agent.turn_iteration_prep import prepare_iteration

    agent = _agent()
    runtime_notices.open_turn(agent)
    messages = [{"role": "user", "content": "do the task"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": _PROBE, "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "probe ok"}]
    assert agent.notify_runtime("[background finished]", ticket="notice") is True
    prepared = prepare_iteration(agent, messages=messages, api_call_count=2)
    messages = prepared.messages
    prefix = copy.deepcopy(messages)
    assert messages[-1]["display_kind"] == "runtime_notice"
    assert agent.steer("check the logs too", ticket="late-human") is True

    prepared = prepare_iteration(agent, messages=messages, api_call_count=2)
    assert prepared.messages == prefix
    assert agent.withdraw_steer("late-human") is True, "unread admission must remain refundable"
    assert agent.withdraw_steer("notice") is False, "already included notice keeps its consumed fate"



def test_a_human_steer_and_redirect_after_a_notice_preserve_the_sent_prefix_and_tickets():
    agent, requests = _agent(), []
    _DURING_TOOL.append(lambda: agent.notify_runtime("[background result]", ticket="notice"))
    ordinary = _script(1, requests)

    def redirect_after_notice(api_kwargs):
        if len(requests) == 1:
            requests.append(copy.deepcopy(api_kwargs["messages"]))
            assert agent.steer("keep this accepted steer", ticket="human") is True
            assert agent.redirect("use this correction too") is True
            raise InterruptedError("redirect rebuild")
        return ordinary(api_kwargs)

    result = _run(agent, redirect_after_notice)
    assert result["completed"] is True and result["user_intervened"] is True
    before, rebuilt = requests[1], requests[2]
    assert rebuilt[:len(before)] == before
    assert RUNTIME_NOTICE_OPEN in _text(before[-1])
    assert "keep this accepted steer" in _text(rebuilt[-1])
    assert "use this correction too" in _text(rebuilt[-1])
    assert not result.get("pending_steer")
    assert agent.withdraw_steer("human") is False
    assert agent.withdraw_steer("notice") is False
