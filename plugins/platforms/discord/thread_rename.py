"""Bounded, confirmed thread rename through discord.py's own HTTP layer."""
from __future__ import annotations

import asyncio

import discord

from hermes_state import SessionDB

RENAME_TIMEOUT = 15.0


def validate_thread_name(name: str) -> str:
    # Discord documents 1–100 characters. Reject normalization, rather than claiming
    # that a different visible name is the user's exact requested title.
    if not name or len(name) > 100:
        raise ValueError("Discord thread names must contain 1–100 characters; choose a shorter name.")
    if SessionDB.sanitize_title(name) != name:
        raise ValueError("The name contains unsupported characters or whitespace; choose an exact, single-line name.")
    return name


class RenameUnconfirmed(RuntimeError):
    """A sent edit may have landed, but its current visible result is unknown."""


class RenameLookupFailed(RuntimeError):
    """The thread lookup timed out before any edit was sent: nothing was renamed."""


def _admit(adapter, thread_id: int) -> dict:
    """Refuse while a retained 429 cooldown (this thread or global) is open."""
    cooldowns = adapter.__dict__.setdefault("_thread_rename_cooldowns", {})
    remaining = max(cooldowns.get(thread_id, 0), cooldowns.get(None, 0)) - asyncio.get_running_loop().time()
    if remaining > 0:
        raise discord.RateLimited(remaining)
    return cooldowns


def _retain(cooldowns: dict, thread_id: int, exc: discord.RateLimited) -> None:
    cooldowns[thread_id] = asyncio.get_running_loop().time() + exc.retry_after


async def read_thread_name(adapter, thread_id: int) -> str:
    """Fresh lookup through ``HTTPClient.get_channel``: native auth, proxy and shared rate limits."""
    cooldowns = _admit(adapter, thread_id)
    try:
        async with asyncio.timeout(RENAME_TIMEOUT):
            data = await adapter._client.http.get_channel(thread_id)
    except discord.RateLimited as exc:
        _retain(cooldowns, thread_id, exc)
        raise
    if data.get("type") not in (10, 11, 12):
        raise ValueError("The Discord channel is not a thread.")
    return data["name"]


async def rename_once(adapter, thread_id: int, name: str, expected: str | None) -> bool:
    """Resolve, guard, edit through the channel's own ``edit``, confirm from Discord's reply; bounded.

    discord.py owns auth, proxy and the shared route/global rate limits. One 15-second budget covers
    the channel lookup and the edit together, so a stalled lookup cannot hold the rename slot, and a
    ``RateLimited`` it raises is retained as a cooldown so later renames do not re-send. A failure
    before the edit is sent (lookup, guard) is a plain error: nothing was renamed. Only a failure
    after the edit left is ``RenameUnconfirmed``. Fresh visible-name lookup for ``/rename`` is
    ``read_thread_name``; the guard here reads the connected client's gateway-fed channel, the same
    value the automatic title lane has always used.
    """
    validate_thread_name(name)
    client = adapter._client
    cooldowns = _admit(adapter, thread_id)
    sent = False
    try:
        async with asyncio.timeout(RENAME_TIMEOUT):
            thread = client.get_channel(thread_id)
            if thread is None:
                thread = await client.fetch_channel(thread_id)
            edit = getattr(thread, "edit", None)
            if edit is None or not hasattr(thread, "parent_id"):
                raise ValueError("The Discord channel is not a thread.")
            current = getattr(thread, "name", None)
            if expected is not None and current != expected:
                raise RuntimeError(f"The thread name changed to {current!r}; leaving the newer decision alone.")
            if current == name:
                return True
            sent = True
            applied = await edit(name=name, reason="Hermes semantic session title")
    except asyncio.CancelledError:
        # No detached task or retry exists. A request already accepted by Discord cannot be
        # revoked, so record that uncertainty instead of claiming rollback.
        if sent:
            import logging
            logging.getLogger(__name__).warning("Thread %s rename cancelled after send; visible state unconfirmed", thread_id)
        raise
    except discord.RateLimited as exc:
        _retain(cooldowns, thread_id, exc)
        raise
    except TimeoutError as exc:
        if not sent:
            raise RenameLookupFailed("Discord did not answer the thread lookup in time; the thread was not renamed.") from exc
        raise RenameUnconfirmed("Discord may have applied the edit. Check the thread name before retrying; no delayed retry remains.") from exc
    except discord.HTTPException as exc:
        if not sent or 400 <= exc.status < 500:
            raise
        raise RenameUnconfirmed("Discord may have applied the edit; check the thread name before retrying.") from exc
    except Exception as exc:
        if not sent:
            raise
        raise RenameUnconfirmed("Discord may have applied the edit. Check the thread name before retrying; no delayed retry remains.") from exc
    if getattr(applied, "name", None) != name:
        raise RenameUnconfirmed(f"Discord returned a different applied name: {getattr(applied, 'name', None)!r}.")
    return True
