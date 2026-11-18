"""Watcher argv for an explicit staged ``/restart`` (HERMES-FORK-028).

The ordinary detached watcher runs ``hermes gateway restart``. A staged restart must
instead run the candidate's activation command, from the candidate's own interpreter:
the running install is about to be renamed away, so neither the watcher nor the
activation may hold a handle inside it.
"""
from __future__ import annotations

from pathlib import Path


def staged_watcher_argv(watcher_code: str, staged: dict, current_pid: int,
                        restart_after_s: float) -> list[str]:
    """``python -I -c <watcher> PID WAIT <activation command>``, rooted in the candidate."""
    from hermes_cli.update_pin_stage import activation_command
    command = activation_command(Path(staged["root"]), staged)
    # The candidate's checkout pth (not the running tree) supplies the watcher's imports.
    return [command[0], "-I", "-c", watcher_code, str(current_pid), str(restart_after_s), *command]
