"""Offline command routing and effective request-input regressions.

All command entry points stop at fake chat/agent execution. Real native skill
rendering and request assembly run against an isolated skill and fake provider.
No updater, installer, gateway lifecycle or paid model call is permitted.
"""
import asyncio
import copy
import socket
import subprocess
from argparse import ArgumentParser, Namespace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.fork_update_entry import build_update_invocation, UPDATE_REQUEST


@pytest.fixture(autouse=True)
def forbid_external_execution(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Update entry test attempted external execution")
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        # CPython builds Windows asyncio's self-pipe with a private loopback
        # socketpair before the test coroutine runs. Permit only that constructor.
        import traceback
        if (isinstance(address, tuple) and address[0] == "127.0.0.1"
                and any(frame.name in {"socketpair", "_fallback_socketpair"} and frame.filename.endswith("socket.py")
                        for frame in traceback.extract_stack())):
            return original_connect(sock, address)
        return refuse(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    # Import candidate main only with its native self-repair hooks intercepted.
    import hermes_cli._early_recovery as recovery
    monkeypatch.setattr(recovery, "restore_interrupted_pull", lambda: False)
    monkeypatch.setattr(recovery, "recover_if_needed", lambda: None)
    import hermes_cli.main as main
    monkeypatch.setattr(main, "_prepare_agent_startup", MagicMock(name="native_chat_startup"))
    monkeypatch.setattr(main, "cmd_chat", MagicMock(name="native_chat", return_value="fake-chat"))
    monkeypatch.setattr(main, "_cmd_native_update", refuse)
    monkeypatch.setattr("gateway.slash_commands._spawn_detached_update", refuse)


@pytest.fixture
def isolated_skill(tmp_path, monkeypatch):
    import agent.skill_commands as skills
    import tools.skills_tool as tool
    root = tmp_path / "skills"
    directory = root / "hermes-fork-update"
    directory.mkdir(parents=True)
    directory.joinpath("SKILL.md").write_text(
        "---\nname: hermes-fork-update\ndescription: Offline update fixture\n---\n"
        "# Update the maintained fork\n\nOFFLINE_WORKFLOW_BODY\n"
        "Prepare an isolated candidate. Respect protected and lifecycle gates.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "SKILLS_DIR", root)
    monkeypatch.setattr(skills, "_skill_commands", {})
    monkeypatch.setattr(skills, "_skill_commands_platform", None)
    skills.scan_skill_commands()
    return root


def make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._draining = False
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]))
    runner._check_slash_access = lambda *args: None
    # A scope fixture records the boundary without accessing any real profile.
    scopes = []
    @asynccontextmanager
    async def scope(source):
        scopes.append(("enter", source.profile))
        try:
            yield
        finally:
            scopes.append(("exit", source.profile))
    runner._async_profile_scope_for_source = scope
    return runner, scopes


def event_for(platform=Platform.DISCORD, text="/update"):
    return MessageEvent(text=text, message_id="current-message",
        reply_to_message_id="reply-anchor", reply_to_text="historical /update",
        channel_context="Earlier user: hermes update --force",
        channel_prompt="stable channel instructions",
        metadata={"request_metadata": "retained"},
        source=SessionSource(platform=platform, chat_id="parent", thread_id="existing-thread",
            user_id="owner", profile="default"))


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.DISCORD, Platform.TELEGRAM])
async def test_idle_native_dispatch_keeps_session_metadata_and_loaded_user_turn(isolated_skill, platform):
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    runner, scopes = make_runner()
    event = event_for(platform)
    before = copy.deepcopy(vars(event))
    source = event.source
    key = runner._session_key_for_source(source)
    handled, result = await runner._hm_dispatch_idle_commands(event, source, key)
    assert (handled, result) == (False, None), "Update must fall through to the ordinary current-session agent turn"
    assert "OFFLINE_WORKFLOW_BODY" in event.text
    assert UPDATE_REQUEST in event.text
    assert "Explicit command in this turn: /update" in event.text
    assert "required independently authorized machine-side handoff" in event.text
    assert scopes == [("enter", "default"), ("exit", "default")]
    assert event.source is source and runner._session_key_for_source(source) == key
    assert {k: v for k, v in vars(event).items() if k != "text"} == {k: v for k, v in before.items() if k != "text"}


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.DISCORD, Platform.TELEGRAM])
async def test_effective_gateway_agent_input_has_current_prompt_history_and_request_metadata(isolated_skill, platform):
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    runner, _ = make_runner()
    event = event_for(platform)
    source = event.source
    key = runner._session_key_for_source(source)
    await runner._hm_dispatch_idle_commands(event, source, key)
    history = [{"role": "user", "content": "prior /update is history only"},
               {"role": "assistant", "content": "prior answer"}]
    snapshot = copy.deepcopy(history)
    entry = SimpleNamespace(session_id="existing-session")
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, key))
    async def prepare(*args):
        # Exercise the production inbound decoration and timestamp consumer.
        text = runner._prepend_inbound_reply_context(event, source, event.text)
        text, persisted, timestamp = runner._hmwa_apply_message_timestamp(event, text)
        return runner._PreparedTurn(history, "stable session/system context", text,
            persisted, timestamp, None, entry.session_id, "fixture-owner"), {}
    runner._hmwa_prepare_turn = prepare
    observed = []
    async def fake_agent(**kwargs):
        observed.append(kwargs)
        raise asyncio.CancelledError  # stop before actual execution/persistence/delivery
    runner._run_agent = fake_agent
    runner._clear_session_env = lambda *args: None
    with pytest.raises(asyncio.CancelledError):
        await runner._handle_message_with_agent(event, source, key, 7)
    request = observed[0]
    assert "OFFLINE_WORKFLOW_BODY" in request["message"]
    assert request["history"] == snapshot and history == snapshot
    assert request["context_prompt"] == "stable session/system context"
    assert request["session_id"] == "existing-session" and request["session_key"] == key
    assert request["session_message_event"] is event
    assert request["source"] is source and request["run_generation"] == 7
    assert request["inbound_message_id"] == "current-message"
    assert request["event_message_id"] == "current-message"  # native reply anchor, not the quoted-message ID
    assert request["channel_prompt"] == "stable channel instructions"
    assert request["persist_user_display_metadata"]["gateway_input_owner"] == "fixture-owner"
    assert "Explicit command in this turn: /update" in request["persist_user_message"]
    assert event.metadata == {"request_metadata": "retained"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["running", "pending"])
async def test_busy_policy_refuses_before_loading_or_interrupting(isolated_skill, kind):
    from hermes_cli.commands import resolve_command
    from gateway.run import _AGENT_PENDING_SENTINEL
    runner, scopes = make_runner()
    runner._handle_update_command = AsyncMock(side_effect=AssertionError("Busy update reached skill loading"))
    runner._interrupt_running_turn = MagicMock(side_effect=AssertionError("Busy update interrupted current run"))
    event = event_for()
    current = _AGENT_PENDING_SENTINEL if kind == "pending" else object()
    runner._running_agents = {"owned-route": current}
    result = await runner._hm_busy_slash_or_photo(event, event.source, "owned-route")
    assert result[0] is True and "can't run" in result[1]
    assert resolve_command("update").busy_policy == "reject"
    assert event.text == "/update" and runner._running_agents["owned-route"] is current
    assert not scopes
    runner._handle_update_command.assert_not_awaited()
    runner._interrupt_running_turn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no-control", "internal", "bot", "quoted", "ordinary", "argument"])
async def test_untrusted_or_nonliteral_input_cannot_authorize_update(isolated_skill, kind):
    runner, _ = make_runner()
    event = event_for()
    if kind == "no-control":
        event.allow_gateway_control = False
    elif kind == "internal":
        event.internal = True
    elif kind == "bot":
        event.source.is_bot = True
    elif kind == "quoted":
        event.text = "> /update"
    elif kind == "ordinary":
        event.text = "Discuss the earlier /update request"
    else:
        event.text = "/update --continue"
    with pytest.raises(ValueError):
        await runner._handle_update_command(event)


@pytest.mark.asyncio
async def test_authorization_and_drain_gates_remain_before_agent(isolated_skill):
    runner, _ = make_runner()
    event = event_for()
    runner._check_slash_access = lambda *args: "admin denied"
    runner._handle_update_command = AsyncMock(side_effect=AssertionError("Authorization bypass"))
    assert await runner._hm_dispatch_idle_commands(event, event.source, "key") == (True, "admin denied")
    runner._handle_update_command.assert_not_awaited()
    runner._check_slash_access = lambda *args: None
    runner._draining = True
    runner._status_action_gerund = lambda: "draining"
    runner._handle_update_command = AsyncMock(return_value="loaded user skill")
    handled, reply = await runner._hm_dispatch_idle_commands(event, event.source, "key")
    assert handled and "not accepting" in reply


def test_native_terminal_command_dispatch_uses_fake_chat_only(isolated_skill):
    import hermes_cli.main as main
    args = Namespace(model="explicit/model", provider="explicit-provider", toolsets="terminal",
        resume="existing-terminal-session", profile="default", verbose=True)
    before = vars(args).copy()
    assert main.cmd_update(args) == "fake-chat"
    request = main.cmd_chat.call_args.args[0]
    assert "OFFLINE_WORKFLOW_BODY" in request.query and UPDATE_REQUEST in request.query
    assert "Explicit command in this turn: hermes update" in request.query
    assert request.model == args.model and request.provider == args.provider
    assert request.resume == args.resume and request.toolsets == args.toolsets
    assert request.command == "chat"
    main._prepare_agent_startup.assert_called_once_with(request)
    assert vars(args) == before


@pytest.mark.parametrize("flag", ["--check", "--plan", "--gateway", "--yes", "--force", "--no-gateway-restart",
    "--list-venv-holders", "--no-backup", "--backup", "--keep-stash", "--switch-branch", "--force-venv"])
def test_native_options_refuse_without_chat_or_updater(isolated_skill, flag):
    import hermes_cli.main as main
    from hermes_cli.subcommands.update import build_update_parser
    parser = ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    args = parser.parse_args(["update", flag])
    with pytest.raises(SystemExit) as result:
        args.func(args)
    assert result.value.code == 2
    main.cmd_chat.assert_not_called()
    main._prepare_agent_startup.assert_not_called()


@pytest.mark.parametrize("flag", ["--continue", "--abort"])
def test_legacy_flags_are_no_longer_in_parser(flag):
    import hermes_cli.main as main
    from hermes_cli.subcommands.update import build_update_parser
    parser = ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    with pytest.raises(SystemExit) as result:
        parser.parse_args(["update", flag])
    assert result.value.code == 2
    main.cmd_chat.assert_not_called()
    main._prepare_agent_startup.assert_not_called()


@pytest.mark.parametrize("state", ["missing", "disabled"])
def test_skill_unavailable_fails_closed_without_chat(isolated_skill, monkeypatch, state):
    import hermes_cli.main as main
    if state == "disabled":
        monkeypatch.setattr("agent.skill_utils.get_disabled_skill_names", lambda **kwargs: {"hermes-fork-update"})
    else:
        monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {})
    with pytest.raises(SystemExit) as result:
        main.cmd_update(Namespace())
    assert result.value.code == 2
    main.cmd_chat.assert_not_called()
    main._prepare_agent_startup.assert_not_called()


def test_terminal_slash_queues_native_skill_turn_without_relaunch(isolated_skill):
    from hermes_cli.cli_loops_mixin import CLILoopsMixin
    cli = SimpleNamespace(session_id="same-terminal-session", _queue_loaded_skills=MagicMock(),
        _console_print=MagicMock(), _handle_update_command=MagicMock(side_effect=AssertionError("Legacy relaunch")))
    assert CLILoopsMixin._cmd_update(cli, "/update") is True
    prompt, label, missing = cli._queue_loaded_skills.call_args.args
    assert "OFFLINE_WORKFLOW_BODY" in prompt and "Explicit command in this turn: /update" in prompt
    assert UPDATE_REQUEST in prompt and missing is None
    cli._handle_update_command.assert_not_called()


@pytest.mark.parametrize("cache", [False, True])
def test_effective_fake_provider_requests_keep_prefix_schemas_options_and_resume(isolated_skill, cache):
    from agent.prompt_caching import strip_anthropic_cache_control
    from run_agent import AIAgent
    schemas = [{"type": "function", "function": {"name": "offline_only", "description": "offline fixture",
        "parameters": {"type": "object", "properties": {}}}}]
    with (patch("model_tools.get_tool_definitions", return_value=schemas),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="offline", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "stable system\n\nstable session context"
    agent._cached_system_prompt_static = "stable system"
    agent._use_prompt_caching = cache
    agent._use_native_cache_layout = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    requests = []
    def fake_model(**payload):
        requests.append(copy.deepcopy(payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="offline answer",
            tool_calls=None, reasoning=None, reasoning_content=None, reasoning_details=None),
            finish_reason="stop")], model="test/model", usage=None)
    agent.client.chat.completions.create.side_effect = fake_model
    history = [{"role": "user", "content": "ordinary prior user /update"},
               {"role": "assistant", "content": "ordinary prior assistant"}]
    before = copy.deepcopy(history)
    message = build_update_invocation("/update", task_id="same-session", platform="discord")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation(message, conversation_history=history)
        assert result["completed"]
        resumed = copy.deepcopy(result["messages"])
        again = agent.run_conversation("ordinary follow-up", conversation_history=resumed)
        assert again["completed"]
    canonical = [strip_anthropic_cache_control(copy.deepcopy(row["messages"])) for row in requests]
    assert len(requests) == 2 and history == before
    assert [row["role"] for row in canonical[0]] == ["system", "user", "assistant", "user"]
    assert "OFFLINE_WORKFLOW_BODY" in canonical[0][-1]["content"] and UPDATE_REQUEST in canonical[0][-1]["content"]
    assert canonical[0] == canonical[1][:len(canonical[0])]
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    assert requests[0]["tools"] == requests[1]["tools"] == schemas
    assert {k: v for k, v in requests[0].items() if k != "messages"} == {k: v for k, v in requests[1].items() if k != "messages"}
    assert agent._cached_system_prompt == "stable system\n\nstable session context"
