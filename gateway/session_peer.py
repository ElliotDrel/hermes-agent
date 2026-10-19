"""Private peer provenance on an already authorized canonical submission; never a wire grant."""
from hermes_state_runtime import RuntimeStoreError

_FIELDS = frozenset({'chain_id', 'sender_session_id', 'human_id', 'runtime_profile', 'transport_profile', 'platform'})


def commit_origin(origin, authorize):
    if (not callable(authorize) or not isinstance(origin, dict) or set(origin) != _FIELDS | {'provenance'}
            or any(not isinstance(value, str) or not value or len(value) > 256 for key, value in origin.items() if key in _FIELDS)):
        raise RuntimeStoreError('permission_denied')
    provenance = origin['provenance']
    if (not isinstance(provenance, dict) or len(provenance) > 8
            or any(not isinstance(key, str) or (value is not None and (not isinstance(value, str) or len(value) > 4096))
                   for key, value in provenance.items())):
        raise RuntimeStoreError('permission_denied')
    return {**origin, 'provenance': dict(provenance)}


def check_origin(authority, source, origin):
    from gateway.session_identity import canonical_identity
    commit_origin(origin, lambda conn: None)
    from gateway.session_ingress_context import restore_provenance
    restore_provenance(authority.runner, source, origin['provenance'])
    identity = canonical_identity(source, runner=authority.runner)
    if (identity is None or identity.transport_inferred or source.platform.value != origin['platform']
            or source.user_id != origin['human_id'] or source.is_bot
            or identity.runtime_profile != origin['runtime_profile']
            or identity.transport_profile != origin['transport_profile']
            or not authority.runner._is_user_authorized_for_source(source, allow_adapter_delegation=False)):
        raise RuntimeStoreError('permission_denied')


def restore_peer(event, origin):
    """Peer text cannot execute slash controls or impersonate a human/runtime producer."""
    event.allow_gateway_control = False
    event.metadata['turn_author'] = {'id': origin['sender_session_id'], 'name': 'Peer session', 'is_bot': True}
    event._peer_message_origin = dict(origin)


def claim_guard(authority, ref, live, first):
    """Recheck private peer provenance inside the native FIFO claim write, after writer waits."""
    origin = (first or {}).get('payload', {}).get('peer_message_v1')
    if origin is None:
        return None
    def guard(conn):
        import json
        check_origin(authority, live.source, origin)
        physical = authority.physical_target(ref)
        row = conn.execute('SELECT user_id,origin_json,transport_profile FROM sessions WHERE id=?', (physical,)).fetchone()
        source = json.loads(row['origin_json'] or '{}') if row is not None else {}
        if (row is None or row['user_id'] != origin['human_id'] or first['principal_id'] != origin['human_id']
                or source.get('user_id') != origin['human_id'] or source.get('platform') != origin['platform']
                or source.get('chat_id') != live.source.chat_id
                or (source.get('thread_id') or None) != (live.source.thread_id or None)):
            raise RuntimeStoreError('permission_denied')
        if getattr(authority.runner.config, 'multiplex_profiles', False):
            if ((source.get('profile') or origin['transport_profile']) != origin['runtime_profile']
                    or row['transport_profile'] != origin['transport_profile']):
                raise RuntimeStoreError('profile_mismatch')
    return guard
