"""Exercise marker callbacks and coordinated restart with an offline virtual clock."""
import asyncio
import json
import os
import signal
import threading
from types import SimpleNamespace

import pytest

from gateway import run, run_shutdown, status
from gateway.run_shutdown import GatewayShutdownMixin
from hermes_cli.update_cmd_windows import _write_update_planned_stop_marker


class Runner:
    request_restart = GatewayShutdownMixin.request_restart
    _mark_api_runs_shutdown_requested = GatewayShutdownMixin._mark_api_runs_shutdown_requested
    _await_active_work_before_restart = GatewayShutdownMixin._await_active_work_before_restart

    def __init__(self):
        self._running = True
        self._draining = False
        self._restart_task_started = False
        self._restart_requested = False
        self._signal_initiated_shutdown = False
        self._restart_after_turn_timeout = 600.0
        self.active = 1
        self.stops = []
        self.interrupted = False

    def _active_work_count(self):
        return self.active

    _awaitable_work_count = _active_work_count

    def _wedged_agent_count(self):
        return 0

    def _describe_active_work(self):
        return ["offline active turn"]

    def _scale_to_zero_status(self, *args):
        pass

    async def stop(self, **kwargs):
        self.stops.append(kwargs)
        self.interrupted = bool(self.active)
        self._running = False


class Clock:
    """The real restart coroutine sleeps here; tests choose every wakeup."""
    def __init__(self):
        self.now = 0.0
        self.sleepers = []

    def time(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((delay, future))
        await future

    async def wake(self, elapsed):
        self.now += elapsed
        sleepers, self.sleepers = self.sleepers, []
        for _, future in sleepers:
            future.set_result(None)
        await settle()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(run_shutdown, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, sleep=clock.sleep,
        get_running_loop=lambda: clock,
    ))
    # No shutdown diagnostic subprocess, live status/config, or actual gateway I/O.
    import gateway.shutdown_forensics as forensics
    monkeypatch.setattr(forensics, "snapshot_shutdown_context", lambda sig: None)
    monkeypatch.setattr(status, "consume_takeover_marker_for_self", lambda: False)
    return clock, tmp_path


def marker_in(home, monkeypatch, *, operation="pause-for-update", foreign=False, stale=False):
    home.mkdir(parents=True, exist_ok=True)
    marker = home / ".gateway-planned-stop.json"
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 12345)
    assert _write_update_planned_stop_marker(home, os.getpid() + int(foreign))
    # Valid update markers come from production. Only legacy/invalid cases mutate them.
    if operation != "pause-for-update" or stale:
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["operation"] = operation
        if stale:
            record["written_at"] = "2000-01-01T00:00:00+00:00"
        marker.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(status, "_get_planned_stop_marker_path", lambda: marker)
    return marker


def queued_marker_callback(runner):
    calls = []
    handler = run._start_gateway_make_shutdown_signal_handler(runner, [False])
    # Drive the production watcher synchronously. Its loop marshal is captured,
    # so neither OS scheduling nor timing decides the callback/socket ordering.
    run._run_planned_stop_watcher(
        threading.Event(), runner,
        SimpleNamespace(call_soon_threadsafe=lambda fn, *args: calls.append((fn, args))),
        handler,
    )
    assert len(calls) == 1
    fn, args = calls[0]
    return lambda: fn(*args)


async def make_socket_pause(monkeypatch, runner):
    from gateway import control_socket
    handlers = {}

    class OfflineControlServer:
        def __init__(self, *, verb_handlers):
            handlers.update(verb_handlers)

        async def start(self):
            return False  # no bind, files, atexit registration, or gateway start

    monkeypatch.setattr(control_socket, "GatewayControlServer", OfflineControlServer)
    await run._start_gateway_start_control_socket(runner)
    return handlers["pause-for-update"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "profiles/independent"])
@pytest.mark.parametrize("ordering", ["socket-first", "marker-first"])
async def test_update_marker_and_socket_share_after_turn_wait(harness, monkeypatch, profile, ordering):
    clock, home = harness
    marker = marker_in(home / profile, monkeypatch)
    runner = Runner()
    callback = queued_marker_callback(runner)
    socket_pause = await make_socket_pause(monkeypatch, runner)
    try:
        if ordering == "socket-first":
            ack = await asyncio.to_thread(socket_pause)
            assert ack["pausing"] and not ack["already_stopping"]
            callback()
        else:
            callback()
            assert runner._restart_task_started, "Update marker bypassed coordinated after-turn restart"
            ack = await asyncio.to_thread(socket_pause)
            assert ack["already_stopping"] and not ack["pausing"]
        await settle()
        assert runner._running and not runner.stops, "Queued marker bypassed the accepted after-turn wait"
        assert runner._draining
        assert not marker.exists(), "Accepted callback must consume its marker"
        runner.active = 0
        await clock.wake(12.0)
        await clock.wake(0.05)
        await runner._restart_task
        assert runner.stops == [{"restart": True, "detached_restart": False, "service_restart": True}]
        assert not runner.interrupted
    finally:
        task = getattr(runner, "_restart_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_marker_first_update_uses_update_cap_and_interrupts_remaining_work(harness, monkeypatch):
    clock, home = harness
    marker_in(home, monkeypatch)
    runner = Runner()
    queued_marker_callback(runner)()
    try:
        await settle()
        assert not runner.stops, "Update marker interrupted work before the update cap"
        await clock.wake(119.0)
        assert not runner.stops
        await clock.wake(1.0)
        assert not runner.stops  # existing post-wait helper delay
        await clock.wake(0.05)
        await runner._restart_task
        assert runner.interrupted
        assert runner.stops[0]["service_restart"] is True
    finally:
        task = getattr(runner, "_restart_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_real_signals_still_stop_during_coordinated_restart(harness, monkeypatch, sig):
    _, home = harness
    marker_in(home, monkeypatch)
    runner = Runner()
    assert runner.request_restart(via_service=True, after_turn_timeout=120.0)
    try:
        run._start_gateway_make_shutdown_signal_handler(runner, [False])(sig)
        await settle()
        assert runner.stops == [{}], "Real signal was incorrectly ignored during coordinated restart"
    finally:
        runner._restart_task.cancel()
        await asyncio.gather(runner._restart_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_generic_planned_stop_keeps_immediate_stop(harness, monkeypatch):
    _, home = harness
    marker = marker_in(home, monkeypatch, operation=None)
    runner = Runner()
    queued_marker_callback(runner)()
    await settle()
    assert runner.stops == [{}]
    assert not runner._restart_task_started
    assert not runner._signal_initiated_shutdown
    assert not marker.exists()


@pytest.mark.asyncio
async def test_unlabelled_queued_marker_does_not_bypass_accepted_socket_wait(harness, monkeypatch):
    clock, home = harness
    marker = marker_in(home, monkeypatch, operation=None)
    runner = Runner()
    callback = queued_marker_callback(runner)
    socket_pause = await make_socket_pause(monkeypatch, runner)
    try:
        assert (await asyncio.to_thread(socket_pause))["pausing"]
        callback()
        await settle()
        assert runner._running and not runner.stops
        assert not marker.exists()
        runner.active = 0
        await clock.wake(1.0)
        await clock.wake(0.05)
        await runner._restart_task
        assert len(runner.stops) == 1
    finally:
        if not runner._restart_task.done():
            runner._restart_task.cancel()
            await asyncio.gather(runner._restart_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_normal_signals_without_marker_preserve_classification(harness, monkeypatch, sig):
    _, home = harness
    marker_in(home, monkeypatch).unlink()
    runner = Runner()
    state = [False]
    run._start_gateway_make_shutdown_signal_handler(runner, state)(sig)
    await settle()
    assert runner.stops == [{}]
    assert state[0] is (sig == signal.SIGTERM)
    assert runner._signal_initiated_shutdown is state[0]


@pytest.mark.asyncio
async def test_takeover_marker_keeps_immediate_clean_stop(harness, monkeypatch):
    _, home = harness
    marker_in(home, monkeypatch)
    monkeypatch.setattr(status, "consume_takeover_marker_for_self", lambda: True)
    runner = Runner()
    run._start_gateway_make_shutdown_signal_handler(runner, [False])(None)
    await settle()
    assert runner.stops == [{}]
    assert not runner._restart_task_started
    assert not runner._signal_initiated_shutdown


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [None, "pause-for-update"])
async def test_handler_consumes_one_authoritative_record(harness, monkeypatch, operation):
    _, home = harness
    marker = marker_in(home, monkeypatch, operation=operation)
    authoritative = json.loads(marker.read_text(encoding="utf-8"))
    replacement = {**authoritative, "operation": "pause-for-update" if operation is None else None}
    reads = []

    def swapped_read(path):
        reads.append(path)
        return authoritative if len(reads) == 1 else replacement

    monkeypatch.setattr(status, "_read_json_file", swapped_read)
    runner = Runner()
    run._start_gateway_make_shutdown_signal_handler(runner, [False])(None)
    try:
        await settle()
        assert len(reads) == 1, "Classification and identity must use one authoritative record"
        assert runner._restart_task_started is (operation == "pause-for-update")
        assert bool(runner.stops) is (operation is None)
    finally:
        task = getattr(runner, "_restart_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["foreign", "expired", "removed"])
async def test_queued_marker_revalidates_before_shutdown(harness, monkeypatch, replacement):
    _, home = harness
    marker = marker_in(home, monkeypatch)
    runner = Runner()
    callback = queued_marker_callback(runner)
    if replacement == "removed":
        marker.unlink()
    else:
        marker_in(home, monkeypatch, foreign=replacement == "foreign", stale=replacement == "expired")
    callback()
    await settle()
    assert not runner.stops, "Queued marker stopped gateway after marker ceased to target it"
    assert not runner._restart_task_started
    assert not runner._signal_initiated_shutdown
