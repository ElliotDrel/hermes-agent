"""Cron durable-definition versus generated-runtime layout."""

import json

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
