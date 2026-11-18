"""Recovery helpers for explicit staged activation (HERMES-FORK-028).

Topical sibling of ``update_pin_stage``. A ``/restart`` drains the gateway before
activation runs, so every activation outcome that leaves the old install in place
must end with that install's gateway running again.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import time

# The watcher fires activation once the drain deadline passes, even when the gateway is
# still exiting; wait this long for it to leave before deciding nobody will restart it.
GATEWAY_EXIT_WAIT_S = 60.0


class GatewayStillRunning(ValueError):
    """Activation refused because the drained gateway has not exited yet."""


def consume_marker(path: Path, *, attempts: int = 5, delay: float = 0.2) -> bool:
    """Remove the pending marker; tolerate a missing file and a briefly locked one (Windows).

    Returns False when it is still present. A leftover marker is harmless once its
    candidate directory is gone: ``restart_candidate`` clears it explicitly.
    """
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if attempt < attempts - 1:
                time.sleep(delay)
    return False


def recover_gateway(root: Path, *, wait_s: float = 0.0) -> None:
    """Start the untouched installed gateway when none is running (after waiting up to ``wait_s``)."""
    from gateway.status import get_running_pid
    from hermes_cli.update_pin_stage_env import candidate_python, clean_env
    from hermes_cli.update_receipt import record_step
    deadline = time.monotonic() + wait_s
    while get_running_pid() and time.monotonic() < deadline:
        time.sleep(0.5)
    if get_running_pid() or not root.exists():
        return
    try:
        recovered = subprocess.run([str(candidate_python(root)), "-m", "hermes_cli.main", "gateway", "start"],
                                   cwd=root.parent, env=clean_env(PYTHONPATH=str(root)))
        record_step("activation_recovery_start", recovered.returncode == 0,
                    "native start attempted; inspect gateway status")
    except OSError as recovery_error:
        record_step("activation_recovery_start", False, str(recovery_error))
