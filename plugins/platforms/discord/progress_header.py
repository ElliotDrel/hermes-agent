"""Discord receipt snapshots and atomic progress-header variants."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MessagePreview:
    text: str
    url: str
    number: int = 0


def excerpt(text: str, width: int) -> str:
    value = " ".join(text.split())
    if len(value) > width:
        value = value[:width - 1] + "…"
    return "".join("\\" + ch if ch in "\\[]()`*_~|" else ch for ch in value)


@dataclass
class DiscordProgressHeader:
    messages: dict[str, MessagePreview] = field(default_factory=dict)
    active: list[str] = field(default_factory=list)
    compositor: object = None

    def remember(self, message, source, number=0):
        mid = str(message.id)
        text = getattr(message, "content", "") or ""
        attachments = getattr(message, "attachments", ())
        if not text and attachments:
            text = "📎 " + attachments[0].filename
        channel = source.parent_chat_id if source.auto_thread_created else source.chat_id
        url = getattr(message, "jump_url", None) or (
            f"https://discord.com/channels/{source.guild_id or '@me'}/{channel}/{mid}")
        self.messages[mid] = MessagePreview(text, url, number)
        self.refresh()

    def attach(self, compositor, ids):
        self.compositor = compositor
        self.active = list(ids)
        compositor.reply_to = None
        self.refresh()

    def queued(self):
        groups = {}
        for mid, preview in self.messages.items():
            if mid not in self.active and preview.number:
                groups.setdefault(preview.number, []).append(mid)
        return list(groups.items())

    def refresh(self):
        c = self.compositor
        active = [self.messages[mid] for mid in self.active if mid in self.messages]
        if c is None or c.closed or not active:
            return
        first = active[0]
        more = f" +{len(self.active) - 1} more" if len(self.active) > 1 else ""
        queue = self.queued()
        variants = []
        for width in (25, 12, 4, 0):
            line = f'↪ **["{excerpt(first.text, 60)}"]({first.url})**{more}'
            if queue and width:
                entries = []
                for number, ids in queue[:3]:
                    preview = self.messages[ids[0]]
                    keycap = str(number) + "️⃣" if number < 10 else ("🔟" if number == 10 else "⏳")
                    entry = f'{keycap} ["{excerpt(preview.text, width)}"]({preview.url})'
                    if len(ids) > 1:
                        entry += f" +{len(ids) - 1}"
                    entries.append(entry)
                if len(queue) > 3:
                    entries.append(f"+{len(queue) - 3} more")
                line += f"\nQueue - **{len(queue)}** - " + " | ".join(entries)
            variants.append(line)
        for width in (30, 8):
            variants.append(f'↪ **["{excerpt(first.text, width)}"]({first.url})**{more}')
        c.set_header(variants[0], compact=tuple(variants[1:]))

    def remove(self, ids):
        for mid in ids:
            self.messages.pop(mid, None)
        self.active = [mid for mid in self.active if mid not in ids]
        self.refresh()
