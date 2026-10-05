"""Windows updates honor ordinary restart waits and report their active work."""

import json
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, update_cmd_windows as windows
from hermes_cli import update_cmd_drain_report as reports
from gateway import status


@pytest.mark.parametrize("ack", [{"pausing": True}, {"already_stopping": True}, None, RuntimeError("unavailable")])
def test_socket_pause_uses_stop_marker_only_as_fallback(tmp_path, monkeypatch, ack):
    from gateway import control_socket

    calls = []

    def pause(home):
        calls.append("socket")
        if isinstance(ack, Exception):
            raise ack
        return ack

    monkeypatch.setattr(control_socket, "pause_gateway_for_update", pause)
    monkeypatch.setattr(windows, "_write_update_planned_stop_marker", lambda *args: calls.append("marker"))
    result = windows._request_socket_pauses(
        [123], {123: SimpleNamespace(profile="default", path=tmp_path)}, set(),
    )
    accepted = isinstance(ack, dict)
    assert calls == (["socket"] if accepted else ["socket", "marker"])
    assert result == ({"default": 123}, [123], [ack] if accepted else [])


def test_update_wait_covers_normal_restart_after_turn_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)
    monkeypatch.delenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", raising=False)
    (tmp_path / "config.yaml").write_text(
        "agent:\n  restart_drain_timeout: 0\n  restart_after_turn_timeout: 120\n",
        encoding="utf-8",
    )
    normal_budget = gateway._get_restart_exit_wait_budget()
    assert normal_budget > 120
    assert windows._gateway_drain_timeout([{"drain_timeout": 0}]) == normal_budget
    assert windows._gateway_drain_timeout([]) == normal_budget
    assert windows._gateway_drain_timeout([{"drain_timeout": normal_budget + 20}]) > normal_budget


def test_windows_wait_reports_owned_home_counts_and_returns_survivors(tmp_path, monkeypatch, capsys):
    homes = [tmp_path / "a", tmp_path / "b"]
    for home, count in zip(homes, (2, 1)):
        home.mkdir()
        (home / "gateway_state.json").write_text(json.dumps({
            "active_work": [{"kind": "api"}] * count,
        }), encoding="utf-8")
    now = [0.0]
    monkeypatch.setattr(reports.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(status, "_pid_exists", lambda pid: pid == 11)

    def poll(condition, timeout, interval):
        now[0] += 31
        assert not condition()
        return False

    monkeypatch.setattr(windows, "_poll_until", poll)
    for home, count in ((homes[0], 2), (homes[1], 1), (homes[0], 2)):
        assert windows._wait_for_windows_update_gateway_exit(
            [11, 22], timeout=120, profile_homes={11: home, 22: homes[1]},
        ) == {11}
        output = capsys.readouterr().out
        assert f"waiting on {count} active work unit(s)" in output
        assert output.count("still draining") == 1
