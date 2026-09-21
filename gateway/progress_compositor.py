"""Single-message progress composition for editable messaging platforms.

The compositor owns exactly one temporary message for a turn. It never falls
back to a replacement send after the initial post because preserving one
message is more important than preserving every transient update.
"""

from __future__ import annotations

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
        self.status_line: Optional[str] = None
        self.activity_items: Deque[str] = deque()
        self.omitted_count = 0
        self.editing_disabled = False
        self.next_edit_after = 0.0
        self._clock = clock
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

    async def start(self) -> SendResult:
        """Post the sole temporary message. Failure disables progress for this turn."""
        try:
            result = await self.adapter.send(
                self.chat_id,
                "⏳ Working…",
                reply_to=self.reply_to,
                metadata=self.metadata,
            )
        except Exception as exc:
            logger.debug("Progress compositor initial send failed: %s", type(exc).__name__)
            self.editing_disabled = True
            return SendResult(success=False, error=str(exc))
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            self.message_id = str(result.message_id)
        else:
            self.editing_disabled = True
        return result

    def publish_activity(self, text: Any) -> None:
        value = str(text or "").strip()
        if value and not self.editing_disabled:
            self.activity_items.append(value)
            self._dirty = True

    def publish_status(self, text: Any) -> None:
        value = str(text or "").strip()
        if value and not self.editing_disabled:
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
            if kind == "__dedup__" and len(raw) == 3:
                _, base, count = raw
                replacement = f"{base} (×{count + 1})"
                if self.activity_items:
                    self.activity_items[-1] = replacement
                    self._dirty = True
                else:
                    self.publish_activity(replacement)
                return
        self.publish_activity(raw)

    def _formatted_len(self, text: str) -> int:
        formatted = self.adapter.format_message(text)
        return self._len_fn(formatted)

    def _compose(self) -> str:
        header = self.status_line or "⏳ Working…"
        lines = [header]
        if self.activity_items:
            lines.append("")
            lines.extend(self.activity_items)
        if self.omitted_count:
            lines.append(f"… {self.omitted_count} earlier updates omitted")
        return "\n".join(lines)

    def _fit_render(self) -> str:
        rendered = self._compose()
        while len(self.activity_items) > 1 and self._formatted_len(rendered) > self._text_limit:
            self.activity_items.popleft()
            self.omitted_count += 1
            rendered = self._compose()
        if self._formatted_len(rendered) <= self._text_limit:
            return rendered

        # One pathological activity line can still overflow. Keep its newest tail and the omission marker.
        if self.activity_items:
            original = self.activity_items[-1]
            lo, hi, best = 0, len(original), ""
            while lo <= hi:
                mid = (lo + hi) // 2
                self.activity_items[-1] = ("…" + original[-mid:]) if mid < len(original) else original
                candidate = self._compose()
                if self._formatted_len(candidate) <= self._text_limit:
                    best = self.activity_items[-1]
                    lo = mid + 1
                else:
                    hi = mid - 1
            self.activity_items[-1] = best
            rendered = self._compose()
        if self._formatted_len(rendered) <= self._text_limit:
            return rendered

        # A pathological status line is bounded last. This keeps the one-message invariant.
        header = self.status_line or "⏳ Working…"
        while header and self._formatted_len(header) > self._text_limit:
            header = header[:-1]
        return header or "⏳"

    async def flush(self, *, force: bool = False) -> bool:
        """Edit the owned message once; retryable failures retain the latest desired state."""
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
