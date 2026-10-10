"""TUI/Desktop ownership/claims route work to the current parent, leaving human FIFO alone."""
import queue
import threading
from types import SimpleNamespace

from agent.interrupt_control import InterruptControlMixin
from tui_gateway import server


class Parent(InterruptControlMixin):
    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()


def test_owned_events_active_batch_kanban_and_late_retry(monkeypatch):
    session = {'history_lock':threading.RLock(), 'running':True, 'agent':Parent(),
               'session_key':'parent', 'queued_prompt':'human'}
    event = {'type':'async_delegation','session_key':'parent','delegation_id':'a'}
    registry = SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda pid:False)
    completed = []
    monkeypatch.setattr('tools.async_delegation.claim_event_delivery', lambda e,c:'claim')
    monkeypatch.setattr('tools.async_delegation.complete_event_delivery', lambda e,c:completed.append(e))
    monkeypatch.setattr(server, '_emit', lambda *a,**k:None)
    monkeypatch.setattr(server, '_notification_event_belongs_elsewhere', lambda *a:False)
    monkeypatch.setattr(server, '_session_owns_notification_event', lambda *a:True)
    assert server._notif_handle_event('sid',session,event,set(),registry,lambda e:'A done',None)
    completions = [({'type':'completion','session_id':'b'},'B done'), ({'type':'completion','session_id':'c'},'C done')]
    server._notif_dispatch_completions('sid',session,completions,registry,None)
    monkeypatch.setattr(server, '_collect_kanban_notifications', lambda s:['kanban done'])
    server._notif_poll_kanban_scoped('sid',session)
    pending = session['agent']._drain_pending_steer(close_runtime=True)
    assert str(pending).index('A done') < str(pending).index('B done') < str(pending).index('C done') < str(pending).index('kanban done')
    assert session['queued_prompt'] == 'human'
    assert len(completed) == 3
    assert registry.completion_queue.empty()
    assert server._notif_handle_event('sid',session,event,set(),registry,lambda e:'late',[],owned=True) is False
    assert registry.completion_queue.get_nowait() == event
    # Foreign ownership refuses the active path before any claim.
    monkeypatch.setattr(server, '_notification_event_belongs_elsewhere', lambda *a:True)
    deferred = []
    assert server._notif_handle_event('sid',session,event,set(),registry,lambda e:'foreign',deferred)
    assert deferred == [event] and len(completed) == 3


def test_closed_active_fence_refunds_durable_claims_and_preserves_interim():
    import time
    import tools.async_delegation as ad
    ad._reset_for_tests()
    event = {'type': 'async_delegation', 'delegation_id': 'closed-fence', 'session_key': 'parent'}
    ad._persist_dispatch({**event, 'dispatched_at': time.time()})
    ad._persist_completion(event, {'summary': 'done'})
    parent = Parent()
    parent._drain_pending_steer(close_runtime=True)
    session = {'history_lock': threading.RLock(), 'running': True, 'agent': parent}
    try:
        for _ in range(ad._MAX_DELIVERY_ATTEMPTS + 2):
            assert not server._notif_deliver_active(session, 'done', [event], 'test')
        row = ad.get_durable_delegation(event['delegation_id'])
        assert row is not None
        assert row['delivery_state'] == 'pending'
        assert row['delivery_attempts'] == 0
        offered = ('test-home', event['delegation_id'])
        ad._offered.add(offered)
        assert not server._notif_deliver_active(session, 'done', [event], 'test')
        assert offered not in ad._offered
        interim = {**event, 'task_failure_notice': True}
        ad._offered.add(offered)
        assert not server._notif_deliver_active(session, 'early failure', [interim], 'test')
        assert ad.get_durable_delegation(event['delegation_id']) == row
        assert offered in ad._offered
        parent._runtime_notification_closed = False
        assert server._notif_deliver_active(session, 'done', [event], 'test')
        assert ad.get_durable_delegation(event['delegation_id'])['delivery_state'] == 'delivered'
    finally:
        ad._reset_for_tests()
