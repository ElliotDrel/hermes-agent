"""Canonical human/transport and transaction fences for Discord peer messaging."""
import json

from gateway.session_identity import canonical_identity
from gateway.session_ingress_context import route_provenance, restore_provenance
from hermes_state_runtime import RuntimeStoreError, _epoch


def identity(adapter, runner, source):
    found = canonical_identity(source, runner=runner)
    if (source.platform.value != 'discord' or source.is_bot or not source.user_id or found is None
            or found.transport_inferred or runner._delivery_adapter_for(source) is not adapter):
        raise RuntimeStoreError('permission_denied')
    route_provenance(runner, source)  # cached RoutingIdentity cannot freeze current route policy
    if not runner._is_user_authorized_for_source(source, allow_adapter_delegation=False):
        raise RuntimeStoreError('permission_denied')
    return str(source.user_id), found.runtime_profile, found.transport_profile


def route(cap, sid):
    authority = cap.authority
    live = authority.sessions.get(sid)
    if live is None or live.superseded or live.history_mount:
        raise RuntimeStoreError('not_found')
    if identity(cap.adapter, cap.runner, live.source) != cap.identity:
        raise RuntimeStoreError('permission_denied')
    entry = cap.runner.session_store.lookup_by_session_key(live.route)
    if entry is None or authority.logical_owner(entry.session_id) != sid:
        raise RuntimeStoreError('admission_conflict')
    return live, entry


def foreground(cap, caller):
    from agent.delegation_context import is_delegated_child_process_context
    if (not cap.active or caller is None or is_delegated_child_process_context()
            or getattr(caller, '_interrupt_requested', False)
            or not cap.turn._ctx._run_still_current()
            or cap.authority.retiring or cap.authority.retired
            or cap.adapter.config.extra.get('peer_messaging') is not True):
        raise RuntimeStoreError('permission_denied')
    adopted = cap.authority.adopted.get(cap.sid)
    if (adopted is None or adopted[0] != cap.generation or adopted[1] is not caller
            or cap.authority.executing.get(cap.sid) != cap.generation):
        raise RuntimeStoreError('permission_denied')
    live = cap.authority.sessions.get(cap.sid)
    if live is None:
        raise RuntimeStoreError('not_found')
    restore_provenance(cap.runner, live.source, cap.provenance)
    route(cap, cap.sid)


def write_guard(cap, target, source_entry, target_entry, caller, target_provenance):
    """Runs INSIDE the canonical admission transaction, after any awaited writer contention."""
    expected = [(sid, entry.session_key, entry.session_id, entry.origin.to_dict(), provenance)
                for sid, entry, provenance in [(cap.sid, source_entry, cap.provenance),
                                               (target, target_entry, target_provenance)]]

    def guard(conn):
        foreground(cap, caller)
        _epoch(conn, cap.authority.epoch)
        started = conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND status='started' "
                               'AND generation=? AND owner_epoch=?',
                               (cap.sid, cap.generation, cap.authority.epoch)).fetchone()
        if started is None:
            raise RuntimeStoreError('stale_generation')
        for sid, key, physical_id, captured_origin, provenance in expected:
            live, fresh = route(cap, sid)
            restore_provenance(cap.runner, live.source, provenance)
            if fresh.session_id != physical_id or fresh.session_key != key:
                raise RuntimeStoreError('admission_conflict')
            logical = conn.execute('SELECT runtime_generation,ended_at,end_reason FROM sessions WHERE id=?', (sid,)).fetchone()
            if logical is None or (logical['ended_at'] is not None and logical['end_reason'] != 'compression'):
                raise RuntimeStoreError('not_found')
            if sid == cap.sid and logical['runtime_generation'] != cap.generation:
                raise RuntimeStoreError('stale_generation')
            current = conn.execute('SELECT user_id,session_key,origin_json,transport_profile FROM sessions WHERE id=?',
                                   (physical_id,)).fetchone()
            origin = json.loads(current['origin_json'] or '{}') if current is not None else {}
            if (current is None or current['user_id'] != cap.identity[0] or current['session_key'] != key
                    or origin.get('user_id') != cap.identity[0] or origin.get('platform') != 'discord'
                    or origin.get('chat_id') != captured_origin.get('chat_id')
                    or (origin.get('thread_id') or None) != (captured_origin.get('thread_id') or None)):
                raise RuntimeStoreError('admission_conflict')
            if getattr(cap.runner.config, 'multiplex_profiles', False):
                if ((origin.get('profile') or cap.identity[2]) != cap.identity[1]
                        or current['transport_profile'] != cap.identity[2]):
                    raise RuntimeStoreError('profile_mismatch')
    return guard
