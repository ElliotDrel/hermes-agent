"""Bounded, single-bubble gateway activity rendering.

The queue transport remains owned by :mod:`gateway.run_turn_runner`; this module keeps the
rolling-only state and size policy separate from the already-large turn runner.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import threading
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import Any, Optional


class RollingActivityQueue(queue.Queue):
    """One turn's producer gate: finish seals and enqueues its marker atomically."""

    def __init__(self):
        super().__init__()
        self._producer_lock = threading.Lock()
        self._sealed = False

    def put(self, item, block=True, timeout=None):
        with self._producer_lock:
            if not self._sealed:
                super().put(item, block=block, timeout=timeout)

    def finish(self, marker):
        with self._producer_lock:
            if not self._sealed:
                self._sealed = True
                super().put(marker)


WORKING_HEADER = "⏳ Working…"
logger = logging.getLogger("gateway.run")


def terminal_header(
    result: Optional[dict[str, Any]], *, cancelled: bool = False
) -> str:
    """Choose the terminal state for a rolling activity bubble."""
    result = result or {}
    if cancelled or result.get("interrupted"):
        return "⏹️ Stopped"
    if (
        not result
        or result.get("failed")
        or result.get("partial")
        or result.get("completed") is False
        or bool(result.get("error"))
    ):
        return "⚠️ Needs attention"
    return "✅ Completed"


def configure_state(state: Any, grouping: str, initial_message_id: Optional[str]) -> None:
    """Attach rolling fields to the runner's shared editable-progress state."""
    state.progress_msg_id = initial_message_id
    state.rolling_header = WORKING_HEADER if grouping == "rolling" else None
    state.rolling_omitted_count = 0
    state.rolling_state_changed = False


def render_text(state: Any) -> str:
    """Render rolling tails without consuming the chronological event buffer."""
    if getattr(state, "rolling_header", None):
        return fit_tail(state)
    return "\n".join(str(line) for line in state.progress_lines)


def fit_tail(state: Any) -> str:
    """Pure transport-budget fitting; repeated renders cannot discard events."""
    lines = [str(line) for line in state.progress_lines]
    header = str(getattr(state, "rolling_header", "") or "")
    telemetry = getattr(state, "rolling_telemetry", None)
    if telemetry:
        header += "\n" + telemetry
    measure, limit = state._progress_len_fn, state._PROGRESS_TEXT_LIMIT
    for omitted in range(max(1, len(lines))):
        parts = [header] if header else []
        if omitted:
            parts.append(f"… {omitted} earlier activities omitted")
        parts.extend(lines[omitted:])
        rendered = "\n".join(parts)
        if measure(rendered) <= limit:
            return rendered
    if lines:
        prefix = header + "\n"
        if len(lines) > 1:
            prefix += f"… {len(lines) - 1} earlier activities omitted\n"
        if measure(prefix) < limit:
            low, high = 0, len(lines[-1])
            while low < high:
                mid = (low + high + 1) // 2
                if measure(prefix + lines[-1][:mid]) <= limit:
                    low = mid
                else:
                    high = mid - 1
            return prefix + lines[-1][:low]
    # Tiny transport budgets can be smaller than the header itself. Respect the
    # adapter's length function (Discord uses UTF-16 units), including this case.
    low, high = 0, len(header)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(header[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    return header[:low]


def absorb_marker(state: Any, raw: Any) -> tuple[bool, Any, bool]:
    """Apply a rolling control marker and return ``(handled, display_value, finish)``."""
    if not isinstance(raw, tuple) or not raw:
        return False, None, False
    marker = raw[0]
    if marker == "__activity_start__":
        return True, state.rolling_header or WORKING_HEADER, False
    if marker in {"__activity_state__", "__activity_finish__"} and len(raw) >= 2:
        state.rolling_header = str(raw[1])
        state.rolling_state_changed = True
        return True, state.rolling_header, marker == "__activity_finish__"
    return False, None, False


def is_finish_marker(raw: Any) -> bool:
    return bool(isinstance(raw, tuple) and len(raw) >= 2 and raw[0] == "__activity_finish__")


async def retry_terminal_edit(send: Any, state: Any, message: Any, log: Any) -> bool:
    """Retry a consumed terminal marker without losing its finish intent."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        if await send(state, message):
            return True
        if attempt < attempts:
            await asyncio.sleep(0.3)
    log.warning("[%s] Rolling activity terminal edit failed after %d attempts", state.adapter.name, attempts)
    return False


def has_renderable_state(state: Any) -> bool:
    return bool(
        state.progress_lines
        or getattr(state, "rolling_state_changed", False)
        or (getattr(state, "rolling_header", None) and getattr(state, "rolling_omitted_count", 0))
    )


async def start_hygiene_activity(runner: Any, event: Any, source: Any, user_config: Any) -> Optional[str]:
    """Seed rolling activity before a potentially long pre-turn compression."""
    from agent.secret_scope import get_secret
    from gateway.display_config import resolve_display_setting, resolve_tool_progress
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import _interim_metadata, _platform_config_key

    platform_key = _platform_config_key(source.platform)
    progress_mode, _ = resolve_tool_progress(
        user_config if isinstance(user_config, dict) else {},
        platform_key,
        get_secret("HERMES_TOOL_PROGRESS_MODE"),
    )
    grouping = resolve_display_setting(user_config or {}, platform_key, "tool_progress_grouping")
    adapter = runner._delivery_adapter_for(source)
    adapter_edit = getattr(type(adapter), "edit_message", None) if adapter is not None else None
    if (
        grouping != "rolling"
        or progress_mode in {"off", "log"}
        or runner._get_proxy_url()
        or adapter_edit in (None, BasePlatformAdapter.edit_message)
    ):
        return None
    reply_to = runner._reply_anchor_for_event(event)
    metadata, progress_reply_to, _ = runner._run_agent_progress_threading(source, reply_to, False)
    try:
        result = await adapter.send(
            source.chat_id,
            "⏳ Compressing conversation context…",
            reply_to=progress_reply_to,
            metadata=_interim_metadata(metadata),
        )
    except Exception:
        logger.debug("Session hygiene rolling activity send failed", exc_info=True)
        return None
    return str(result.message_id) if result.success and result.message_id else None


async def continue_hygiene_activity(
    runner: Any, source: Any, message_id: Optional[str], event_message_id: Optional[str]
) -> None:
    """Hand a pre-turn activity bubble to the normal rolling sender."""
    await _edit_hygiene_activity(runner, source, message_id, event_message_id, WORKING_HEADER)


async def _edit_hygiene_activity(
    runner: Any, source: Any, message_id: Optional[str], event_message_id: Optional[str], content: str
) -> None:
    if not message_id:
        return
    adapter = runner._delivery_adapter_for(source)
    metadata, _, _ = runner._run_agent_progress_threading(source, event_message_id, False)
    kwargs = {"chat_id": source.chat_id, "message_id": message_id, "content": content}
    try:
        params = inspect.signature(adapter.edit_message).parameters
        if "metadata" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["metadata"] = metadata
    except (TypeError, ValueError):
        pass
    try:
        await adapter.edit_message(**kwargs)
    except Exception:
        logger.debug("Session hygiene rolling activity edit failed", exc_info=True)


async def abort_hygiene_activity(
    runner: Any, source: Any, message_id: Optional[str], event_message_id: Optional[str]
) -> None:
    """Close a pre-turn bubble that cannot be handed to a real agent turn."""
    if not message_id:
        return
    from gateway.platforms.base import BasePlatformAdapter

    adapter = runner._delivery_adapter_for(source)
    delete = getattr(type(adapter), "delete_message", None) if adapter is not None else None
    if delete not in (None, BasePlatformAdapter.delete_message):
        try:
            if await adapter.delete_message(source.chat_id, message_id):
                return
        except Exception:
            logger.debug("Session hygiene rolling activity delete failed", exc_info=True)
    await _edit_hygiene_activity(runner, source, message_id, event_message_id, "⏹️ Stopped")


@asynccontextmanager
async def own_hygiene_activity(runner: Any, event: Any, source: Any):
    """Own a preflight message until preparation hands its ID to the agent turn."""
    activity_message = [None]
    try:
        yield activity_message
    finally:
        if activity_message:
            await abort_hygiene_activity(
                runner, source, activity_message[0], runner._reply_anchor_for_event(event)
            )


async def finish_turn_activity(turn_ctx: Any, progress_task: Any, logger: Any) -> None:
    """Flush the terminal header before final delivery or a queued rolling turn."""
    if not (
        progress_task
        and turn_ctx.progress_grouping == "rolling"
        and turn_ctx.tool_progress_enabled
        and turn_ctx.progress_queue is not None
    ):
        return
    current = asyncio.current_task()
    header = terminal_header(turn_ctx.activity_result, cancelled=bool(current and current.cancelling()))
    if header == "✅ Completed":
        header = "⏳ Finishing…"
    turn_ctx.progress_finish_header[0] = header
    finish = getattr(turn_ctx.progress_queue, "finish", turn_ctx.progress_queue.put)
    finish(("__activity_finish__", header))
    try:
        await asyncio.wait_for(asyncio.shield(progress_task), timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        progress_task.cancel()
        with suppress(asyncio.CancelledError):
            await progress_task
    except Exception:
        logger.warning("Rolling activity terminal flush failed", exc_info=True)


def schedule_progress_completion(response: Any, _cleanup_adapter: Any, turn_ctx: Any) -> None:
    """Capture exact progress IDs after producers stop; release only on confirmed delivery."""
    st = turn_ctx.progress_state
    rolling = turn_ctx.progress_grouping == "rolling" and st is not None
    adapter = st.adapter if rolling else _cleanup_adapter
    if (adapter is None or not isinstance(response, dict) or response.get("failed")
            or terminal_header(response) != "✅ Completed"):
        return
    if not rolling and not (turn_ctx._cleanup_progress and turn_ctx._cleanup_msg_ids):
        return
    register = getattr(type(adapter), "register_confirmed_delivery_callback", None)
    if register is None:
        return
    ids = list(dict.fromkeys(turn_ctx._cleanup_msg_ids)) if turn_ctx._cleanup_progress else []
    chat_id = turn_ctx.source.chat_id
    message_id = st.progress_msg_id if rolling else None
    metadata = dict(turn_ctx._progress_metadata or {})
    content = None
    if rolling:
        # The sender is quiescent: a copy keeps confirmation rendering pure.
        import copy
        snapshot = copy.copy(st)
        snapshot.progress_lines = list(st.progress_lines)
        snapshot.rolling_header = "✅ Completed"
        content = render_text(snapshot)
    edit_pending = bool(message_id)

    async def complete() -> bool:
        nonlocal edit_pending
        if edit_pending:
            kwargs = {"chat_id": chat_id, "message_id": message_id, "content": content}
            if st._edit_accepts_metadata:
                kwargs["metadata"] = metadata
            if getattr(adapter, "REQUIRES_EDIT_FINALIZE", False):
                kwargs["finalize"] = True
            result = await adapter.edit_message(**kwargs)
            if not getattr(result, "success", False):
                return False
            edit_pending = False
        for message in tuple(ids):
            if await adapter.delete_message(chat_id, message):
                ids.remove(message)
        return not ids

    adapter.register_confirmed_delivery_callback(turn_ctx.progress_delivery_owner, complete)

