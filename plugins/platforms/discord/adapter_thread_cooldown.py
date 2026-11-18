"""Per-parent-channel auto-thread rate-limit cooldown (HERMES-FORK-004).

Process-local: a server rate-limit signal is not config state. While a channel cools down, mentions
there neither retry thread creation nor each post their own failure notice (more traffic would only
extend the 429); one notice is allowed per cooldown window.
"""
from __future__ import annotations

import time


class AutoThreadCooldown:
    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._noticed: set[str] = set()

    def remaining(self, channel_id: str) -> float:
        """Seconds left in the channel's cooldown; 0.0 (and state cleared) once it has expired."""
        until = self._until.get(channel_id, 0.0)
        left = until - time.monotonic()
        if left > 0:
            return left
        self._until.pop(channel_id, None)
        self._noticed.discard(channel_id)
        return 0.0

    def record(self, channel_id: str, retry_after: float) -> None:
        self._until[channel_id] = time.monotonic() + retry_after
        self._noticed.discard(channel_id)

    def claim_notice(self, channel_id: str) -> bool:
        """True when a failure notice may be posted: always outside a cooldown, once inside one."""
        if not self.remaining(channel_id):
            return True
        if channel_id in self._noticed:
            return False
        self._noticed.add(channel_id)
        return True
