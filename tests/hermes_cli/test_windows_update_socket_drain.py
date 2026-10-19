"""Native Windows pipe pause preserves drain ownership and crash recovery debt."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd_windows as windows
from hermes_cli import update_pause_record as records
from tests.waiting import wait_until


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("infinite,refused", [(False, False), (True, False), (False, True)])
def test_native_pause_keeps_after_turn_budget_and_durable_acceptance(tmp_path, infinite, refused):
    home = tmp_path / "gateway"
    home.mkdir()
    ready = home / "ready"
    if refused:
        (home / ".gateway-planned-stop.json.accepted").mkdir()
    script = tmp_path / "pause_peer.py"
    script.write_text("""
import asyncio, json, os
from pathlib import Path
from types import SimpleNamespace
from gateway.run_bootstrap import _start_gateway_start_control_socket
home = Path(os.environ['HERMES_HOME'])
def restart(**kwargs):
    (home / 'accepted').write_text(json.dumps({'marker': (home / '.gateway-planned-stop.json').exists(), 'options': kwargs}), encoding='utf-8')
    return True
async def main():
    runner = SimpleNamespace(session_runtime_descriptor={}, session_ticket_store=None,
        _restart_drain_timeout=3, _restart_after_turn_timeout=float(os.environ["TEST_AFTER_TURN"]), _cron_drain_timeout=17,
        request_restart=restart)
    server = await _start_gateway_start_control_socket(runner)
    assert server is not None
    (home / 'ready').write_text(str(os.getpid()), encoding='utf-8')
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
asyncio.run(main())
""", encoding="utf-8")
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
           "HERMES_RUNTIME_DIR": str(tmp_path / "runtime"), "TEST_AFTER_TURN": "inf" if infinite else "41"}
    child = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_until(lambda: ready.exists() or child.poll() is not None, timeout=30)
        assert ready.exists(), child.communicate(timeout=5)
        pid = int(ready.read_text(encoding="utf-8"))
        marker = home / ".gateway-planned-stop.json"
        token = {"profiles": {"default": pid}, "identities": {str(pid): records.identity(pid)["ct"]},
                 "stop_requested": [str(pid)], "stop_sent": [], "stopper_pid": os.getpid(),
                 "stop_markers": {str(pid): str(marker)}}
        # The producer can die immediately after receiving the ACK, before its checkpoint.
        def lost_checkpoint(pid):
            raise RuntimeError("updater checkpoint lost")
        with pytest.raises(RuntimeError, match="checkpoint lost"):
            windows._request_socket_pauses([pid], {pid: SimpleNamespace(profile="default", path=home)},
                                           set(), on_request=lost_checkpoint)
        if refused:
            assert marker.exists()
            assert not (home / "accepted").exists(), "receipt refusal scheduled a restart"
            return
        assert not marker.exists(), "marker watcher raced ahead of the graceful socket request"
        accepted = json.loads((home / "accepted").read_text(encoding="utf-8"))
        assert accepted == {"marker": False, "options": {"detached": False, "via_service": True}}
        assert records.drop_never_stopped(token)["profiles"] == {"default": pid}
        from gateway.control_socket import pause_gateway_for_update
        from gateway.restart import resolve_restart_exit_wait_budget
        ack = pause_gateway_for_update(home, timeout=6)
        assert ack["pausing"] is True
        assert ack["drain_timeout"] == resolve_restart_exit_wait_budget(3, float("inf") if infinite else 41, 17)
        assert windows._gateway_drain_timeout([ack]) >= ack["drain_timeout"]
    finally:
        import psutil
        for worker in psutil.Process(child.pid).children(recursive=True):
            worker.kill()
        child.kill()
        child.communicate(timeout=10)


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("old_ack", [False, True])
def test_unavailable_socket_preserves_legacy_marker_and_checkpoint(tmp_path, monkeypatch, old_ack):
    calls = []
    if old_ack:
        def legacy_pause(*args, **kwargs):
            assert (tmp_path / ".gateway-planned-stop.json").exists()
            assert calls == [os.getpid()]
            return {"pausing": True, "drain_timeout": 30}
        monkeypatch.setattr("gateway.control_socket.pause_gateway_for_update", legacy_pause)
    windows._request_socket_pauses([os.getpid()],
        {os.getpid(): SimpleNamespace(profile="default", path=tmp_path)}, set(), on_request=calls.append)
    marker = json.loads((tmp_path / ".gateway-planned-stop.json").read_text(encoding="utf-8"))
    assert marker["target_pid"] == os.getpid()
    assert marker["stopper_pid"] == os.getpid()
    assert calls == [os.getpid()]

