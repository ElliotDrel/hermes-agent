"""Delivery-only response transforms; never mutate agent results or stored history."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Recognize only a trailing machine-metadata block, not arbitrary italic prose.
_METADATA_TAIL = re.compile(
    r"(?:\n[ \t]*\n|\A)(?:\*(?:Model:|Provider:|Context used:|Compactions:|Limits left:)"
    r"[^\n]*\*[ \t]*(?:\n|\Z))+\s*\Z", re.IGNORECASE,
)


def strip_response_metadata_footer(text: str) -> str:
    """Drop a recognized trailing metadata block from a display copy only."""
    while (match := _METADATA_TAIL.search(text)):
        if not re.search(r"\*Model:", match.group(), re.IGNORECASE):
            break
        text = text[:match.start()].rstrip()
    return text


def transform_discord_response(text: str, result: dict, platform) -> str:
    """Apply the first delivery-hook string; unavailable/broken plugins fail open.

    This runs after the agent's durable flush. Callers keep ``final_response`` and
    ``messages`` unchanged and pass only the returned display copy to transport.
    """
    if getattr(platform, "value", platform) != "discord" or not text:
        return text
    if text.rstrip().upper().endswith("[SILENT]"):
        return text
    from hermes_cli.lifecycle import invoke_hook

    try:
        for value in invoke_hook(
            "transform_gateway_response", response_text=text, platform="discord",
            session_id=result.get("session_id") or "", model=result.get("model"),
            provider=result.get("provider"),
            context_used_tokens=result.get("context_used_tokens"),
            compaction_trigger_tokens=result.get("compaction_trigger_tokens"),
            compaction_count=result.get("compaction_count"),
        ):
            if isinstance(value, str) and value:
                return value
    except Exception:
        logger.debug("Discord delivery transform failed; delivering original", exc_info=True)
    return text
