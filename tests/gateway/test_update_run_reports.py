"""Each gateway ``/update`` invocation owns its report: route, output, prompt answers and receipt.

Two invocations used to share one set of home-root marker files, so a second ``/update``
redirected the first one's report; a transport's unsuccessful ``SendResult`` consumed the notice;
and a watcher timeout invented exit 124 and erased the pending report.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import HomeChannel, Platform
from gateway.platforms.base import SendResult
from hermes_cli.update_gateway_io import gateway_update_home
from tests.gateway.test_update_streaming import _make_event, _make_runner
from tests.waiting import async_wait_until


class _Adapter:
    """Transport double: records sends and answers them from a scripted result list."""

    def __init__(self, platform, results=(), home_chat="555"):
        self.config = SimpleNamespace(home_channel=HomeChannel(platform, home_chat, "home"))
        self.results = list(results)
        self.sent = []
        self.threads = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.threads.append((parent_chat_id, name))
        return "777"

    async def send(self, chat_id, content, metadata=None, **_):
        self.sent.append((chat_id, metadata or {}))
        return self.results.pop(0) if self.results else SendResult(success=True, message_id="m")


class _ThreadReportingAdapter(_Adapter):
    update_reports_in_thread = True  # Discord's opt-in


@pytest.fixture
def home(tmp_path, monkeypatch):
    from gateway import run
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(run, "_hermes_home", home)
    monkeypatch.setattr(run, "_resolve_hermes_bin", lambda: ["hermes"])
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _: "git")
    return home


def _runner_with(adapter, monkeypatch, launched):
    from gateway import slash_commands
    runner = _make_runner()
    runner._UPDATE_ALLOWED_PLATFORMS = runner._UPDATE_ALLOWED_PLATFORMS | {Platform.DISCORD}
    runner._schedule_update_notification_watch = lambda: None
    runner._authorization_adapter = lambda *_: adapter

    def spawn(command, output, exit_code, *, run_id):
        launched.append((output.parent, run_id))
    monkeypatch.setattr(slash_commands, "_spawn_detached_update", spawn)
    return runner


def _finish(run_home, code="0"):
    (run_home / ".update_output.txt").write_text(f"log of {run_home.name}", encoding="utf-8")
    (run_home / ".update_exit_code").write_text(code, encoding="utf-8")


def _native_receipt(home, correlation_id):
    """A terminal native update receipt as the updater leaves it (launcher correlation recorded)."""
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"update_20261019_120000_1_{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({"update_id": "u1", "correlation_id": correlation_id, "outcome": "success",
                                "finished_at": "2026-10-19T12:00:00+00:00"}), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_two_invocations_keep_their_own_reports_through_failed_delivery_and_fallback(home, monkeypatch):
    adapter = _ThreadReportingAdapter(Platform.DISCORD, [SendResult(success=False), SendResult(success=False)])
    launched = []
    runner = _runner_with(adapter, monkeypatch, launched)
    channel = _make_event(platform=Platform.DISCORD, chat_id="100")
    channel.source.chat_type = "group"
    await runner._handle_update_command(channel)
    in_thread = _make_event(platform=Platform.DISCORD, chat_id="200")
    in_thread.source.thread_id = "200"
    await runner._handle_update_command(in_thread)

    (a_home, a_id), (b_home, b_id) = launched
    assert a_id != b_id and a_home == gateway_update_home(home, a_id)
    assert adapter.threads == [("100", f"/update {a_id[:8]}")]  # the threaded request reuses its thread
    a, b = runner._update_paths(a_home), runner._update_paths(b_home)
    a_pending = json.loads(a.pending.read_text(encoding="utf-8"))
    assert (a_pending["chat_id"], a_pending["thread_id"]) == ("777", "777")
    assert json.loads(b.pending.read_text(encoding="utf-8"))["thread_id"] == "200"
    _finish(a_home), _finish(b_home, "1")
    receipt_path = _native_receipt(home, a_id)

    # Both destinations refuse: nothing is consumed, and the attempt is on record.
    assert await runner._send_update_notification(a) is False
    assert a.pending.exists() and a.exit_code.exists() and b.pending.exists()
    assert [chat for chat, _ in adapter.sent] == ["777", "555"]
    attempt = json.loads((a_home / "delivery.json").read_text(encoding="utf-8"))
    assert attempt["correlation_id"] == a_id and attempt["delivered"] is False

    # Retry: the requester's thread still refuses, the profile's configured home accepts.
    adapter.results = [SendResult(success=False)]
    adapter.sent.clear()
    assert await runner._send_update_notification(a) is True
    assert [chat for chat, _ in adapter.sent] == ["777", "555"]
    assert adapter.sent[0][1]["thread_id"] == "777"
    assert not a.any_pending() and b.any_pending()
    assert a.output.read_text(encoding="utf-8") == f"log of {a_id}"  # kept for diagnosis
    native = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert native["gateway_report"]["delivered"] is True
    assert native["gateway_report"]["target"]["chat_id"] == "555"

    adapter.sent.clear()
    assert await runner._send_update_notification(b) is True
    assert adapter.sent == [("200", adapter.sent[0][1])] and adapter.sent[0][1]["thread_id"] == "200"


@pytest.mark.asyncio
async def test_adapter_without_thread_reports_keeps_the_invoking_channel(home, monkeypatch):
    adapter = _Adapter(Platform.TELEGRAM)  # can create handoff threads, but has not opted in
    launched = []
    runner = _runner_with(adapter, monkeypatch, launched)
    event = _make_event(platform=Platform.TELEGRAM, chat_id="-100")
    event.source.chat_type = "group"
    await runner._handle_update_command(event)
    (run_home, _), = launched
    assert adapter.threads == []
    pending = json.loads((run_home / ".update_pending.json").read_text(encoding="utf-8"))
    assert pending["chat_id"] == "-100" and "thread_id" not in pending


@pytest.mark.asyncio
async def test_reporting_timeout_keeps_the_invocation_and_invents_no_exit(home):
    run_home = gateway_update_home(home, uuid.uuid4().hex)
    run_home.mkdir(parents=True)
    runner = _make_runner()
    paths = runner._update_paths(run_home)
    paths.pending.write_text(json.dumps({"run_id": run_home.name, "platform": "discord", "chat_id": "1"}),
                             encoding="utf-8")
    runner._resolve_update_target = lambda _: None
    await runner._watch_update_progress(timeout=0, paths=paths)
    assert paths.any_pending() and not paths.exit_code.exists()
    assert runner._pending_update_paths() == [paths]


@pytest.mark.asyncio
async def test_prompt_answers_and_exit_code_stay_with_the_asking_invocation(home, monkeypatch):
    from hermes_cli import update_cmd
    from hermes_cli.update_cmd_fleet import _write_gateway_update_exit_code
    run_id = uuid.uuid4().hex
    run_home = gateway_update_home(home, run_id)
    run_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", run_id)
    runner = _make_runner()
    paths = runner._update_paths(run_home)

    answer = asyncio.create_task(asyncio.to_thread(update_cmd._gateway_prompt, "Continue?", "n", 10))
    await async_wait_until(paths.prompt.exists, what="the child's prompt in its own directory")
    assert runner._hm_write_update_response("y", run_id) is None
    assert await answer == "y"
    _write_gateway_update_exit_code(True)
    assert paths.exit_code.read_text(encoding="utf-8") == "0"
    paths.exit_code.unlink()
    from hermes_cli.update_completion import _publish_gateway_success  # the stdlib-only completion child
    _publish_gateway_success({"gateway_mode": True, "home": str(home)})
    assert paths.exit_code.read_text(encoding="utf-8") == "0"
    assert not (home / ".update_response").exists() and not (home / ".update_exit_code").exists()

    # A Desktop-launched update carries a correlation id too; with no gateway directory for it,
    # its files stay where its own launcher reads them.
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", uuid.uuid4().hex)
    assert gateway_update_home(home) == home


@pytest.mark.asyncio
async def test_discord_prompt_button_answers_its_invocation(home):
    from plugins.platforms.discord.adapter import UpdatePromptView
    run_id = uuid.uuid4().hex
    run_home = gateway_update_home(home, run_id)
    run_home.mkdir(parents=True)
    view = UpdatePromptView(session_key="s", allowed_user_ids={"99"}, run_id=run_id)
    view._gate = AsyncMock(return_value=True)
    view._finalize_embed = AsyncMock()
    await view._respond(SimpleNamespace(user=SimpleNamespace(display_name="requester")), "n", None,
                        "platform.discord.prompt.negate")
    assert (run_home / ".update_response").read_text(encoding="utf-8") == "n"
    assert not (home / ".update_response").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, object(), SendResult(success=False)])
async def test_unconfirmed_completion_retains_the_pending_invocation(home, monkeypatch, result):
    adapter = _Adapter(Platform.TELEGRAM, [result, result])
    launched = []
    runner = _runner_with(adapter, monkeypatch, launched)
    await runner._handle_update_command(_make_event())
    (run_home, _), = launched
    _finish(run_home)
    paths = runner._update_paths(run_home)
    assert await runner._send_update_notification(paths) is False
    assert paths.pending.exists() and paths.exit_code.exists()
    adapter.results = [SendResult(success=True)]
    assert await runner._send_update_notification(paths) is True
    assert not paths.any_pending()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, object(), SendResult(success=False)])
async def test_unconfirmed_prompt_does_not_capture_answers(home, result):
    class PromptAdapter(_Adapter):
        async def send_update_prompt(self, **kwargs):
            return result
    adapter = PromptAdapter(Platform.TELEGRAM, [result])
    runner = _make_runner()
    target = runner._UpdateTarget(adapter, "123", "session", {}, Platform.TELEGRAM, "run")
    await runner._forward_update_prompt(target, "Continue?", "n")
    state = runner._peek_session_state("session")
    assert state is None or not state.persistent.update_prompt_pending
    adapter.results = [SendResult(success=True)]
    await runner._forward_update_prompt(target, "Continue?", "n")
    assert runner._peek_session_state("session").persistent.update_prompt_run_id == "run"


@pytest.mark.asyncio
async def test_routed_update_uses_receiving_bot_after_restart(home, monkeypatch):
    import weakref
    from gateway.config import GatewayConfig
    from gateway.session_identity import RoutingIdentity
    receiver = _ThreadReportingAdapter(Platform.DISCORD)
    other = _ThreadReportingAdapter(Platform.DISCORD, home_chat="other-home")
    launched = []
    runner = _runner_with(receiver, monkeypatch, launched)
    # Use the real resolver: the receiving bot belongs to A, the execution profile B has its own bot.
    del runner._authorization_adapter
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner.adapters = {Platform.DISCORD: receiver}
    runner._profile_adapters = {"A": {Platform.DISCORD: receiver}, "B": {Platform.DISCORD: other}}
    source = _make_event(platform=Platform.DISCORD, chat_id="100")
    source.source.chat_type = "group"
    source.source.profile = "B"
    source.source._identity = RoutingIdentity("A", "B", home / "A", home / "B", transport=weakref.ref(receiver))
    source.source._transport_adapter_ref = weakref.ref(receiver)
    await runner._handle_update_command(source)
    (run_home, _), = launched
    paths = runner._update_paths(run_home)
    pending = json.loads(paths.pending.read_text(encoding="utf-8"))
    assert receiver.threads and not other.threads
    assert pending["profile"] == "B" and pending["transport_profile"] == "A"
    assert pending["session_key"].startswith("agent:B:")
    # A new runner has no live event or weakref; only the durable marker can name its bot.
    restarted = _make_runner()
    restarted.config = runner.config
    restarted._primary_profile_name = "default"
    restarted.adapters = {Platform.DISCORD: other}
    restarted._profile_adapters = runner._profile_adapters
    assert restarted._resolve_update_target(paths).adapter is receiver
    _finish(run_home)
    restarted._profile_adapters = {"A": {}, "B": {Platform.DISCORD: other}}
    assert restarted._resolve_update_target(paths) is None
    assert await restarted._send_update_notification(paths) is False
    assert paths.pending.exists() and not other.sent
    restarted._profile_adapters = runner._profile_adapters
    receiver.results = [SendResult(success=False), SendResult(success=True)]
    assert await restarted._send_update_notification(paths) is True
    assert [chat for chat, _ in receiver.sent] == ["777", "555"]
    assert not other.sent
