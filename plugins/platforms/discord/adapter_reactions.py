"""Discord receipt and processing reactions, one per message.

Every accepted message is acknowledged the moment it arrives: ⏳ when its conversation is busy (it
queues or merges into a pending turn), 👀 when idle. A turn marks every message it carries 👀 when it
starts and ✅ or ❌ by its outcome when it ends; a message dropped before it ran loses its mark.

Display only: each reaction call is a detached task, bounded by ``_RECEIPT_OP_TIMEOUT`` and serialized
per message, so a Discord 429, failure or hang never delays admission, the turn or delivery.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.platforms.event import MessageEvent, ProcessingOutcome

logger = logging.getLogger(__name__)

_WAITING = "⏳"
_WORKING = "👀"
_OUTCOME_EMOJI = {ProcessingOutcome.SUCCESS: "✅", ProcessingOutcome.FAILURE: "❌"}


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

    def _schedule_reaction(self, mid: str, message: Any, channel_id: str, *ops: tuple[str, str]) -> None:
        """Run ``("add" | "remove", emoji)`` calls for one message as a detached task that follows
        that message's earlier calls and gives up after ``_RECEIPT_OP_TIMEOUT``. The target is captured
        here, because the caller may drop the state it came from before the task runs."""
        tails = self.__dict__.setdefault("_receipt_tails", {})
        prior = tails.get(mid)

        async def run() -> None:
            if prior is not None:
                await asyncio.wait({prior})
            try:
                await asyncio.wait_for(self._apply_reactions(mid, message, channel_id, ops), self._RECEIPT_OP_TIMEOUT)
            except Exception as exc:
                logger.debug("[%s] reaction update for %s abandoned: %r", self.name, mid, exc)

        task = asyncio.get_running_loop().create_task(run())
        tails[mid] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(lambda done: tails.get(mid) is done and tails.pop(mid, None))

    async def _apply_reactions(self, mid: str, message: Any, channel_id: str, ops: tuple[tuple[str, str], ...]) -> None:
        if message is None:
            message = await self._partial_message(mid, channel_id)
        if not hasattr(message, "add_reaction"):
            return
        for kind, emoji in ops:
            await (self._add_reaction if kind == "add" else self._remove_reaction)(message, emoji)

    async def flush_reactions(self) -> None:
        """Wait for every scheduled reaction call (shutdown and tests; a turn never does)."""
        while tails := list(self.__dict__.get("_receipt_tails", {}).values()):
            await asyncio.wait(tails)

    # -- the per-message state machine ------------------------------------------------------------

    @staticmethod
    def _event_message_ids(event: MessageEvent) -> list[str]:
        return event.message_ids() or [f"event-{id(event)}"]

    def _set_mark(self, event: MessageEvent, mid: str, emoji: str, message: Any = None) -> None:
        """Show *emoji* on message *mid*, replacing the mark it has (a restored message may carry a
        waiting mark from before the restart that this process never recorded)."""
        marks = self._receipt_marks()
        held = marks.pop(mid, None)
        if held is not None and held[0] == emoji:
            marks[mid] = held
            return
        ops: list[tuple[str, str]] = []
        if held is not None:
            ops.append(("remove", held[0]))
        elif emoji != _WAITING:
            # Unrecorded (restored, evicted past the cap, or a timed-out earlier call): a ⏳ may be showing.
            ops.append(("remove", _WAITING))
        ops.append(("add", emoji))
        if message is None:
            message = (held[1] if held else None) or self._live_message(event, mid)
        channel_id = held[2] if held else self._home_channel_id(event, mid)
        marks[mid] = [emoji, message, channel_id]
        while len(marks) > self._RECEIPT_STATE_CAP:
            marks.pop(next(iter(marks)))
        self._schedule_reaction(mid, message, channel_id, *ops)

    def _clear_marks(self, event: MessageEvent, mid: str, final: str | None) -> None:
        """Remove the mark of message *mid*, then show *final* (a result) when given."""
        held = self._receipt_marks().pop(mid, None)
        ops: list[tuple[str, str]] = [("remove", held[0] if held else _WORKING)]
        if held is None:
            ops.append(("remove", _WAITING))
        if final:
            ops.append(("add", final))
        message = (held[1] if held else None) or self._live_message(event, mid)
        channel_id = held[2] if held else self._home_channel_id(event, mid)
        self._schedule_reaction(mid, message, channel_id, *ops)

    def _acknowledge_receipt(self, message: Any, source: Any) -> None:
        """Mark a just-accepted message ⏳ when its conversation is busy, else 👀. Never raises."""
        if not self._reactions_enabled():
            return
        try:
            from gateway.session_ingress_burst import holds_busy_burst
            key = self._source_session_key(source)
            busy = key in self._active_sessions or holds_busy_burst(self, key)
            stub = MessageEvent(text="", source=source, raw_message=message, message_id=str(message.id))
            self._set_mark(stub, str(message.id), _WAITING if busy else _WORKING, message)
            self.__dict__.setdefault("_receipt_undispatched", {})[str(message.id)] = source
        except Exception:
            logger.debug("[%s] receipt acknowledgement skipped", self.name, exc_info=True)

    def _bind_receipt(self, message: Any, event: MessageEvent) -> None:
        """Let drop paths that hold only *event* clear its marks (``gateway.receipt_drops``)."""
        from gateway.receipt_drops import bind_adapter
        bind_adapter(event, self)

    def _receipt_dispatched(self, message: Any) -> None:
        """*message* became an event handed to ``handle_message``: its own drop paths own the mark now."""
        self.__dict__.get("_receipt_undispatched", {}).pop(str(message.id), None)

    async def _guard_receipt(self, message: Any, call: Any) -> Any:
        """Run ``call()``; a message acknowledged on receipt but never dispatched (a failed attachment
        download, an early return, an exception) loses its mark instead of keeping 👀 forever."""
        try:
            return await call()
        finally:
            source = self.__dict__.get("_receipt_undispatched", {}).pop(str(message.id), None)
            if source is not None:
                self.on_messages_dropped(source, [str(message.id)])

    async def _settle_reactions(self, timeout: float = 5.0) -> None:
        """Let scheduled reaction calls finish (bounded) before shutdown cancels background tasks."""
        try:
            await asyncio.wait_for(self.flush_reactions(), timeout)
        except TimeoutError:
            logger.debug("[%s] reaction calls still pending at shutdown", self.name)

    def on_messages_dropped(self, source: Any, message_ids: list[str]) -> None:
        """Messages marked on receipt will never run (``/stop``, a replaced slot, a refused queue): clear
        them. A mark exists only if reactions were enabled when it was set, so this reads no config
        (drop paths run outside the message's profile scope)."""
        stub = MessageEvent(text="", source=source)
        for mid in message_ids:
            if mid in self._receipt_marks():
                self._clear_marks(stub, mid, None)

    def on_messages_joined_turn(self, source: Any, message_ids: list[str]) -> None:
        """Messages folded into a turn that already started (steer): ⏳ becomes 👀 at once."""
        stub = MessageEvent(text="", source=source)
        for mid in message_ids:
            if mid in self._receipt_marks():
                self._set_mark(stub, mid, _WORKING)

    # -- processing lifecycle hooks ---------------------------------------------------------------

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Mark every message the turn carries 👀 and record durable handling state. The recovery
        column ``emoji_ack`` records that the 👀 was scheduled (reactions enabled): the call itself is
        detached and best effort, so its outcome is not known here."""
        acked = self._reactions_enabled()
        if acked:
            for mid in self._event_message_ids(event):
                self._set_mark(event, mid, _WORKING)
        await asyncio.to_thread(self._record_discord_processing_start, event, emoji_ack=acked)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap every carried message's mark for its final reaction and record durable state."""
        await asyncio.to_thread(self._record_discord_processing_complete, event, outcome)
        if not self._reactions_enabled():
            return
        for mid in self._event_message_ids(event):
            self._clear_marks(event, mid, _OUTCOME_EMOJI.get(outcome))
