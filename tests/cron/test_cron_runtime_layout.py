"""Cron durable-definition versus generated-runtime layout."""

import json

import pytest

from cron import jobs


def test_legacy_cron_runtime_migrates_beneath_runtime_directory(tmp_path):
    """A profile upgrade keeps job data while making generated state ignorable."""
    home = tmp_path / "profile"
    legacy = home / "cron"
    legacy.mkdir(parents=True)
    (legacy / "jobs.json").write_text(
        json.dumps([{"id": "job-1", "name": "preserved"}]), encoding="utf-8"
    )
    (legacy / "ticker_heartbeat").write_text("123", encoding="utf-8")
    (legacy / "output").mkdir()
    (legacy / "output" / "old.md").write_text("output", encoding="utf-8")

    with jobs.use_cron_store(home):
        jobs.ensure_dirs()
        store = jobs._current_cron_store()

        assert store.cron_dir == legacy
        assert store.runtime_dir == legacy / "runtime"
        assert store.jobs_file == legacy / "runtime" / "jobs.json"
        assert store.output_dir == legacy / "runtime" / "output"
        assert json.loads(store.jobs_file.read_text(encoding="utf-8"))[0]["id"] == "job-1"
        assert (store.runtime_dir / "ticker_heartbeat").read_text(encoding="utf-8") == "123"
        assert (store.output_dir / "old.md").read_text(encoding="utf-8") == "output"
        assert not (legacy / "jobs.json").exists()
        assert not (legacy / "ticker_heartbeat").exists()


def test_direct_storage_access_migrates_legacy_without_jobs_load(tmp_path, monkeypatch):
    """CLI reads must preserve old bytes even before a scheduler tick migrates."""
    import sqlite3
    from cron import notepad, suggestions

    monkeypatch.setattr(notepad, "NOTEPAD_FILE", None)
    monkeypatch.setattr(suggestions, "SUGGESTIONS_FILE", None)
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    legacy_db = cron_dir / "notepad.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE cron_notepad (job_id TEXT, key TEXT, value TEXT, "
                     "updated_at TEXT, PRIMARY KEY(job_id, key))")
        conn.execute("INSERT INTO cron_notepad VALUES ('job', 'cursor', '7', 'old')")
    conn.close()  # SQLite's connection context commits but does not close on Windows.
    payload = {"suggestions": [{"id": "old", "status": "pending"}]}
    (cron_dir / "suggestions.json").write_text(json.dumps(payload), encoding="utf-8")
    with jobs.use_cron_store(tmp_path):
        assert notepad.get_note("job", "cursor") == "7"
        assert suggestions.load_suggestions() == payload["suggestions"]
        assert notepad.clear_notepad("job") == 1
    assert (cron_dir / "runtime/notepad.db").exists()
    assert (cron_dir / "runtime/suggestions.json").exists()
    assert not legacy_db.exists()
    assert not (cron_dir / "suggestions.json").exists()


def test_storage_migration_failure_keeps_legacy_readable(tmp_path, monkeypatch):
    from cron import notepad, suggestions
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", None)
    monkeypatch.setattr(suggestions, "SUGGESTIONS_FILE", None)
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    legacy_db = cron_dir / "notepad.db"
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", legacy_db)
    notepad.set_note("job", "cursor", "7")
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", None)
    payload = {"suggestions": [{"id": "old", "status": "pending"}]}
    legacy_json = cron_dir / "suggestions.json"
    legacy_json.write_text(json.dumps(payload), encoding="utf-8")

    def fail_move(*args):
        raise OSError("migration blocked")

    monkeypatch.setattr(jobs.os, "replace", fail_move)
    with jobs.use_cron_store(tmp_path):
        assert notepad._current_notepad_file() == legacy_db
        assert notepad.get_note("job", "cursor") == "7"
        assert suggestions._current_suggestions_file() == legacy_json
        assert suggestions.load_suggestions() == payload["suggestions"]


def test_runtime_conflicts_preserve_both_stores(tmp_path, monkeypatch):
    from cron import notepad, suggestions
    cron_dir = tmp_path / "cron"
    runtime = cron_dir / "runtime"
    runtime.mkdir(parents=True)
    for path, value in ((cron_dir / "notepad.db", "legacy"),
                        (runtime / "notepad.db", "runtime")):
        monkeypatch.setattr(notepad, "NOTEPAD_FILE", path)
        notepad.set_note("job", "cursor", value)
    for directory, value in ((cron_dir, "legacy"), (runtime, "runtime")):
        (directory / "suggestions.json").write_text(
            json.dumps({"suggestions": [{"id": value}]}), encoding="utf-8")
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", None)
    monkeypatch.setattr(suggestions, "SUGGESTIONS_FILE", None)
    with jobs.use_cron_store(tmp_path):
        assert notepad.get_note("job", "cursor") == "runtime"
        assert suggestions.load_suggestions() == [{"id": "runtime"}]
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", cron_dir / "notepad.db")
    assert notepad.get_note("job", "cursor") == "legacy"
    assert json.loads((cron_dir / "suggestions.json").read_text())["suggestions"] == [{"id": "legacy"}]


def test_deleted_named_profile_is_not_recreated(tmp_path, monkeypatch):
    import pytest
    from cron import notepad, suggestions
    home = tmp_path / "profiles/deleted"
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", None)
    monkeypatch.setattr(suggestions, "SUGGESTIONS_FILE", None)
    with jobs.use_cron_store(home):
        with pytest.raises(FileNotFoundError):
            notepad.set_note("job", "cursor", "7")
        with pytest.raises(FileNotFoundError):
            suggestions._save_raw([])
    assert not home.exists()


@pytest.mark.parametrize("remaining", [0, 1])
def test_runtime_backup_restore_preserves_jobs(tmp_path, remaining):
    """Backup discovery and recovery must follow the actual relocated jobs file."""
    from hermes_cli import backup
    home = tmp_path / "home"
    jobs_file = home / "cron/runtime/jobs.json"
    jobs_file.parent.mkdir(parents=True)
    payload = {"jobs": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    jobs_file.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = backup.create_quick_snapshot(hermes_home=home)
    assert snapshot is not None
    jobs_file.write_text(json.dumps({"jobs": payload["jobs"][:remaining]}), encoding="utf-8")
    result = backup.restore_cron_jobs_if_emptied(snapshot, hermes_home=home)
    assert result["restored"] and result["job_count"] == 3
    assert json.loads(jobs_file.read_text(encoding="utf-8")) == payload
