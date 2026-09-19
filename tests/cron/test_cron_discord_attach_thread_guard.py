"""Guard Discord continuation jobs against an implicit new thread.

`attach_to_session=True` means the scheduler will open a dedicated continuation
thread when the resolved Discord target has no thread ID. An agent asking for a
flat main-channel post must therefore be rejected at the cronjob tool boundary,
before it persists a job that will create an unexpected Discord thread.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolate the cron store and reload modules that cache HERMES_HOME."""
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_constants
    import cron.jobs
    import cron.scheduler

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    importlib.reload(cron.scheduler)
    return home


def _cronjob(**kwargs):
    from tools.cronjob_tools import cronjob

    return json.loads(cronjob(**kwargs))


def test_create_rejects_discord_attachment_without_thread_target(cron_env):
    """A parent-channel target must not implicitly become a new thread."""
    from cron.jobs import list_jobs

    result = _cronjob(
        action="create",
        name="bad-parent-target",
        schedule="every 1h",
        prompt="Report status.",
        deliver="discord:111111111111111111",
        attach_to_session=True,
    )

    assert result["success"] is False
    assert "attach_to_session=true" in result["error"].lower()
    assert "discord:<parent_channel_id>:<thread_id>" in result["error"]
    assert list_jobs() == []


def test_update_rejects_attachment_without_thread_target(cron_env):
    """An existing main-channel job remains unmodified when attachment is invalid."""
    from cron.jobs import create_job, get_job

    job = create_job(
        prompt="Report status.",
        schedule="every 1h",
        deliver="discord:111111111111111111",
    )

    result = _cronjob(
        action="update",
        job_id=job["id"],
        attach_to_session=True,
    )

    assert result["success"] is False
    assert "attach_to_session=true" in result["error"].lower()
    assert get_job(job["id"]).get("attach_to_session") is None


def test_create_rejects_non_snowflake_discord_thread_target(cron_env):
    """A non-empty placeholder is not a valid Discord thread ID."""
    result = _cronjob(
        action="create",
        name="bad-thread-id",
        schedule="every 1h",
        prompt="Report status.",
        deliver="discord:111111111111111111:not-a-thread",
        attach_to_session=True,
    )

    assert result["success"] is False
    assert "numeric Discord channel and thread IDs" in result["error"]


def test_create_rejects_non_snowflake_discord_origin_thread(cron_env, monkeypatch):
    """An origin thread must also be a Discord snowflake, not a session label."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "111111111111111111")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "not-a-thread")

    result = _cronjob(
        action="create",
        name="bad-origin-thread-id",
        schedule="every 1h",
        prompt="Report status.",
        deliver="origin",
        attach_to_session=True,
    )

    assert result["success"] is False
    assert "numeric Discord thread ID" in result["error"]


def test_create_allows_discord_attachment_with_explicit_thread_target(cron_env):
    """A real parent/thread target preserves the deliberate continuable case."""
    result = _cronjob(
        action="create",
        name="thread-target",
        schedule="every 1h",
        prompt="Report status.",
        deliver="discord:111111111111111111:222222222222222222",
        attach_to_session=True,
    )

    assert result["success"] is True
    assert result["job"]["attach_to_session"] is True


def test_create_allows_discord_attachment_with_explicit_thread_origin(cron_env, monkeypatch):
    """A Discord origin preserves a deliberate continuable thread destination."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "111111111111111111")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "222222222222222222")

    result = _cronjob(
        action="create",
        name="thread-origin",
        schedule="every 1h",
        prompt="Report status.",
        deliver="origin",
        attach_to_session=True,
    )

    assert result["success"] is True
    assert result["job"]["attach_to_session"] is True


def test_update_allows_explicit_thread_target_and_detaching_parent_target(cron_env):
    """Valid continuation and explicit detachment both remain available."""
    from cron.jobs import create_job, get_job

    parent_job = create_job(
        prompt="Report status.",
        schedule="every 1h",
        deliver="discord:111111111111111111",
        attach_to_session=True,
    )
    detached = _cronjob(
        action="update",
        job_id=parent_job["id"],
        attach_to_session=False,
    )
    assert detached["success"] is True
    assert get_job(parent_job["id"])["attach_to_session"] is False

    thread_job = create_job(
        prompt="Report status.",
        schedule="every 1h",
        deliver="discord:111111111111111111",
    )
    attached = _cronjob(
        action="update",
        job_id=thread_job["id"],
        deliver="discord:111111111111111111:222222222222222222",
        attach_to_session=True,
    )
    assert attached["success"] is True
    assert get_job(thread_job["id"])["attach_to_session"] is True
