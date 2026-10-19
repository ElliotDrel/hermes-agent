"""Boundary counters follow the actual SQLite transaction, not process-local attempts."""
from types import SimpleNamespace
import pytest
from hermes_state import SessionDB
from agent.codex_runtime import _record_codex_app_server_compaction


def test_completed_counts_rollback_rotation_resume_and_native_receipt(tmp_path,monkeypatch):
    path=tmp_path/"state.db"
    db=SessionDB(path)
    db.create_session("parent",source="discord",model_config={"preserve":"yes"})
    db.append_message("parent",role="user",content="Question")
    summary=[{"role":"user","content":"Summary"}]
    # Mechanical pruning has no completed-summary boundary marker.
    db.archive_and_compact("parent",summary)
    assert db.get_session_model_config_value("parent","_completed_compactions",0)==0
    original=db._insert_message_rows
    def fail(*args,**kwargs): raise RuntimeError("transaction fault")
    monkeypatch.setattr(db,"_insert_message_rows",fail)
    with pytest.raises(RuntimeError):
        db.archive_and_compact("parent",summary,model_config_patch={"_record_compaction":True})
    assert db.get_session_model_config_value("parent","_completed_compactions",0)==0
    monkeypatch.setattr(db,"_insert_message_rows",original)
    db.archive_and_compact("parent",summary,model_config_patch={"_record_compaction":True})
    assert db.get_session_model_config_value("parent","_completed_compactions")==1
    assert db.get_session_model_config_value("parent","preserve")=="yes"
    db.publish_compression_child(parent_session_id="parent",child_session_id="child",source="discord",messages=summary,require_compression_lease=False)
    assert db.get_session_model_config_value("child","_completed_compactions")==2
    db.close();db=SessionDB(path)
    assert db.get_session_model_config_value("child","_completed_compactions")==2
    agent=SimpleNamespace(_session_db=db,session_id="child",context_compressor=None,_emit_status=lambda *a:None,platform="discord",event_callback=None)
    turn=SimpleNamespace(compacted=True,thread_id="thread",turn_id="turn")
    _record_codex_app_server_compaction(agent,turn)
    _record_codex_app_server_compaction(agent,turn)
    assert db.get_session_model_config_value("child","_completed_compactions")==3
    from hermes_state_worker_compression import worker_provider_boundary
    # Worker replay uses the same transaction helper and exact boundary id.
    for _ in range(2):
        db._execute_write(lambda conn:worker_provider_boundary(db,conn,"child",{"boundary_id":"codex:thread:worker-turn"}))
    assert db.get_session_model_config_value("child","_completed_compactions")==4
    db.record_provider_compaction("child",None)
    assert db.get_session_model_config_value("child","_completed_compactions",0) is None
    db.close()
