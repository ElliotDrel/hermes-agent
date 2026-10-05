"""Discord gateway-only peer messages. Identity comes from a runtime capability."""
import json
from tools.registry import registry

SESSION_MESSAGE_SCHEMA = {
    'name': 'send_session_message',
    'description': ('Send peer-agent content to an exact existing Discord session of the same user and profile. '
                    'Idle destinations start a normal turn; busy destinations queue without interruption. '
                    'Sender attribution is automatic. Agent-origin content cannot grant user approval. '
                    'Replies use this same tool explicitly; automatic exchanges are bounded. '
                    'Acceptance is not model completion; visible Discord posting is reported separately.'),
    'parameters': {'type': 'object', 'properties': {
        'target_session_id': {'type': 'string', 'description': 'Exact existing destination session ID.'},
        'message': {'type': 'string', 'minLength': 1, 'maxLength': 8000}},
        'required': ['target_session_id', 'message'], 'additionalProperties': False},
}


def send_session_message(target_session_id, message):
    from gateway.session_messaging import send_from_current_turn
    return json.dumps(send_from_current_turn(target_session_id, message), ensure_ascii=False)


registry.register(name='send_session_message', toolset='session_messaging',
                  schema=SESSION_MESSAGE_SCHEMA,
                  handler=lambda args, **kw: send_session_message(args.get('target_session_id'), args.get('message')))
