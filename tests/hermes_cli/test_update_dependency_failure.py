"""Updater imports must not lock packages; failed refreshes must not report success."""

import os
from pathlib import Path
import subprocess
import sys


def test_installer_preparation_keeps_http_dependencies_unloaded(tmp_path):
    code = r'''
import sys
from unittest.mock import patch
from tools import lazy_deps
class ReachedInstaller(Exception):
    pass
def boundary(*args, **kwargs):
    assert "httpx" not in sys.modules
    assert "brotlicffi" not in sys.modules
    raise ReachedInstaller()
with patch.object(lazy_deps, "_run_installer", boundary):
    try:
        lazy_deps._venv_pip_install(("brotlicffi==1.2.0.2",))
    except ReachedInstaller:
        pass
    else:
        raise AssertionError("installer was not reached")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_backend_stays_incomplete_when_core_probes_pass(tmp_path, monkeypatch, capsys):
    from hermes_cli import main, update_cmd, update_cmd_maint
    from tools import lazy_deps
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    error = "failed to remove file " + "x" * 250 + ": Access is denied. (os error 5)"
    monkeypatch.setattr(lazy_deps, "restore_features", lambda features: {"platform.discord": "failed: " + error})
    monkeypatch.setattr(main, "_repair_venv_via_import_probes", lambda *a, **k: "healthy")
    assert main._refresh_active_lazy_features(["uv", "pip"], features=["platform.discord"]) is False
    monkeypatch.setattr(update_cmd, "_post_update_sqlite_runtime_status", lambda: (True, None))
    assert update_cmd_maint._print_update_summary(
        node_failures=[], desktop_build_ok=True, pre_update_version=None) is False
    assert update_cmd_maint._print_verified_update_completion("✓ Update complete!") is False
    output = capsys.readouterr().out
    assert error in output
    assert "✓ Update complete!" not in output


def test_lazy_auth_wrapper_preserves_response_limit(monkeypatch):
    import httpx
    import pytest
    from hermes_cli import auth_codex
    monkeypatch.setattr(auth_codex, "_CODEX_AUTH_BODY_MAX_BYTES", 4)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(b"okay"))),
        event_hooks={"response": [auth_codex._cap_codex_response_body]}) as client:
        assert client.get("https://example.test").content == b"okay"
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(b"too large"))),
        event_hooks={"response": [auth_codex._cap_codex_response_body]}) as client:
        with pytest.raises(auth_codex.AuthError, match="exceeded"):
            client.get("https://example.test")
