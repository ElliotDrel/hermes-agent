"""Already-human former worker routes are boundaries, never repair targets."""
import copy
from types import SimpleNamespace
from typing import cast

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", ["current", "descendant", "parent", "sibling", "compression"])
@pytest.mark.parametrize("malformed", [False, True])
async def test_current_worker_route_preserves_human_history_and_cached_prefix(
    tmp_path, request, private_db_probe_cleanup, pin, malformed,
):
    import sqlite3
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.session import SessionSource
    from tools import async_delegation as ad
    from hermes_state import SessionDB

    ad._reset_for_tests()
    request.addfinalizer(ad._reset_for_tests)
    with _profile_runtime_scope(tmp_path):
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        request.addfinalizer(runner.close_all_session_db_handles)
        request.addfinalizer(runner.session_store.close_all_db_handles)
        store = runner.session_store
        entry = store.get_or_create_session(SessionSource(
            platform=Platform.TELEGRAM, chat_id="former-worker", chat_type="dm",
        ))
        db = cast(SessionDB, store._db)
        parent = entry.session_id
        db.create_session("worker", source="subagent", model_config={"_delegate_from": parent})
        entry = store.switch_session(entry.session_key, "worker")
        assert entry is not None
        if malformed:
            with sqlite3.connect(db.db_path) as conn:
                conn.execute("UPDATE sessions SET model_config=? WHERE id='worker'", ("{broken",))
        db.create_session("descendant", source="subagent", model_config={"_delegate_from": "worker"})
        db.create_session("sibling", source="subagent", model_config={"_delegate_from": parent})
        if pin == "compression":
            db.end_session(parent, end_reason="compression")
            db.create_session("tip", source="telegram", parent_session_id=parent)
        # Real human history on an already-affected route must not be ended or rewritten.
        db.append_message("worker", role="user", content="human follow-up on former worker")
        db.append_message("worker", role="assistant", content="answer already sent")
        history = db.get_messages_as_conversation("worker")
        cached_prefix = copy.deepcopy(history)
        rows_before = {sid: db.get_session(sid) for sid in [parent, "worker", "descendant", "sibling"]}
        pinned = {"current": "worker", "descendant": "descendant", "parent": parent,
                  "sibling": "sibling", "compression": parent}[pin]
        admitted = []

        async def accept(event):
            current = store.lookup_by_session_key(entry.session_key)
            assert current is not None
            resolved = await runner._resolve_async_delegation_session(
                current, event.metadata["gateway_session_id"],
            )
            assert resolved is not None and resolved.session_id == "worker"
            admitted.append(event.text)
            event._gateway_accepted = True

        runner.adapters[Platform.TELEGRAM] = cast(BasePlatformAdapter, SimpleNamespace(handle_message=accept))
        evt = {"type": "async_delegation", "delegation_id": "route-boundary",
               "session_key": entry.session_key, "parent_session_id": pinned,
               "dispatched_at": 1.0, "summary": "new completion", "status": "completed"}
        ad._persist_dispatch(evt)
        ad._persist_completion(evt, {"status": "completed", "summary": "new completion"})
        await runner._deliver_completion_notification("new completion", evt)
        allowed = pin in {"current", "descendant"}
        assert bool(admitted) is allowed
        durable = ad.get_durable_delegation(evt["delegation_id"])
        assert durable is not None
        assert durable["delivery_state"] == (
            "delivered" if allowed else "dropped"
        )
        current = store.lookup_by_session_key(entry.session_key)
        assert current is not None and current.session_id == "worker"
        assert db.get_messages_as_conversation("worker") == history == cached_prefix
        for sid, row in rows_before.items():
            assert db.get_session(sid) == row

