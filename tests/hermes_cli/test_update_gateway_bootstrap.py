"""The completion bootstrap loads without application dependencies after a checkout swap."""

import os
import shutil
import subprocess
import sys


def test_completion_bootstrap_publishes_correlated_status_without_app_imports(tmp_path):
    from hermes_cli import update_completion
    script = tmp_path / "completion.py"
    shutil.copy2(update_completion.__file__, script)
    run_id = "ab" * 16
    run_home = tmp_path / "logs" / "update_receipts" / "gateway" / run_id
    run_home.mkdir(parents=True)
    program = (
        "import runpy, sys; namespace = runpy.run_path(sys.argv[1]); "
        "namespace['_publish_gateway_success']({'gateway_mode': True, 'home': sys.argv[2]})"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", program, str(script), str(tmp_path)],
        env={**os.environ, "HERMES_UPDATE_CORRELATION_ID": run_id},
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (run_home / ".update_exit_code").read_text(encoding="utf-8") == "0"
    assert not (tmp_path / ".update_exit_code").exists()
