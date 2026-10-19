"""Completion delivery for gateway ``/update``: the requester first, then that profile's home channel.

Only a ``SendResult`` with ``success=True`` confirms delivery (the adapter send contract); anything
else did not deliver, and the caller retains the pending invocation for a later retry.
"""

import logging

logger = logging.getLogger(__name__)


def _report_targets(runner, pending: dict, adapter, platform, metadata) -> list[tuple[str, object]]:
    targets = [(str(pending["chat_id"]), metadata)]
    # ``adapter`` is the requester's own profile bot, so its configured home is that profile's.
    home = getattr(getattr(adapter, "config", None), "home_channel", None)
    if home is None or not home.chat_id:
        return targets
    same = (str(home.chat_id) == str(pending["chat_id"])
            and str(home.thread_id or "") == str(pending.get("thread_id") or ""))
    if not same:
        targets.append((str(home.chat_id), runner._thread_metadata_for_target(
            platform, home.chat_id, home.thread_id, adapter=adapter)))
    return targets


async def deliver_report(runner, pending: dict, adapter, platform, text: str, metadata, run_home) -> bool:
    """True once one destination confirmed delivery; correlated runs record the outcome."""
    from gateway.run import _non_conversational_metadata
    from hermes_cli.update_gateway_io import record_gateway_delivery

    delivered_to = None
    for chat_id, meta in _report_targets(runner, pending, adapter, platform, metadata):
        try:
            result = await adapter.send(chat_id, text, metadata=_non_conversational_metadata(meta, platform=platform))
        except Exception:
            logger.warning("Update report delivery to %s failed", chat_id, exc_info=True)
            continue
        if getattr(result, "success", None) is True:
            delivered_to = {"chat_id": chat_id, "thread_id": (meta or {}).get("thread_id")}
            break
        logger.warning("Update report delivery to %s was not accepted: %s", chat_id, getattr(result, "error", ""))
    try:
        record_gateway_delivery(run_home, pending, delivered_to)
    except OSError:
        # A confirmed send must not be repeated because its receipt could not be written.
        logger.warning("Could not record update report delivery", exc_info=True)
    return delivered_to is not None
