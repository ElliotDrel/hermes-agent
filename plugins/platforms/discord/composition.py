"""Opt-in fixed-window physical-text composition inside the native busy burst."""
import math
import time

from gateway.platforms.event import MessageEvent
from gateway.session_identity import transport_profile_of

_now = time.monotonic


def window(adapter):
    value = adapter.config.extra.get("busy_composition_window_seconds", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return min(30.0, max(0.0, float(value)))


def _route(event):
    source = event.source
    return (source.profile, source.scope_id, source.chat_id, source.thread_id, source.user_id,
            transport_profile_of(source), event.channel_prompt, event.channel_context, event.auto_skill)


class Composition:
    def __init__(self, head, seconds):
        self.head = head
        self.deadline = _now() + seconds
        self.sealed = False
        self.discarded = False
        self.route = _route(head)
        self.parts = {str(head.message_id): head.text}
        self.last_id = str(head.message_id)

    def add(self, event):
        if (self.sealed or _now() >= self.deadline or not event.message_id
                or event.media_urls or _route(event) != self.route
                or len(self.parts) >= 32 or sum(map(len,self.parts.values())) + len(event.text) + len(self.parts) > 32768):
            return False
        message_id = str(event.message_id)
        if message_id not in self.parts:
            self.parts[message_id] = event.text
            self.last_id = message_id
            self.head.absorb_reply_expected(event)
        return True

    def edit(self, adapter, message, *, deleted=False):
        if self.sealed or _now() >= self.deadline:
            return False
        author, channel = getattr(message, "author", None), getattr(message, "channel", None)
        message_id = str(getattr(message, "id", ""))
        if (message_id not in self.parts or str(getattr(author, "id", "")) != str(self.head.source.user_id)
                or str(getattr(channel, "id", "")) != str(self.head.source.chat_id)
                or getattr(author, "bot", False)):
            return False
        text = normalized_edit(adapter, message)
        if deleted or text is None or getattr(message, "attachments", ()):
            self.discarded = True
            return True
        if MessageEvent(text=text).is_command():
            self.discarded = True
            return True
        if sum(map(len,self.parts.values())) - len(self.parts[message_id]) + len(text) + len(self.parts) - 1 > 32768:
            self.discarded = True
            return True
        self.parts[message_id] = text
        return True

    def seal(self):
        if not self.sealed:
            self.sealed = True
            self.head.text = "\n".join(self.parts.values())
            self.head.reply_anchor_override = self.last_id


def normalized_edit(adapter, message):
    """Use native mention handling and the optional native draft policy; never copy raw edited text."""
    raw = getattr(message, "content", None)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if adapter._self_is_explicitly_mentioned(message):
        bot_id = adapter._client.user.id
        text = text.replace(f"<@{bot_id}>", "").replace(f"<@!{bot_id}>", "").strip()
    draft_filter = getattr(adapter, "_is_draft_message", None)
    if callable(draft_filter) and (draft_filter(raw) or draft_filter(text)):
        return None
    return text if text and len(text) <= 32768 else None


def edit_held(adapter, message, *, deleted=False):
    for key, burst in list(adapter.__dict__.get("_shared_bursts", {}).items()):
        draft = getattr(burst, "composition", None)
        if draft is not None and draft.edit(adapter, message, deleted=deleted):
            if draft.discarded:
                from gateway.session_ingress_burst import discard_busy_burst
                discard_busy_burst(adapter, key)
            return True
    return False
