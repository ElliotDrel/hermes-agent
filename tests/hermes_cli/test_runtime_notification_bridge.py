"""CLI monitor admission preserves the human queue and existing delivery claims."""
import queue
import threading
from types import SimpleNamespace

from cli import HermesCLI
from agent.interrupt_control import InterruptControlMixin


class Parent(InterruptControlMixin):
    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()


def test_active_cli_drain_parent_and_late_fifo(monkeypatch):
    cli = object.__new__(HermesCLI)
    cli.session_id = 'parent'
    cli.agent = Parent()
    cli._agent_running = True
    cli._pending_input = queue.Queue()
    cli._pending_input.put('human')
    events = [{'type':'async_delegation','session_key':'parent'}, {'type':'watch_match','session_key':'parent'}]
    ready = [(events[0], 'A complete'), (events[1], 'B watch')]
    class Registry:
        def drain_notifications(self, *, session_key, owns_event):
            assert session_key == 'parent'
            assert all(owns_event(event) for event in events)
            result = ready[:]
            ready.clear()
            return result
    monkeypatch.setattr('tools.process_registry.process_registry', Registry())
    claims, completed = [], []
    monkeypatch.setattr('tools.async_delegation.claim_event_delivery', lambda e,c: claims.append(c) or 'claim')
    monkeypatch.setattr('tools.async_delegation.complete_event_delivery', lambda e,c: completed.append(e))
    cli._drain_process_notifications('cli-active')
    assert cli._pending_input.qsize() == 1
    assert cli._pending_input.get_nowait() == 'human'
    pending = cli.agent._drain_pending_steer(close_runtime=True)
    assert str(pending).index('A complete') < str(pending).index('B watch')
    assert completed == events and claims == ['cli-active','cli-active']
    # Exercise the effective monitor consumer, not just the drain method.
    ready.append((events[1], 'monitor delivery'))
    cli.agent._runtime_notification_closed = False
    cli._voice_mode = False
    cli._interrupt_queue = queue.Queue()
    cli._invalidate = lambda **kwargs:None
    alive = iter([True, False])
    worker = SimpleNamespace(is_alive=lambda:next(alive), join=lambda **kwargs:None)
    cli._chat_monitor_agent_thread(SimpleNamespace(), worker)
    assert 'monitor delivery' in cli.agent._drain_pending_steer(close_runtime=True)
    ready.append((events[1], 'late'))
    cli._drain_process_notifications('cli-active')
    assert cli._pending_input.get_nowait() == 'late'
