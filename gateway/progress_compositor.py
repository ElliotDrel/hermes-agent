"""Single-message progress composition for editable messaging platforms.

The compositor owns exactly one temporary message for a turn. It never falls
back to a replacement send after the initial post because preserving one
message is more important than preserving every transient update.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from contextlib import suppress
from typing import Any, Callable, Deque, Optional

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


class ProgressCompositor:
    """Compose status and activity into one bounded, editable message."""

    EDIT_INTERVAL_SECONDS = 1.5
    DEFAULT_TEXT_LIMIT = 1936
    # Best-effort display must never stall worker admission or hold-expiry continuation.
    TRANSPORT_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        session_key: Optional[str] = None,
        generation: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.metadata = metadata
        self.session_key = session_key
        self.generation = generation
        self.message_id: Optional[str] = None
        self.closed: bool = False
        self._start_attempted = False
        self.status_line: Optional[str] = None
        self.activity_items: Deque[str] = deque()
        self.activity_kinds: Deque[str] = deque()
        self.editing_disabled = False
        self.next_edit_after = 0.0
        self._clock = clock
        self.started_at = clock()
        self._dirty = False
        self._last_rendered = "⏳ Working…"
        self._backoff_seconds = 1.0
        self._len_fn, self._text_limit = self._resolve_limit()

    def _resolve_limit(self) -> tuple[Callable[[str], int], int]:
        len_fn: Callable[[str], int] = len
        raw_limit = int(getattr(self.adapter, "MAX_MESSAGE_LENGTH", 2000) or 2000)
        with suppress(Exception):
            len_fn = self.adapter.message_len_fn_for_chat(self.chat_id)
        with suppress(Exception):
            raw_limit = int(self.adapter.max_message_length_for_chat(self.chat_id) or raw_limit)
        # Formatting can inflate Markdown. Reserve a small transport margin on normal-size limits.
        margin = 64 if raw_limit > 128 else 0
        return len_fn, max(1, raw_limit - margin)

    async def start(self) -> Optional[SendResult]:
        """Post the sole temporary message. Failure disables progress for this turn."""
        if self.closed:
            return None
        # Concurrent handoff or an ambiguous timeout must never duplicate the initial send.
        if self._start_attempted:
            return SendResult(success=bool(self.message_id), message_id=self.message_id)
        self._start_attempted = True
        try:
            # Hygiene supplies the initial header without posting a second temporary message.
            initial_text = self._fit_render()
            # Cancel inline, with no shield/detached send: an ambiguous timeout never retries.
            async with asyncio.timeout(self.TRANSPORT_TIMEOUT_SECONDS):
                result = await self.adapter.send(
                    self.chat_id,
                    initial_text,
                    reply_to=self.reply_to,
                    metadata=self.metadata,
                )
        except asyncio.CancelledError:
            self.editing_disabled = True
            raise
        except Exception as exc:
            logger.debug("Progress compositor initial send failed: %s", type(exc).__name__)
            self.editing_disabled = True
            return SendResult(success=False, error=str(exc))
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            self.message_id = str(result.message_id)
            self._last_rendered = initial_text
            self._dirty = False
        else:
            self.editing_disabled = True
        return result

    def publish_activity(self, text: Any, kind: str = "message") -> None:
        value = str(text or "").strip()
        if value:
            self.activity_items.append(value)
            self.activity_kinds.append("tool" if kind == "tool" else "message")
            self._dirty = True

    def publish_status(self, text: Any) -> None:
        value = str(text or "").strip()
        if value:
            self.status_line = value
            self._dirty = True

    def absorb(self, raw: Any) -> None:
        """Fold the gateway progress-bus item into compositor state."""
        if isinstance(raw, tuple) and raw:
            kind = raw[0]
            if kind == "__reset__":
                # A final-stream boundary must not create a second progress bubble.
                return
            if kind == "__status__" and len(raw) > 1:
                self.publish_status(raw[1])
                return
            if kind == "__interim__" and len(raw) > 1:
                self.publish_activity(raw[1])
                return
            if kind == "__steer__" and len(raw) > 1:
                # A steer is an ordered activity boundary, not a replaceable status line.
                self.publish_activity(raw[1])
                return
            if kind == "__tool__" and len(raw) > 1:
                self.publish_activity(raw[1], kind="tool")
                return
            if kind == "__dedup__" and len(raw) == 3:
                _, base, count = raw
                # Legacy producers can still emit summaries; never rewrite prior events.
                self.publish_activity(f"{base} (×{count + 1})")
                return
        self.publish_activity(raw)

    def _formatted_len(self, text: str) -> int:
        formatted = self.adapter.format_message(text)
        return self._len_fn(formatted)

    @staticmethod
    def _compose(header: str, activities: list[str]) -> str:
        return header + ("\n\n" + "\n".join(activities) if activities else "")

    def _fit_prefix(self, text: str, *, suffix: str = "", limit: Optional[int] = None) -> str:
        """Bound a display-only prefix using the adapter's formatted length."""
        budget = self._text_limit if limit is None else limit
        lo, hi, best = 0, len(text), ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = text[:mid] + suffix
            if self._formatted_len(candidate) <= budget:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _header(self, hidden_tools: int, hidden_messages: int, *, reserve: int = 0) -> str:
        status = self.status_line or "⏳ Working…"
        summary = (
            f" · hidden: {hidden_tools} tools / {hidden_messages} messages"
            if hidden_tools or hidden_messages else ""
        )
        # Preserve the omission counts before a pathological status. Tiny budgets
        # cannot show the whole summary; even that fallback must respect formatting.
        summary_length = self._formatted_len(summary)
        budget = max(self._text_limit - reserve, min(summary_length, self._text_limit))
        if summary_length > budget:
            return self._fit_prefix(summary.lstrip(" ·"), limit=budget)
        return self._fit_prefix(status, suffix=summary, limit=budget).lstrip(" ·")

    def _fit_render(self) -> str:
        """Render the newest suffix without changing the canonical log or status."""
        activities = list(self.activity_items)
        rendered = self._compose(self.status_line or "⏳ Working…", activities)
        if self._formatted_len(rendered) <= self._text_limit:
            return rendered
        if not activities:
            return self._header(0, 0)

        hidden_tools = sum(kind == "tool" for kind in self.activity_kinds)
        hidden_messages = len(activities) - hidden_tools
        # Leave room for the newest activity when a status itself is oversized.
        reserve = min(self._text_limit // 2, self._formatted_len("\n\n" + activities[-1]))
        visible: list[str] = []
        best = ""
        for index in range(len(activities) - 1, -1, -1):
            if self.activity_kinds[index] == "tool":
                hidden_tools -= 1
            else:
                hidden_messages -= 1
            visible.insert(0, activities[index])
            header = self._header(hidden_tools, hidden_messages, reserve=reserve)
            candidate = self._compose(header, visible)
            if self._formatted_len(candidate) <= self._text_limit:
                best = candidate
                continue
            if best:
                return best

            # Only the newest entry may be partially displayed. It is not hidden
            # while any of its text is visible; earlier entries remain whole omissions.
            original = activities[-1]
            lo, hi = 1, len(original)
            while lo <= hi:
                mid = (lo + hi) // 2
                # Keep the tool/target at the beginning as well as useful trailing flags/details.
                head_len, tail_len = (mid + 1) // 2, mid // 2
                clipped = (original[:head_len] + " … " + (original[-tail_len:] if tail_len else "")) if mid < len(original) else original
                candidate = self._compose(header, [clipped])
                if header and self._formatted_len(candidate) <= self._text_limit:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best:
                return best
            # Not even one character fits: all entries are entirely hidden.
            return self._header(
                sum(kind == "tool" for kind in self.activity_kinds),
                len(activities) - sum(kind == "tool" for kind in self.activity_kinds),
            )
        return best

    async def flush(self, *, force: bool = False) -> Optional[bool]:
        """Edit the owned message once; retryable failures retain the latest desired state."""
        if self.closed:
            return None
        if self.editing_disabled or not self.message_id or not self._dirty:
            return False
        now = self._clock()
        if not force and now < self.next_edit_after:
            return False
        rendered = self._fit_render()
        if rendered == self._last_rendered:
            self._dirty = False
            return True
        kwargs = {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "content": rendered,
        }
        try:
            params = inspect.signature(self.adapter.edit_message).parameters.values()
            accepts_metadata = any(
                param.kind is inspect.Parameter.VAR_KEYWORD or param.name == "metadata"
                for param in params
            )
        except (TypeError, ValueError):
            accepts_metadata = False
        if self.metadata and accepts_metadata:
            kwargs["metadata"] = self.metadata
        try:
            # Timeout freezes the owned ID; continuation never waits on an orphan edit.
            async with asyncio.timeout(self.TRANSPORT_TIMEOUT_SECONDS):
                result = await self.adapter.edit_message(**kwargs)
        except Exception as exc:
            logger.debug(
                "Progress compositor edit exception platform=%s chat=%s message=%s session=%s generation=%s category=%s",
                getattr(self.adapter, "name", "unknown"), self.chat_id, self.message_id,
                self.session_key, self.generation, type(exc).__name__,
            )
            self.editing_disabled = True
            return False
        if getattr(result, "success", False):
            self._last_rendered = rendered
            self._dirty = False
            self._backoff_seconds = 1.0
            self.next_edit_after = now + self.EDIT_INTERVAL_SECONDS
            return True
        if getattr(result, "retryable", False) or getattr(result, "retry_after", None) is not None:
            retry_after = getattr(result, "retry_after", None)
            delay = max(0.0, float(retry_after)) if retry_after is not None else self._backoff_seconds
            self.next_edit_after = now + min(delay, 30.0)
            self._backoff_seconds = min(self._backoff_seconds * 2.0, 30.0)
            return False
        self.editing_disabled = True
        logger.debug(
            "Progress compositor disabled platform=%s chat=%s message=%s session=%s generation=%s category=%s",
            getattr(self.adapter, "name", "unknown"), self.chat_id, self.message_id,
            self.session_key, self.generation, getattr(result, "error_kind", None) or "permanent",
        )
        return False
