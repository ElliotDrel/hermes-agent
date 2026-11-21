"""Discord batch receipt reactions; detached, bounded and serialized per message."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from gateway.platforms.event import MessageEvent, ProcessingOutcome

logger = logging.getLogger(__name__)

_DISCORD_EPOCH_MS = 1420070400000

_WAITING = "⏳"
_KEYCAPS = tuple(str(i) + "\ufe0f\u20e3" for i in range(1, 10)) + ("\U0001f51f",)


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
        """message id -> [emoji currently shown, live message or None, channel id to find it in], oldest first."""
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
            if receipt and kind == "add" and emoji == _WAITING and landed:
                # Discord send time is in the snowflake; the gap is delivery + admission + rate limit + REST.
                sent_ms = (int(mid) >> 22) + _DISCORD_EPOCH_MS
                logger.info("[%s] receipt ack %s on %s landed %d ms after send",
                            self.name, emoji, mid, max(0, int(time.time() * 1000) - sent_ms))

    async def flush_reactions(self) -> None:
        """Wait for every scheduled reaction call (shutdown and tests; a turn never does)."""
        while tails := list(self.__dict__.get("_receipt_tails", {}).values()):
            await asyncio.wait(tails)

    # -- the per-message state machine ------------------------------------------------------------

    @staticmethod
    def _event_message_ids(event: MessageEvent) -> list[str]:
        return event.message_ids() or [f"event-{id(event)}"]

    def _acknowledge_receipt(self, message: Any, source: Any) -> None:
        """Open the batch before downloads; acknowledgement failures never abort dispatch."""
        try:
            from gateway.session_ingress_burst import open_discord_batch
            batch, position = open_discord_batch(self, source, str(message.id))
            self.__dict__.setdefault("_receipt_undispatched", {})[str(message.id)] = source
            enabled = self._reactions_enabled()
            self._receipt_reactions_enabled = enabled
            if not enabled:
                return
            stub = MessageEvent(text="", source=source, raw_message=message, message_id=str(message.id))
            number = _KEYCAPS[position - 1] if position <= len(_KEYCAPS) else None
            channel = self._home_channel_id(stub, str(message.id))
            self._receipt_marks()[str(message.id)] = [number, message, channel]
            while len(self._receipt_marks()) > self._RECEIPT_STATE_CAP:
                self._receipt_marks().pop(next(iter(self._receipt_marks())))
            ops = [("add", _WAITING)] + ([("add", number)] if number else [])
            self._schedule_reaction(str(message.id), message, channel, *ops, receipt=True)
        except Exception:
            logger.debug("[%s] receipt acknowledgement skipped", self.name, exc_info=True)

    def _clear_marks(self, event: MessageEvent, mid: str, dropped: bool = False) -> None:
        held = self._receipt_marks().pop(mid, None)
        ops = [("remove", _WAITING)]
        if dropped:
            ops += [("remove", number) for number in ([held[0]] if held else _KEYCAPS) if number]
        message = (held[1] if held else None) or self._live_message(event, mid)
        channel = held[2] if held else self._home_channel_id(event, mid)
        self._schedule_reaction(mid, message, channel, *ops)

    def _bind_receipt(self, message: Any, event: MessageEvent) -> None:
        """Let drop paths that hold only *event* clear its marks (``gateway.receipt_drops``)."""
        from gateway.receipt_drops import bind_adapter
        bind_adapter(event, self)

    def _receipt_dispatched(self, message: Any) -> None:
        """Standalone dispatch releases immediately; a runner receipt waits for admission/verdict."""
        if getattr(self, "gateway_runner", None) is None:
            self.__dict__.get("_receipt_undispatched", {}).pop(str(message.id), None)
            from gateway.session_ingress_burst import discord_receipt_committed
            discord_receipt_committed(self, str(message.id))

    async def _guard_receipt(self, message: Any, call: Any) -> Any:
        """Run ``call()``; a message acknowledged on receipt but never dispatched (a failed attachment
        download, an early return, an exception) loses its mark instead of keeping waiting reactions forever."""
        accepted = False
        try:
            accepted = await call()
            return accepted
        finally:
            source = self.__dict__.get("_receipt_undispatched", {}).pop(str(message.id), None)
            if source is not None and accepted is not True:
                self.on_messages_dropped(source, [str(message.id)])

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        try:
            await super()._process_message_background(event, session_key)
        finally:
            # Activity hooks intentionally skip unauthorized senders; receipt cleanup must still run.
            if event.message_id in self.__dict__.get("_discord_preparing", {}):
                self.on_messages_dropped(event.source, event.message_ids())

    async def _settle_reactions(self, timeout: float = 5.0) -> None:
        """Let scheduled reaction calls finish (bounded) before shutdown cancels background tasks."""
        try:
            await asyncio.wait_for(self.flush_reactions(), timeout)
        except TimeoutError:
            logger.debug("[%s] reaction calls still pending at shutdown", self.name)

    def on_messages_dropped(self, source: Any, message_ids: list[str]) -> None:
        """Dropped inputs lose both their waiting mark and permanent batch number."""
        from gateway.session_ingress_burst import drop_discord_messages
        drop_discord_messages(self, source, message_ids)
        stub = MessageEvent(text="", source=source)
        for mid in message_ids:
            if mid in self._receipt_marks() or self.__dict__.get('_receipt_reactions_enabled', False):
                self._clear_marks(stub, mid, dropped=True)

    def on_messages_joined_turn(self, source: Any, message_ids: list[str]) -> None:
        stub = MessageEvent(text="", source=source)
        for mid in message_ids:
            if mid in self._receipt_marks() or self.__dict__.get("_receipt_reactions_enabled", False):
                self._clear_marks(stub, mid)

    async def on_processing_start(self, event: MessageEvent) -> None:
        from gateway.session_ingress_burst import discord_batch_descriptor
        from gateway.session_ingress import executing_admission
        if (getattr(self, "gateway_runner", None) is not None and discord_batch_descriptor(self, event)
                and not executing_admission.get()):
            return
        if self._reactions_enabled():
            for mid in self._event_message_ids(event):
                self._clear_marks(event, mid)
        await asyncio.to_thread(self._record_discord_processing_start, event, emoji_ack=False)
        event._discord_processing_started = True

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        from gateway.session_ingress_burst import discord_batch_descriptor
        if (getattr(self, "gateway_runner", None) is not None and discord_batch_descriptor(self, event)
                and not getattr(event, "_discord_processing_started", False)):
            return
        await asyncio.to_thread(self._record_discord_processing_complete, event, outcome)
