"""Bounded, freshly confirmed Discord renames. Discord has no atomic name precondition."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from agent.retry_utils import parse_retry_after_seconds
from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url


@asynccontextmanager
async def _transport(adapter):
    session_options, request_options = proxy_kwargs_for_aiohttp(resolve_proxy_url(platform_env_var="DISCORD_PROXY"))
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {adapter.config.token}"},
                                     timeout=aiohttp.ClientTimeout(total=15), **session_options) as session:
        yield session, request_options


async def _request(session, options, method, thread_id, **kwargs):
    async with session.request(method, f"https://discord.com/api/v10/channels/{int(thread_id)}",
                               **options, **kwargs) as response:
        try:
            body = await response.json()
        except (ValueError, aiohttp.ContentTypeError):
            body = {}
        return response.status, body if isinstance(body, dict) else {}


def _thread_name(status, body):
    if status == 200 and body.get("type") in (10, 11, 12) and isinstance(body.get("name"), str):
        return body["name"]
    return None


def _rate_limit(adapter, thread_id, status, body):
    if status != 429:
        return None
    delay = parse_retry_after_seconds(body.get("retry_after"))
    # No server deadline means no invented cooldown. The request still reports rate limiting.
    if delay is not None:
        cooldowns = getattr(adapter, "_explicit_rename_cooldowns", {})
        cooldowns[str(thread_id)] = time.monotonic() + max(0, delay)
        adapter._explicit_rename_cooldowns = cooldowns
    return {"status": "rate_limited", "retry_after": delay}


async def read_thread_name(adapter, thread_id: str) -> str | None:
    async with _transport(adapter) as (session, options):
        status, body = await _request(session, options, "GET", thread_id)
        _rate_limit(adapter, thread_id, status, body)
        return _thread_name(status, body)


async def rename_thread_confirmed(adapter, thread_id: str, *, name: str, expected_name: str) -> dict[str, Any]:
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        return {"status": "invalid"}
    cooldowns = getattr(adapter, "_explicit_rename_cooldowns", None)
    if cooldowns is None:
        cooldowns = adapter._explicit_rename_cooldowns = {}
    wait = cooldowns.get(str(thread_id), 0) - time.monotonic()
    if wait > 0:
        return {"status": "rate_limited", "retry_after": wait}
    patched = False
    try:
        async with _transport(adapter) as (session, options):
            status, body = await _request(session, options, "GET", thread_id)
            if limited := _rate_limit(adapter, thread_id, status, body):
                return limited
            current = _thread_name(status, body)
            if current != expected_name:
                return {"status": "conflict", "name": current}
            patched = True  # from this point a timeout/cancellation may follow a committed remote write
            status, body = await _request(session, options, "PATCH", thread_id, json={"name": name})
            if limited := _rate_limit(adapter, thread_id, status, body):
                return limited
            if status >= 500:
                return {"status": "uncertain"}
            if status != 200:
                return {"status": "refused"}
            status, body = await _request(session, options, "GET", thread_id)
            _rate_limit(adapter, thread_id, status, body)
            confirmed = _thread_name(status, body)
            return {"status": "confirmed" if confirmed == name else "uncertain", "name": confirmed}
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        return {"status": "uncertain" if patched else "unavailable"}
