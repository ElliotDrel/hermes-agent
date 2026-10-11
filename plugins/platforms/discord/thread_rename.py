"""Single-attempt thread rename: never leave discord.py's delayed retry behind."""
from __future__ import annotations

import asyncio
import math
from urllib.parse import quote

import aiohttp
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


async def _request(adapter, session, thread_id: int, method: str, **kwargs):
    http = adapter._client.http
    loop = asyncio.get_running_loop()
    cooldowns = adapter.__dict__.setdefault("_thread_rename_cooldowns", {})
    remaining = max(cooldowns.get(thread_id, 0), cooldowns.get(None, 0)) - loop.time()
    if remaining > 0:
        raise discord.RateLimited(remaining)
    if not http._global_over.is_set():
        raise RuntimeError("Discord's global cooldown is active; retry after it clears.")
    # Respect a native route bucket already known to be exhausted.
    route = discord.http.Route(method, '/channels/{channel_id}', channel_id=thread_id)
    bucket_hash = http._bucket_hashes.get(route.key)
    bucket_key = f'{bucket_hash or route.key}:{route.major_parameters}'
    bucket = http._buckets.get(bucket_key)
    if bucket is not None and bucket.remaining <= 0 and bucket.expires:
        wait = bucket.expires - loop.time()
        if wait > 0:
            raise discord.RateLimited(wait)
    headers = {"Authorization": "Bot " + http.token, "User-Agent": http.user_agent}
    if method == "PATCH":
        headers["X-Audit-Log-Reason"] = quote("Hermes semantic session title", safe="/ ")
    async with session.request(
        method, f"https://discord.com/api/v10/channels/{thread_id}",
        headers=headers, proxy=http.proxy, proxy_auth=http.proxy_auth, **kwargs,
    ) as response:
        data = await discord.http.json_or_text(response)
        if response.status != 429 and 'X-Ratelimit-Remaining' in response.headers:
            native = http.get_ratelimit(bucket_key)
            native.update(response, use_clock=http.use_clock)
        if response.headers.get('X-Ratelimit-Bucket'):
            value = response.headers['X-Ratelimit-Bucket']
            http._bucket_hashes[route.key] = value
            if bucket_key in http._buckets:
                canonical_key = f'{value}:{route.major_parameters}'
                canonical = http._buckets.setdefault(canonical_key, http._buckets[bucket_key])
                if canonical is not http._buckets[bucket_key] and response.status != 429:
                    canonical.update(response, use_clock=http.use_clock)
        if response.status == 429:
            retry = data.get("retry_after") if isinstance(data, dict) else None
            try:
                retry = float(retry)
                if not math.isfinite(retry) or retry < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise discord.HTTPException(response, data)
            key = None if data.get("global") else thread_id
            cooldowns[key] = loop.time() + retry
            if key is None:
                http._global_over.clear()
                # Expire only the global admission gate, never retry a rename.
                deadline = cooldowns[key]
                def release_global():
                    if cooldowns.get(None) == deadline:
                        http._global_over.set()
                loop.call_later(retry, release_global)
            raise discord.RateLimited(retry)
        if response.status == 403:
            raise discord.Forbidden(response, data)
        if response.status == 404:
            raise discord.NotFound(response, data)
        if not 200 <= response.status < 300:
            raise discord.HTTPException(response, data)
        return data



async def read_thread_name(adapter, thread_id: int) -> str:
    """Single-attempt fresh lookup, with the same cooldown admission as editing."""
    http = adapter._client.http
    async with asyncio.timeout(RENAME_TIMEOUT):
        async with aiohttp.ClientSession(connector=http.connector if http.connector is not discord.utils.MISSING else None, connector_owner=http.connector is discord.utils.MISSING) as session:
            data = await _request(adapter, session, thread_id, "GET")
            if data.get("type") not in (10, 11, 12):
                raise ValueError("The Discord channel is not a thread.")
            return data["name"]


async def rename_once(adapter, thread_id: int, name: str, expected: str | None) -> bool:
    """Read, compare, edit, read back; bounded and with no automatic HTTP retries.

    Use the connected native client's token/proxy/user-agent, but not its shared
    HTTP policy. Per-thread cooldowns also cover Discord's rename sub-rate-limit.
    """
    validate_thread_name(name)
    http = adapter._client.http
    loop = asyncio.get_running_loop()
    cooldowns = adapter._thread_rename_cooldowns
    remaining = max(cooldowns.get(thread_id, 0), cooldowns.get(None, 0)) - loop.time()
    if remaining > 0:
        raise discord.RateLimited(remaining)
    sent = False
    try:
        async with asyncio.timeout(RENAME_TIMEOUT):
            async with aiohttp.ClientSession(connector=http.connector if http.connector is not discord.utils.MISSING else None, connector_owner=http.connector is discord.utils.MISSING) as session:
                before = await _request(adapter, session, thread_id, "GET")
                if before.get("type") not in (10, 11, 12):
                    raise ValueError("The Discord channel is not a thread.")
                current = before.get("name")
                if expected is not None and current != expected:
                    raise RuntimeError(f"The thread name changed to {current!r}; leaving the newer decision alone.")
                if current == name:
                    return True
                sent = True
                try:
                    applied = await _request(adapter, session, thread_id, "PATCH", json={"name": name})
                except discord.RateLimited:
                    sent = False
                    raise
                except discord.HTTPException as exc:
                    if 400 <= exc.status < 500:
                        sent = False
                    raise
                if applied.get("name") != name:
                    raise RenameUnconfirmed(f"Discord returned a different applied name: {applied.get('name')!r}.")
                confirmed = await _request(adapter, session, thread_id, "GET")
                if confirmed.get("name") != name:
                    raise RenameUnconfirmed(f"The current Discord name is {confirmed.get('name')!r}; saved title was not changed.")
                return True
    except asyncio.CancelledError:
        # No detached task or retry exists. A request already accepted by Discord
        # cannot be revoked, so record that uncertainty instead of claiming rollback.
        if sent:
            import logging
            logging.getLogger(__name__).warning("Thread %s rename cancelled after send; visible state unconfirmed", thread_id)
        raise
    except RenameUnconfirmed:
        raise
    except (discord.RateLimited, discord.HTTPException, ValueError):
        if sent:
            raise RenameUnconfirmed("Discord may have applied the edit, but read-back failed. Check the thread name before retrying.")
        raise
    except Exception as exc:
        if sent:
            raise RenameUnconfirmed("Discord may have applied the edit. Check the thread name before retrying; no delayed retry remains.") from exc
        raise
