"""Nonhuman runtime notices handed to a RUNNING turn (the gateway's active-parent notifications).

A background result for a session whose turn is still running is delivered at the turn's next safe
boundary instead of waiting as the next queued turn. It is Hermes' own text, never the user's, so it is
not a ``steer()``: it does not mark the turn user-intervened and carries no approval, and its row is
typed ``runtime_notice``. It lands only where the transcript ends in tool results, right before the
next request (append-only, so the cached prefix holds and ``tool -> user`` stays the one legal
mid-turn shape). Each notice is ticketed by its admission id and shares the steer fate ledger, so the
gateway settles both through ``withdraw_steer``: a drained notice settles with the turn, an unread one
goes back to its own FIFO place. The finalizer seals the buffer: nothing reaches a turn that has no
request left to make.
"""
from agent.interrupt_control import _ic_lock, _steer_fates
from agent.prompt_builder import runtime_notice_row


def _parts(agent) -> list:
    return vars(agent).setdefault("_runtime_notice_parts", [])


def offer(agent, text: str, ticket) -> bool:
    """Buffer ``text`` for this turn; False when the turn can no longer take it (sealed, or a Codex
    app-server turn, whose model loop has no typed nonhuman boundary)."""
    if not isinstance(text, str) or not text.strip() or ticket is None:
        return False
    if getattr(agent, "api_mode", None) == "codex_app_server":
        return False
    with _ic_lock(agent, "_pending_steer_lock"):
        if vars(agent).get("_runtime_notices_sealed", True):
            return False
        _parts(agent).append((ticket, text.strip()))
    return True


def drain(agent, messages: list) -> bool:
    """Append every buffered notice as one typed row when ``messages`` ends in a tool result."""
    last = messages[-1] if messages else None
    if not isinstance(last, dict) or last.get("role") != "tool":
        return False
    with _ic_lock(agent, "_pending_steer_lock"):
        parts = _parts(agent)
        if not parts:
            return False
        agent._runtime_notice_parts = []
        for ticket, _ in parts:
            _steer_fates(agent)[ticket] = True
    messages.append(runtime_notice_row("\n\n".join(text for _, text in parts)))
    return True


def open_turn(agent) -> None:
    """A new turn takes notices again (a notice offered before it started stays buffered for it)."""
    with _ic_lock(agent, "_pending_steer_lock"):
        agent._runtime_notices_sealed = False


def seal(agent, *, discard: bool = False) -> None:
    """No later boundary will drain: refuse new notices. ``discard`` (a hard stop) also drops the
    unread ones, which then read unconsumed to ``withdraw_steer``."""
    with _ic_lock(agent, "_pending_steer_lock"):
        agent._runtime_notices_sealed = True
        if discard:
            for ticket, _ in _parts(agent):
                _steer_fates(agent)[ticket] = False
            agent._runtime_notice_parts = []


def withdraw(agent, ticket) -> bool:
    """Take an unread notice out by its ticket; True when it was still buffered."""
    with _ic_lock(agent, "_pending_steer_lock"):
        parts = _parts(agent)
        kept = [part for part in parts if part[0] != ticket]
        if len(kept) == len(parts):
            return False
        agent._runtime_notice_parts = kept
    return True
