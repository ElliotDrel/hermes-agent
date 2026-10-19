"""Active-parent runtime notices: a background result for a session whose turn is still running.

A process completion or async-delegation result admitted while its session executes is handed to
that running turn as a typed nonhuman notice (``agent.runtime_notices``) instead of waiting as the
next turn. It reuses the queued-steer escrow (``session_queued_controls``): the automation row goes
queued -> terminal ``steering`` bound to the running claim in one owner write, gated inside that write
on the row's own canonical provenance (never the caller's payload) and on its FIFO place (an earlier
queued input, a person's message included, keeps it queued behind it). An earlier unresolved transfer
also holds that place: one active-parent transfer per executing claim, with later results retaining their
native queued turns. It settles with that claim:
``steered`` once the claim completes with the notice drained, ``interrupted`` if drained under any
other ending (never replayed), queued again in its own place when no boundary of the turn took it.

The agent receives it through ``notify_runtime``, never ``steer``: the turn is not marked
user-intervened and the notice authorizes nothing. An idle session, a turn with no notice seam (a
managed worker, a Codex app-server turn) or any refusal leaves the row queued for its native next
turn. The producer's ACK is unchanged: it means committed input, whichever turn reads it.
"""
import json
import logging
from functools import partial

from hermes_state_runtime import (
    STEERING, RuntimeStoreError, _row, get_session_admission, revert_steer_input, steer_session_input,
)

logger = logging.getLogger(__name__)

# Producer identities whose result a running turn may read (``_completion_delivery_identity``). Liveness
# ticks, watch matches and route wakes are turns of their own.
_NOTICE_PRODUCERS = frozenset({'completion', 'async_delegation'})


def _descriptor(payload):
    if set(payload) == {'text', 'local_automation_v1'}:
        return payload['local_automation_v1']
    if set(payload) == {'text', 'native_text_v1'}:
        return payload['native_text_v1'].get('automation')
    return None


def eligible(row):
    """The stored row is a trusted producer's background result: an automation principal, its own
    identity, a result (not a muted diagnostic) and no liveness tick."""
    payload = row['payload']
    descriptor = _descriptor(payload)
    if (not row['principal_id'].startswith('automation:') or not isinstance(descriptor, dict)
            or descriptor.get('identity') != row['request_id']
            or not isinstance(payload.get('text'), str) or not payload['text'].strip()
            or descriptor.get('notification_category') == 'diagnostic'
            or 'heartbeat' in descriptor or 'heartbeat_tick' in descriptor):
        return False
    try:
        identity = json.loads(row['request_id'])
    except ValueError:
        return False
    return isinstance(identity, list) and bool(identity) and identity[0] in _NOTICE_PRODUCERS


def _gate(conn, row):
    """Inside the transfer's own write: provenance from the row, and the oldest-queued FIFO place."""
    if not eligible(_row(row)):
        raise RuntimeStoreError('unsupported_control', field='admission_id', detail='not a background result')
    if conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND seq<? "
                    "AND (status IN ('queued','unknown') OR (status='terminal' AND outcome=?)) LIMIT 1",
                    (row['target_session_id'], row['seq'], STEERING)).fetchone() is not None:
        raise RuntimeStoreError('steer_unavailable', field='admission_id', detail='an earlier input is queued or still in transfer')


def _notice_agent(authority, session_id, generation):
    """The agent the running claim adopted while that claim still executes, if it takes notices."""
    adopted_generation, agent = authority.adopted.get(session_id, (None, None))
    if (adopted_generation != generation or authority.executing.get(session_id) != generation
            or not callable(getattr(agent, 'notify_runtime', None))
            or getattr(agent, 'api_mode', None) == 'codex_app_server'):
        return None
    return agent


def _deliver(authority, session_id, generation, agent, admission_id, text):
    """Hand the committed notice to the claim's agent; the caller holds the stream lock."""
    entry = authority.steer_transfers.get(session_id)
    if (entry is None or entry[0] != generation or admission_id not in entry[1]
            or _notice_agent(authority, session_id, generation) is not agent):
        return False
    if not entry[1][admission_id]:
        entry[1][admission_id] = bool(agent.notify_runtime(text, ticket=admission_id))
    return entry[1][admission_id]


def _check_route(authority, ref, live, row):
    """The checks the drain runs before claiming this row: its owner and route are still this session's."""
    if 'local_automation_v1' in row['payload']:
        from gateway.session_automation import check_local_automation
        check_local_automation(authority, ref, row)
        return
    from gateway.session_automation import check_automation_route
    check_automation_route(authority.runner, row['payload'], authority.physical_target(ref), live.source,
                           authority.runner._adapter_for_source(live.source))


async def offer_runtime_notice(authority, ref, admission_id):
    """After an automation admission committed: True once its running turn holds it as a notice.
    False leaves the row queued (its native next turn), except a transfer that may have committed
    under an uncertain failure, which stays fenced to the claim and settles with it."""
    from gateway.session_queued_controls import _register, _unregister
    from gateway.session_runtime_workers import tracked_write
    handle = authority._handle(ref)
    row = get_session_admission(authority.db, admission_id=admission_id)
    if handle.execution_state != 'running' or row is None or row['status'] != 'queued' or not eligible(row):
        return False
    generation, live, session_id = handle.execution_generation, authority.sessions[ref.session_id], ref.session_id
    try:
        _check_route(authority, ref, live, row)
    except RuntimeStoreError:
        return False  # the drain refuses it on its own terms
    with live.event_stream.lock:
        agent = _notice_agent(authority, session_id, generation)
        if agent is None:
            return False
        _register(authority, session_id, generation, admission_id)
    try:
        committed, fresh = await tracked_write(authority, partial(
            steer_session_input, authority.db, epoch=authority.epoch, admission_id=admission_id,
            generation=generation, _guard=_gate))
    except RuntimeStoreError:
        with live.event_stream.lock:
            _unregister(authority, session_id, generation, admission_id)
        return False
    if not fresh:
        return False  # another offer of the same admission committed it and delivers it
    with live.event_stream.lock:
        delivered = _deliver(authority, session_id, generation, agent, admission_id, committed['payload']['text'])
    if not delivered:
        # The claim ended, was stopped or sealed first: nobody read it, so it is queued again in its place.
        await tracked_write(authority, partial(revert_steer_input, authority.db, epoch=authority.epoch,
                                               admission_id=admission_id, generation=generation))
        with live.event_stream.lock:
            _unregister(authority, session_id, generation, admission_id)
        authority._publish_pending(ref)
        authority._schedule(ref)
        return False
    authority._publish_pending(ref)
    return True


async def offer_after_admission(authority, receipt):
    """A background result's admission tail: the input is already committed, so a failure here never
    reaches the producer (the row stays queued, or fenced to the claim when uncertain)."""
    try:
        await offer_runtime_notice(authority, receipt.ref, receipt.admission_id)
    except Exception:
        logger.exception('Runtime notice offer for admission %s failed', receipt.admission_id)
    return receipt
