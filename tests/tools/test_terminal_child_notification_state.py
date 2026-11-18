"""The model-facing launch receipt agrees with live-owner completion admission."""
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("surface_child", [False, True])
@pytest.mark.parametrize("async_supported", [False, True])
@pytest.mark.parametrize("promoted", [False, True])
def test_child_receipt_policy_handoff_and_notification_intent(
    tmp_path, monkeypatch, surface_child, async_supported, promoted,
):
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools import terminal_tool as terminal
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.registry import registry
    import tools.process_registry as processes
    import gateway.run as gateway_run

    (tmp_path / "config.yaml").write_text(
        f"delegation:\n  surface_child_process_notifications: {str(surface_child).lower()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    processes_registry = ProcessRegistry()
    proc = ProcessSession(id="proc_child", command="build", cwd=str(tmp_path),
                          task_id="sa-child", owner_task_id="sa-child")
    processes_registry._running[proc.id] = proc
    monkeypatch.setattr(processes, "process_registry", processes_registry)
    monkeypatch.setattr(processes_registry, "spawn_local", lambda **kwargs: proc)
    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "local", "timeout": 180, "cwd": str(tmp_path), "host_cwd": None,
        "modal_mode": "auto", "docker_image": "", "singularity_image": "",
        "modal_image": "", "daytona_image": "",
    })
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_acquire_env", lambda *args, **kwargs: SimpleNamespace(env={}))
    monkeypatch.setattr(terminal, "_check_all_guards", lambda *args, **kwargs: {"approved": True})
    armed = []
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: SimpleNamespace(
        arm_process_watcher=lambda watcher: armed.append(watcher) or True,
    ))
    tokens = set_session_vars(platform="telegram", chat_id="123", session_id="parent",
                              async_delivery=async_supported)
    try:
        with delegated_child_context("worker"):
            arguments: dict[str, object] = {"command": "build"}
            if promoted:
                arguments["timeout"] = 601
            else:
                arguments.update(background=True, notify_on_complete=True)
            raw = registry.dispatch("terminal", arguments, task_id="sa-child")
        assert isinstance(raw, str)
        receipt = json.loads(raw)
        assert receipt.get("error") is None, receipt
        assert receipt["notify_on_complete"] is proc.notify_on_complete is async_supported
        assert bool(armed) is async_supported
        if promoted:
            note = receipt["promoted_from_foreground"]
            if async_supported and not surface_child:
                assert "currently suppressed" in note
                assert "intent remains armed for handoff" in note
                assert "arrives as a notification" not in note
            elif not async_supported:
                assert "cannot receive" in note
            else:
                assert "arrives as a notification" in note
        if not async_supported:
            assert "notify_unsupported" in receipt
            assert "subagent_note" not in receipt
            return
        assert receipt["notification_suppressed"] is (not surface_child)
        assert ("currently suppressed" in receipt["subagent_note"]) is (not surface_child)
        assert "will NOT reach your parent" not in receipt["subagent_note"]
        assert armed[0]["parent_session_id"] == proc.parent_session_id == "worker"
        event = {"type": "completion", "session_id": proc.id, "command": "build",
                 "exit_code": 0, "output": "done", "owner_task_id": proc.owner_task_id,
                 "task_id": proc.task_id}
        processes_registry.completion_queue.put(event)
        assert bool(processes_registry.drain_notifications()) is surface_child
        assert processes_registry.transfer_ownership(
            proc.id, from_owner="sa-child", to_owner="parent", to_task_id="parent",
            to_session_key="parent-route", note="finish build",
        ) is proc
        processes_registry.completion_queue.put({**event, "owner_task_id": proc.owner_task_id,
                                                  "task_id": proc.task_id})
        assert len(processes_registry.drain_notifications()) == 1
        assert proc.notify_on_complete is True
    finally:
        clear_session_vars(tokens)
