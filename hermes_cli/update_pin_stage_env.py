"""Candidate dependency environment, probe and dependency publication for pinned staging.

Topical sibling of ``update_pin_stage`` (HERMES-FORK-028). Everything here is
stdlib-only at import so the promoted candidate can run it from a fresh
interpreter: ``python -I -m hermes_cli.update_pin_stage_env --root R --previous P``.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

_SCRUBBED = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "_HERMES_GATEWAY",
             "UV_NO_CONFIG", "UV_MANAGED_PYTHON", "UV_CONSTRAINT", "UV_BUILD_CONSTRAINT",
             "UV_OVERRIDE", "PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT")


def clean_env(**extra: str) -> dict:
    """Ambient environment without interpreter/dependency selectors of the invoking process."""
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED}
    env.update(PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    env.update(extra)
    return env


# Exit status of ``complete_install`` after PM committed the generation but a later step failed.
SYNC_COMMITTED_EXIT = 75


class PublicationFailedAfterSync(RuntimeError):
    """PM's sync committed the promoted lock, then generation collection/launcher publication failed."""


def candidate_python(root: Path) -> Path:
    from pm.environments import venv_python
    return venv_python(root / "venv")


def build_candidate_environment(candidate: Path, extras: list[str]) -> None:
    """Build the candidate's dependency-only environment with PM's sanctioned builder.

    ``pm.build_environment`` owns toolchain, lock validation and the relocatable
    venv. The candidate's own project is not installed (Hermes forbids non-Nix
    wheels); a relative-to-``sys.prefix`` ``.pth`` binds the source checkout and
    survives whole-tree promotion, which editable finders and console-script
    absolute paths do not.
    """
    import pm
    destination = candidate / "venv"
    pm.build_environment(source=candidate, out=destination, extras=extras,
                         no_install_project=True, frozen=True, explicit=True)
    from pm.environments import site_packages
    (site_packages(destination) / "hermes-checkout.pth").write_text(
        "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent))\n",
        encoding="utf-8")


def probe(candidate: Path) -> None:
    """Import real candidate code in a fresh isolated interpreter; never bless active imports.

    The throwaway home isolates PM state (a probe must not select or create
    dependency generations) and ``HERMES_DISABLE_LAZY_INSTALLS`` stops launch
    preparation from running a dependency sync inside the probe.
    """
    env = clean_env(HERMES_HOME=str(candidate / "venv" / ".stage-probe-home"),
                    HERMES_DISABLE_LAZY_INSTALLS="1")
    python = str(candidate_python(candidate))
    subprocess.run([python, "-I", "-c", "import hermes_cli.main, run_agent, model_tools, toolsets"],
                   cwd=candidate.parent, env=env, check=True, timeout=180)
    subprocess.run([python, "-I", "-m", "hermes_cli.main", "--help"],
                   cwd=candidate.parent, env=env, check=True, timeout=180)


def rename_with_retry(source: Path, destination: Path, *, attempts: int = 8, delay: float = 0.5) -> None:
    """Windows directory rename that tolerates short-lived scanner/indexer handles."""
    for attempt in range(attempts):
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def complete_install(root: Path, previous: Path) -> None:
    """Publish the promoted checkout's PM dependency generation and launchers.

    PM keys install state by checkout path, so a generation cannot be built for the
    candidate's staging path and moved: it is committed here, at the final path, by
    the same explicit transaction ``hermes update`` runs. A failed sync commits
    nothing. The first PM generation of a pre-PM install carries the extras its old
    venv held (read from the retained previous tree, never imported); once PM
    recorded a selection, that ledger owns the extras.
    """
    import pm
    from hermes_cli.venv_sync import collect_superseded_generations, publish_launchers
    from pm.extras import first_generation_selection, legacy_selection
    if first_generation_selection(root) is None:
        extras = carried = None
    else:
        extras = legacy_selection(previous)
        carried = [extra for extra in extras if extra != "all"] or None
    pm.sync_venv(extras, explicit=True, project_root=root, carried=carried)
    try:
        collect_superseded_generations(root)
        publish_launchers(root)
    except Exception as exc:
        raise PublicationFailedAfterSync(str(exc)) from exc


def resync_previous(root: Path) -> None:
    """Re-sync PM to the restored previous tree's lock (the ledger already holds its extras)."""
    import pm
    from hermes_cli.venv_sync import collect_superseded_generations, publish_launchers
    pm.sync_venv(None, explicit=True, project_root=root)
    collect_superseded_generations(root)
    publish_launchers(root)


def run_complete_install(root: Path, previous: Path) -> None:
    """Run ``complete_install`` from the promoted candidate's fresh interpreter."""
    subprocess.run([str(candidate_python(root)), "-I", "-m", "hermes_cli.update_pin_stage_env",
                    "--root", str(root), "--previous", str(previous)],
                   cwd=root.parent, env=clean_env(), check=True, timeout=3600)


def run_resync_previous(root: Path, candidate: Path) -> None:
    """Re-sync PM to the restored tree, driven by the candidate's interpreter (the old venv may be pre-PM)."""
    subprocess.run([str(candidate_python(candidate)), "-I", "-m", "hermes_cli.update_pin_stage_env",
                    "--root", str(root), "--resync-previous"],
                   cwd=root.parent, env=clean_env(), check=True, timeout=3600)


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--previous", type=Path)
    mode.add_argument("--resync-previous", action="store_true")
    options = parser.parse_args()
    if options.resync_previous:
        resync_previous(options.root.resolve())
    else:
        try:
            complete_install(options.root.resolve(), options.previous.resolve())
        except PublicationFailedAfterSync as exc:
            print(f"Dependency publication failed after PM sync committed: {exc}", file=sys.stderr)
            sys.exit(SYNC_COMMITTED_EXIT)
