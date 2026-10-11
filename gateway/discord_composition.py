"""Fixed-window Discord queue composition. Only the first event owns the FIFO slot."""
import asyncio
from dataclasses import dataclass, field
import logging
import time

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 30.0


@dataclass
class Composition:
    event: object
    deadline: float
    messages: list = field(default_factory=list)
    received: list = field(default_factory=list)
    sealed: asyncio.Event = field(default_factory=asyncio.Event)
    seal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: object = None
    begun: bool = False
    reacted: bool = False
    reaction_removed: bool = False
    reaction_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reaction_attempts: int = 0
    selected: bool = False
    cancelled: bool = False


async def begin_composition_turn(event):
    """Hold both runner and adapter drains until the fixed window has sealed."""
    state = getattr(event, "_discord_composition", None)
    if state is None:
        return
    state.begun = True
    await state.sealed.wait()
    await remove_composition_reaction(event)


async def remove_composition_reaction(event):
    """Retry a failed waiting-marker removal at completion, bounded to two attempts."""
    state = getattr(event, "_discord_composition", None)
    if state is None:
        return
    async with state.reaction_lock:
        if not state.reacted or state.reaction_removed or state.reaction_attempts >= 2:
            return
        state.reaction_attempts += 1
        try:
            await state.messages[-1].remove_reaction(
                "⏳", getattr(state.event, "_discord_composition_bot", None))
        except Exception:
            logger.debug("Could not remove Discord queue reaction", exc_info=True)
        else:
            state.reaction_removed = True


async def seal_composition(state, adapter, is_waiting=None):
    """Fetch each message once at seal; never mutate its text afterwards."""
    async with state.seal_lock:
        if state.sealed.is_set():
            return
        try:
            parts = []
            for index, message in enumerate(state.messages):
                try:
                    fresh = await message.channel.fetch_message(message.id)
                    text = fresh.content
                    bot_id = getattr(getattr(state.event, "_discord_composition_bot", None), "id", None)
                    if bot_id is not None:
                        # Match the Discord ingress normalization; edits must not reintroduce
                        # a bot mention that was stripped before normal dispatch.
                        text = text.replace(f"<@{bot_id}>", "").replace(f"<@!{bot_id}>", "").strip()
                    if text.lstrip().startswith("/"):
                        # Edits cannot promote admitted ordinary text to a control command.
                        text = state.received[index] if index < len(state.received) else state.event.text
                    parts.append(text)
                except Exception:
                    # A deleted/unfetchable message keeps its receipt text, not a newer edit.
                    logger.debug("Discord queue seal fetch failed", exc_info=True)
                    parts.append(state.received[index] if index < len(state.received)
                                 else state.event.text)
            state.event.text = "\n".join(parts)
            last = state.messages[-1]
            state.event.reply_anchor_override = str(last.id)
            # The FIFO owner keeps its physical identity. The last member owns only
            # the waiting marker; recovery hooks account for every member separately.
            if not state.cancelled and not state.begun and (is_waiting is None or is_waiting()):
                try:
                    await last.add_reaction("⏳")
                    state.reacted = True
                except Exception:
                    logger.debug("Discord queue reaction failed", exc_info=True)
        finally:
            state.sealed.set()
