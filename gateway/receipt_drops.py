"""Tell an adapter that inputs it acknowledged on receipt were dropped, or joined a running turn.

An adapter that marks a busy message as waiting (Discord's hourglass reaction) must clear that
marker when the message is dropped instead of answered: a ``/stop``, a replaced pending slot, a full
queue, a refused or fenced admission. The adapter opts in with ``on_messages_dropped(source,
message_ids)`` (and ``on_messages_joined_turn`` for input a running turn absorbed); every call is
display-only and must never raise into the path that dropped the input.

Events bind their adapter (``bind_adapter``) so a site that holds only the event, such as the
pending-slot replacement in ``merge_pending_message_event``, can report it.
"""
import logging

logger = logging.getLogger(__name__)


def bind_adapter(event, adapter) -> None:
    event._receipt_adapter = adapter


def _call(adapter, hook_name, source, message_ids) -> None:
    hook = getattr(adapter, hook_name, None)
    if not callable(hook) or not message_ids:
        return
    try:
        hook(source, list(message_ids))
    except Exception:
        logger.debug('%s failed', hook_name, exc_info=True)


def notify_dropped(adapter, source, message_ids) -> None:
    _call(adapter, 'on_messages_dropped', source, message_ids)


def drop_event(event) -> None:
    """*event* (or None) will never run: clear the marks of every message it carries."""
    if event is not None:
        _call(getattr(event, '_receipt_adapter', None), 'on_messages_dropped', event.source, event.message_ids())


def join_event(event) -> None:
    """*event* was folded into a turn that is already running: its messages are now being handled."""
    _call(getattr(event, '_receipt_adapter', None), 'on_messages_joined_turn', event.source, event.message_ids())
    adapter = getattr(event, '_receipt_adapter', None)
    hook = getattr(adapter, 'on_event_joined_turn', None)  # adapters that keep a durable per-message claim
    if callable(hook):
        try:
            hook(event)
        except Exception:
            logger.debug('on_event_joined_turn failed', exc_info=True)


def payload_message_ids(payload) -> list[str]:
    """Provider message ids of a committed native admission row's payload (empty for any other)."""
    event = (payload.get('native_text_v1') or {}).get('event') or {}
    ids = event.get('absorbed_message_ids') or [event.get('message_id')]
    return [str(mid) for mid in ids if mid]


def pending_envelope_message_ids(authority) -> set[str]:
    """Provider message ids owned by this profile's unresolved durable envelopes (queued, started or
    unknown), absorbed ids included. A recovery scan must leave them to that envelope: it runs them
    once its FIFO resumes, so a second admission of any carried id would run the input twice."""
    from hermes_state_runtime import pending_native_admissions
    return {mid for row in pending_native_admissions(authority.db) for mid in payload_message_ids(row['payload'])}
