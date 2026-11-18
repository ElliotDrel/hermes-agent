"""Explicit personal-draft markers on Discord posts (HERMES-FORK-002)."""

from __future__ import annotations

import re

_DISCORD_DRAFT_MESSAGE_RE = re.compile(r"^/?drafts?(?=$|[:\s])", re.IGNORECASE)


def is_discord_draft_message(content: str) -> bool:
    """Return whether a Discord post has Elliot's explicit private-draft marker."""
    return bool(_DISCORD_DRAFT_MESSAGE_RE.match((content or "").strip()))
