"""Native terminal hooks and exact-turn confirmed delivery obligations."""
import asyncio
import inspect
import logging
from typing import Callable, Awaitable

logger = logging.getLogger("gateway.platforms.base")


class DeliveryCallbacksMixin:
    def register_post_delivery_callback(
        self, session_key: str, callback: Callable, *, generation: int | None = None) -> None:
        """Register a deferred callback to fire after the main response. Same-key registrations are
        chained (both fire in order, per-callback exception isolation) so independent features
        coexist; ``generation`` ties it to a gateway run — stale generations never overwrite a
        fresher slot."""
        if not session_key or not callable(callback):
            return
        from gateway.platforms.base import _split_post_delivery_entry
        existing = self._post_delivery_callbacks.get(session_key)
        if existing is not None:
            existing_gen, existing_cb = _split_post_delivery_entry(existing)
            if existing_gen is not None and generation is not None and int(generation) < int(existing_gen):
                return
            # Same-or-newer generation: chain so both fire in registration order.
            if callable(existing_cb) and (
                existing_gen is None or generation is None or int(existing_gen) == int(generation)):
                callback = self._chain_callbacks(existing_cb, callback)
        self._post_delivery_callbacks[session_key] = (
            callback if generation is None else (int(generation), callback))


    def register_confirmed_delivery_callback(self, owner: object, callback: Callable) -> None:
        """Register a progress obligation against an opaque, immutable turn token."""
        pending = self._confirmed_delivery_callbacks
        if owner not in pending and len(pending) >= 128:
            pending.pop(next(iter(pending)))
            logger.warning("Expired oldest unconfirmed progress obligation (128-turn bound)")
        pending.setdefault(owner, []).append(callback)


    async def confirm_delivery(self, owner: object) -> None:
        """Settle progress after transport confirmation of this exact turn."""
        await self._settle_progress_obligations(owner)

    async def settle_no_response(self, owner: object) -> None:
        """Settle this turn's progress after native policy authorized intentional silence.

        This does not confirm a transport send. Callers must not use it for unknown empty,
        failed or declined output. It shares the same owner serialization/retry obligations.
        """
        await self._settle_progress_obligations(owner)

    async def _settle_progress_obligations(self, owner: object) -> None:
        """Claim this owner's obligations; retry failures without replaying successful siblings."""
        from gateway.platforms.base import _POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS
        pending = self._confirmed_delivery_callbacks.get(owner)
        inflight = self.__dict__.setdefault('_confirmed_delivery_inflight', set())
        if not pending or owner in inflight:
            return
        inflight.add(owner)
        try:
            for attempt in range(3):
                for callback in tuple(pending):
                    try:
                        result = callback()
                        if inspect.isawaitable(result):
                            result = await asyncio.wait_for(result, timeout=_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS)
                        if result is False:
                            continue
                    except Exception:
                        logger.debug("Confirmed progress obligation failed", exc_info=True)
                        continue
                    pending.remove(callback)
                if not pending:
                    self._confirmed_delivery_callbacks.pop(owner, None)
                    return
                if attempt < 2:
                    await asyncio.sleep(0.1)
            logger.warning("Progress cleanup remains pending after three attempts")
        finally:
            inflight.discard(owner)

    @staticmethod
    def _chain_callbacks(*callbacks: Callable) -> Callable[[], Awaitable[None]]:
        """Async wrapper running ``callbacks`` in order with per-callback exception isolation;
        async so coroutines returned by async hooks are awaited, not dropped."""
        async def _chained() -> None:
            for _cb in callbacks:
                try:
                    _result = _cb()
                    if inspect.isawaitable(_result):
                        await _result
                except Exception:
                    logger.debug("Post-delivery callback failed", exc_info=True)
        return _chained


    def pop_post_delivery_callback(
        self, session_key: str, *, generation: int | None = None) -> Callable | None:
        """Pop a deferred callback, optionally requiring generation ownership."""
        from gateway.platforms.base import _split_post_delivery_entry
        entry = self._post_delivery_callbacks.get(session_key) if session_key else None
        if entry is None:
            return None
        entry_generation, callback = _split_post_delivery_entry(entry)
        if generation is not None and (entry_generation is None or int(entry_generation) != int(generation)):
            return None
        self._post_delivery_callbacks.pop(session_key, None)
        return callback if callable(callback) else None
