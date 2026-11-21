"""Detached receipt display: hourglass while a busy batch is open, stable number until work starts."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from gateway.platforms.event import MessageEvent, ProcessingOutcome

logger = logging.getLogger(__name__)

_DISCORD_EPOCH_MS = 1420070400000

_POSITIONS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
_BEYOND = "⏳"


class DiscordReactionsMixin:
    name: str
    _client: Any
    _extra_or_env_flag: Any
    _resolve_channel: Any
    _record_discord_processing_start: Any
    _record_discord_processing_complete: Any
    _source_session_key: Any
    _active_sessions: Any
    _background_tasks: Any

    _RECEIPT_OP_TIMEOUT = 10.0
    _RECEIPT_STATE_CAP = 1024

    async def _add_reaction(self, message: Any, emoji: str) -> bool:
        """Add an emoji reaction to a Discord message."""
        if not message or not hasattr(message, "add_reaction"):
            return False
        try:
            await message.add_reaction(emoji)
            return True
        except Exception as e:
            logger.debug("[%s] add_reaction failed (%s): %s", self.name, emoji, e)
            return False

    async def _remove_reaction(self, message: Any, emoji: str) -> bool:
        """Remove the bot's own emoji reaction from a Discord message."""
        if not message or not hasattr(message, "remove_reaction") or not self._client or not self._client.user:
            return False
        try:
            await message.remove_reaction(emoji, self._client.user)
            return True
        except Exception as e:
            logger.debug("[%s] remove_reaction failed (%s): %s", self.name, emoji, e)
            return False

    def _reactions_enabled(self) -> bool:
        """Reactions enabled via ``extra.reactions`` (YAML, per profile) or ``DISCORD_REACTIONS``."""
        return self._extra_or_env_flag("reactions", "DISCORD_REACTIONS", "true", truthy=False)

    # -- reaction targets and detached, per-message serialized calls ------------------------------

    def _receipt_marks(self) -> dict:
        """message id -> [number, live message, channel id, session key, window open], oldest first."""
        return self.__dict__.setdefault("_receipt_marks_by_id", {})

    @staticmethod
    def _live_message(event: MessageEvent, mid: str) -> Any:
        """The event's own Discord message when it IS message *mid*: a merged event keeps the head's
        ``raw_message`` while its ``message_id`` moves to the latest, so ids must be compared."""
        raw = event.raw_message
        if raw is None:
            return None
        raw_id = str(getattr(raw, "id", "") or "")
        return raw if (raw_id or str(event.message_id or "")) == mid else None

    @staticmethod
    def _home_channel_id(event: MessageEvent, mid: str) -> str:
        """Channel *mid* was sent in. An auto-thread's starter (the first message of the event) lives in
        the parent channel, not in the thread; every later message of a merged burst was sent inside it."""
        source = event.source
        ids = event.message_ids()
        return source.parent_chat_id if source.auto_thread_created and ids and mid == ids[0] else source.chat_id

    async def _partial_message(self, mid: str, channel_id: str) -> Any:
        """A partial message built from its id (a queued message restored from the session queue), best effort."""
        if not self._client or not mid:
            return None
        try:
            channel = await self._resolve_channel(channel_id)
            return channel.get_partial_message(int(mid))
        except Exception:
            logger.debug("[%s] No reaction target for message %s", self.name, mid, exc_info=True)
            return None

    def _schedule_reaction(
        self, mid: str, message: Any, channel_id: str, *ops: tuple[str, str], receipt: bool = False,
    ) -> None:
        """Run ``("add" | "remove", emoji)`` calls for one message as a detached task that follows
        that message's earlier calls and gives up after ``_RECEIPT_OP_TIMEOUT``. The target is captured
        here, because the caller may drop the state it came from before the task runs. A *receipt*
        logs when its acknowledgement lands."""
        tails = self.__dict__.setdefault("_receipt_tails", {})
        prior = tails.get(mid)

        async def run() -> None:
            if prior is not None:
                await asyncio.wait({prior})
            try:
                await asyncio.wait_for(
                    self._apply_reactions(mid, message, channel_id, ops, receipt), self._RECEIPT_OP_TIMEOUT)
            except Exception as exc:
                logger.debug("[%s] reaction update for %s abandoned: %r", self.name, mid, exc)

        task = asyncio.get_running_loop().create_task(run())
        tails[mid] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(lambda done: tails.get(mid) is done and tails.pop(mid, None))

    async def _apply_reactions(
        self, mid: str, message: Any, channel_id: str, ops: tuple[tuple[str, str], ...], receipt: bool = False,
    ) -> None:
        if message is None:
            message = await self._partial_message(mid, channel_id)
        if not hasattr(message, "add_reaction"):
            return
        for kind, emoji in ops:
            landed = await (self._add_reaction if kind == "add" else self._remove_reaction)(message, emoji)
            if receipt and kind == "add" and landed:
                # Discord send time is in the snowflake; the gap is delivery + admission + rate limit + REST.
                sent_ms = (int(mid) >> 22) + _DISCORD_EPOCH_MS
                logger.info("[%s] receipt ack %s on %s landed %d ms after send",
                            self.name, emoji, mid, max(0, int(time.time() * 1000) - sent_ms))

    async def flush_reactions(self) -> None:
        """Wait for every scheduled reaction call (shutdown and tests; a turn never does)."""
        while tails := list(self.__dict__.get("_receipt_tails", {}).values()):
            await asyncio.wait(tails)

    # -- queue positions ---------------------------------------------------------------------------

    @staticmethod
    def _event_message_ids(event: MessageEvent) -> list[str]:
        return event.message_ids() or [f"event-{id(event)}"]

    def _queues(self) -> dict:
        """session key -> [last number, numbered ids, open [sender, number, ids] or None]."""
        return self.__dict__.setdefault("_queue_positions", {})

    def _clear_mark(self, mid: str) -> None:
        """Remove message *mid*'s number; its conversation's counter restarts once none are left."""
        held = self._receipt_marks().pop(mid, None)
        if held is None:
            return
        emoji, message, channel_id, key, waiting = held
        queue = self._queues().get(key)
        if queue is not None:
            queue[1].discard(mid)
            if queue[2]:
                queue[2][2].discard(mid)
                if not queue[2][2]:
                    queue[2] = None
            if not queue[1]:
                queue[0] = 0
                if queue[2] is None:
                    self._queues().pop(key, None)
        ops = [("remove", _BEYOND)] if waiting else []
        if emoji != _BEYOND:
            ops.append(("remove", emoji))
        self._schedule_reaction(mid, message, channel_id, *ops)

    def _acknowledge_receipt(self, message: Any, source: Any) -> None:
        """Schedule receipt display: idle Working or the open busy batch's marks. Never raises."""
        try:
            self._post_receipt_progress(message, source)
            from gateway.session_ingress_burst import holds_busy_burst, reserve_busy_receipt
            key = self._source_session_key(source)
            if key not in self._active_sessions and not holds_busy_burst(self, key):
                return
            mid = str(message.id)
            stub = MessageEvent(text="", source=source, raw_message=message, message_id=mid)
            burst = reserve_busy_receipt(self, stub, key) if self._shared_runtime_owns_input(stub) else None
            self.__dict__.setdefault("_receipt_undispatched", {})[mid] = source
            if not self._reactions_enabled():
                return
            queue = self._queues().setdefault(key, [0, set(), None])
            sender = source.user_id_alt or source.user_id
            if burst is not None:
                if burst.number is None:
                    queue[0] += 1
                    burst.number = queue[0]
                number = burst.number
            else:
                if queue[2] is None or queue[2][0] != sender:
                    queue[0] += 1
                    queue[2] = [sender, queue[0], set()]
                queue[2][2].add(mid)
                number = queue[2][1]
            emoji = _POSITIONS[number - 1] if number <= len(_POSITIONS) else _BEYOND
            if emoji != _BEYOND:
                queue[1].add(mid)
            marks = self._receipt_marks()
            marks[mid] = [emoji, message, self._home_channel_id(stub, mid), key, True]
            while len(marks) > self._RECEIPT_STATE_CAP:
                self._clear_mark(next(iter(marks)))
            ops = [("add", _BEYOND)]
            if emoji != _BEYOND:
                ops.append(("add", emoji))
            self._schedule_reaction(mid, message, marks[mid][2], *ops, receipt=True)
            self.__dict__.setdefault("_receipt_undispatched", {})[mid] = source
        except Exception:
            logger.debug("[%s] queue position skipped", self.name, exc_info=True)

    def on_busy_burst_closed(self, source: Any, message_ids: list[str]) -> None:
        for mid in message_ids:
            held = self._receipt_marks().get(mid)
            if held is None:
                continue
            _, message, channel_id, key, waiting = held
            if waiting:
                held[4] = False
                self._schedule_reaction(mid, message, channel_id, ("remove", _BEYOND))
            queue = self._queues().get(key)
            if queue and queue[2] and mid in queue[2][2]:
                queue[2] = None

    def _post_receipt_progress(self, message: Any, source: Any) -> None:
        """Start the existing compositor before downloads; same-session text chunks share it."""
        from gateway.session_ingress_burst import holds_busy_burst
        key = self._source_session_key(source)
        if key in self._active_sessions or holds_busy_burst(self, key):
            return
        pending = self.__dict__.setdefault("_receipt_progress", {})
        mid = str(message.id)
        if key not in pending:
            from hermes_cli.config import read_raw_config_readonly
            from gateway.display_config import resolve_display_setting
            from gateway.bot_peer_notices import automatic_notices_allowed
            from gateway.progress_compositor import ProgressCompositor
            if not automatic_notices_allowed(source) or resolve_display_setting(
                    read_raw_config_readonly() or {}, "discord", "progress_compositor", "off") != "single_message":
                return
            compositor = ProgressCompositor(self, source.chat_id, reply_to=mid,
                                            metadata={"non_conversational": True}, session_key=key)
            compositor.start_nowait()
            pending[key] = [compositor, set()]
        pending[key][1].add(mid)
        self.__dict__.setdefault("_receipt_undispatched", {})[mid] = source

    def take_receipt_progress(self, session_key: str, generation: Any, message_ids: list[str]) -> Any:
        pending = self.__dict__.get("_receipt_progress", {})
        held = pending.get(session_key)
        if held is None or not held[1].intersection(message_ids):
            return None
        del pending[session_key]
        held[0].generation = generation
        return held[0]

    def _drop_receipt_progress(self, message_ids: list[str]) -> None:
        from types import SimpleNamespace
        from gateway.run_turn_bubble_cleanup import detach_turn_bubble_delete
        pending = self.__dict__.get("_receipt_progress", {})
        for key, (compositor, ids) in list(pending.items()):
            ids.difference_update(message_ids)
            if ids:
                continue
            del pending[key]
            detach_turn_bubble_delete(SimpleNamespace(
                _cleanup_progress=True, _cleanup_adapter=self, _cleanup_msg_ids=[],
                progress_compositor_mode="single_message", progress_compositor=compositor,
                session_key=key, source=SimpleNamespace(chat_id=compositor.chat_id)))

    def _bind_receipt(self, message: Any, event: MessageEvent) -> bool:
        """Let drop paths that hold only *event* clear its marks (``gateway.receipt_drops``)."""
        from gateway.receipt_drops import bind_adapter
        bind_adapter(event, self)
        burst = self.__dict__.get('_shared_burst_receipts', {}).get(str(message.id))
        return burst is None or str(message.id) in burst.receipts

    def _receipt_dispatched(self, message: Any) -> None:
        """*message* became an event handed to ``handle_message``: its own drop paths own the mark now."""
        self.__dict__.get("_receipt_undispatched", {}).pop(str(message.id), None)

    async def _guard_receipt(self, message: Any, call: Any) -> Any:
        """Run ``call()``; a message numbered on receipt but never dispatched (a failed attachment
        download, an early return, an exception) loses its number."""
        try:
            return await call()
        finally:
            if self.__dict__.get("_receipt_undispatched", {}).pop(str(message.id), None) is not None:
                self.on_messages_dropped(None, [str(message.id)])
            self.__dict__.get('_shared_burst_receipts', {}).pop(str(message.id), None)

    async def _settle_reactions(self, timeout: float = 5.0) -> None:
        """Let scheduled reaction calls finish (bounded) before shutdown cancels background tasks."""
        try:
            await asyncio.wait_for(self.flush_reactions(), timeout)
        except TimeoutError:
            logger.debug("[%s] reaction calls still pending at shutdown", self.name)

    def on_messages_dropped(self, source: Any, message_ids: list[str]) -> None:
        """Messages that will never run (``/stop``, a replaced slot, a refused queue) lose their number.
        Reads no config: drop paths run outside the message's profile scope."""
        from gateway.session_ingress_burst import drop_busy_receipts
        drop_busy_receipts(self, message_ids)
        self._drop_receipt_progress(message_ids)
        for mid in message_ids:
            self._clear_mark(mid)

    def on_messages_joined_turn(self, source: Any, message_ids: list[str]) -> None:
        """Messages absorbed by a turn that already started are being worked on."""
        for mid in message_ids:
            self._clear_mark(mid)

    # -- processing lifecycle hooks ---------------------------------------------------------------

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Work began on every message the turn carries: remove their numbers and record durable
        handling state (``emoji_ack`` keeps meaning "reactions were enabled")."""
        for mid in self._event_message_ids(event):
            self._clear_mark(mid)
        await asyncio.to_thread(self._record_discord_processing_start, event,
                                emoji_ack=self._reactions_enabled())

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Record durable state; outcomes get no reaction."""
        self._drop_receipt_progress(event.message_ids())
        await asyncio.to_thread(self._record_discord_processing_complete, event, outcome)
