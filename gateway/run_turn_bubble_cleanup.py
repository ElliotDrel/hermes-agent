"""Deletion of a turn's temporary progress bubbles (``display.platforms.<p>.cleanup_progress``)."""

import asyncio
import logging
from typing import Any

from gateway.platforms.base_post_delivery import delivery_confirmed
from gateway.turn_context import TurnContext

logger = logging.getLogger("gateway.run")

# Per-delete bound: the callback runs under the post-delivery timeout (30 s), so one slow delete must
# not use it up and leave the remaining bubbles undeleted. Also caps the handoff's sender wait.
DELETE_TIMEOUT_SECONDS = 10.0
# Single-message progress (HERMES-FORK-017): two attempts per ID inside one shared budget that leaves
# the adapter's outer callback timeout a second to unwind. Exhaustion retains the message; there is
# no sweeper or retry service.
_COMPOSED_DELETE_BUDGET_SECONDS = 25.0
_COMPOSED_ATTEMPT_TIMEOUT_SECONDS = 5.0
# Detached deletes never hold the turn, the running-session guard or the result's delivery: they get
# the initial-send settle wait, the delete budget and a second to unwind, then are cancelled.
_DETACHED_DELETE_BOUND_SECONDS = 6.0 + _COMPOSED_DELETE_BUDGET_SECONDS + 1.0
_detached_deletes: set = set()


async def settle_turn_senders(*tasks: Any) -> None:
    """Stop the turn's bubble senders (progress, heartbeat) and wait for them, as the turn's
    ``finally`` does, so every bubble id they mint is tracked before a queued follow-up takes over.

    Bounded by ``DELETE_TIMEOUT_SECONDS``: a sender whose cancel-time flush edit hangs is cancelled
    again and the follow-up goes ahead (ids it mints later are lost). ``asyncio.wait`` never raises
    the senders' own cancellation, so a cancel aimed at the turn (shutdown, /stop) propagates and no
    follow-up starts."""
    live = [task for task in tasks if task is not None]
    if not live:
        return
    for task in live:
        task.cancel()
    done, pending = await asyncio.wait(live, timeout=DELETE_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            logger.debug("Bubble sender failed at handoff", exc_info=task.exception())


def _composed(turn_ctx: Any) -> Any:
    """The turn's single-message compositor, or None when it renders legacy progress bubbles."""
    if getattr(turn_ctx, "progress_compositor_mode", "off") != "single_message":
        return None
    return getattr(turn_ctx, "progress_compositor", None)


async def _delete_composed(turn_ctx: Any, compositor: Any) -> None:
    """Delete the owned compositor message. An ID leaves the turn's tracked list only on
    transport-confirmed deletion; exhaustion retains it."""
    adapter, chat_id, session_key = turn_ctx._cleanup_adapter, turn_ctx.source.chat_id, turn_ctx.session_key
    tracked = turn_ctx._cleanup_msg_ids
    # Freeze the message (and learn its ID if the initial send was still in flight) before deleting.
    await compositor.settle()
    if compositor.message_id and compositor.message_id not in tracked:
        tracked.append(compositor.message_id)
    ids = list(dict.fromkeys(str(mid) for mid in tracked if mid))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _COMPOSED_DELETE_BUDGET_SECONDS
    for index, mid in enumerate(ids):
        for attempt in range(2):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                if await asyncio.wait_for(
                    adapter.delete_message(chat_id, mid), min(_COMPOSED_ATTEMPT_TIMEOUT_SECONDS, remaining),
                ):
                    tracked[:] = [known for known in tracked if str(known) != mid]
                    break
            except Exception as exc:  # health: allow BLE001 -- a failed delete retains the message; category logged
                logger.warning("Temp bubble delete failed session=%s chat=%s message=%s attempt=%s category=%s",
                               session_key, chat_id, mid, attempt + 1, type(exc).__name__)
            if attempt == 0:
                await asyncio.sleep(min(0.25, max(0.0, deadline - loop.time())))
        else:
            logger.warning("Temp bubble delete unconfirmed chat=%s message=%s after 2 attempts", chat_id, mid)
        if loop.time() >= deadline:
            logger.warning("Temp bubble cleanup budget exhausted chat=%s unattempted=%s", chat_id, ids[index + 1:])
            return


async def _delete_legacy(adapter: Any, chat_id: str, ids: list, session_key: Any) -> None:
    async def _delete(mid: str) -> bool:
        try:
            return await asyncio.wait_for(adapter.delete_message(chat_id, mid), DELETE_TIMEOUT_SECONDS) is not False
        except Exception:  # health: allow BLE001 -- one bad or slow delete must not stop the rest; logged below
            logger.debug("Temp bubble delete failed for %s", mid, exc_info=True)
            return False

    failed = sum(not ok for ok in await asyncio.gather(*(_delete(mid) for mid in ids)))
    if failed:
        logger.info("Temp bubble cleanup for session %s: %d of %d deletes failed", session_key, failed, len(ids))


def _spawn_detached_delete(coro: Any, session_key: Any) -> None:
    async def _bounded() -> None:
        try:
            await asyncio.wait_for(coro, _DETACHED_DELETE_BOUND_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # health: allow BLE001 -- detached presentation cleanup; retained message is the failure mode
            logger.warning("Detached temp bubble cleanup did not finish session=%s", session_key, exc_info=True)

    task = asyncio.ensure_future(_bounded())
    _detached_deletes.add(task)
    task.add_done_callback(_detached_deletes.discard)


async def settle_detached_deletes() -> None:
    """Wait for in-flight detached deletes (tests and orderly shutdown; never the turn path)."""
    pending = list(_detached_deletes)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def detach_turn_bubble_delete(turn_ctx: TurnContext) -> None:
    """Freeze the turn's progress now and delete it in a detached, bounded background task.

    The compositor is closed synchronously (no further edits), and the exact IDs the turn owns are
    fixed here; the transport work then runs off the turn, so the delete can never delay the session
    guard's release, the next turn's start or the result's delivery."""
    if not (turn_ctx._cleanup_progress and turn_ctx._cleanup_adapter is not None):
        return
    compositor = _composed(turn_ctx)
    if compositor is not None:
        compositor.closed = True
        _spawn_detached_delete(_delete_composed(turn_ctx, compositor), turn_ctx.session_key)
    elif turn_ctx._cleanup_msg_ids:
        _spawn_detached_delete(
            _delete_legacy(turn_ctx._cleanup_adapter, turn_ctx.source.chat_id,
                           list(turn_ctx._cleanup_msg_ids), turn_ctx.session_key),
            turn_ctx.session_key,
        )


def register_bubble_cleanup(response: Any, turn_ctx: TurnContext) -> None:
    """Register deletion of the turn's tracked progress bubbles as its post-delivery callback.

    The callback is async and awaits every delete, so whoever fires it (the adapter after the final
    send, or the queued-follow-up handoff before the next turn starts) waits for the deletion.
    Failed runs keep them as breadcrumbs. Only on adapters with ``delete_message``.

    Single-message progress is stricter about completion and about delivery: only a completed,
    uninterrupted turn registers (a delivered terminal error reply counts), and the callback deletes
    only unless the firing site reported that no final payload reached the chat."""
    adapter, ids, session_key = turn_ctx._cleanup_adapter, turn_ctx._cleanup_msg_ids, turn_ctx.session_key
    compositor = _composed(turn_ctx)
    if not (
        turn_ctx._cleanup_progress and adapter is not None and (ids or compositor is not None) and session_key
        and isinstance(response, dict) and hasattr(adapter, "register_post_delivery_callback")
    ):
        return
    if compositor is not None:
        if response.get("interrupted") or response.get("cancelled") or response.get("completed", True) is False:
            return

        async def _cleanup_composed() -> None:
            if delivery_confirmed.get() is False:
                logger.debug("Retaining temp progress for session %s: final delivery unconfirmed", session_key)
                return
            await _delete_composed(turn_ctx, compositor)

        adapter.register_post_delivery_callback(session_key, _cleanup_composed, generation=turn_ctx.run_generation)
        return
    if response.get("failed"):
        return
    ids_snapshot, chat_id = list(ids), turn_ctx.source.chat_id

    async def _cleanup_temp_bubbles() -> None:
        await _delete_legacy(adapter, chat_id, ids_snapshot, session_key)

    adapter.register_post_delivery_callback(session_key, _cleanup_temp_bubbles, generation=turn_ctx.run_generation)
