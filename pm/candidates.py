"""Inert, owner-bound dependency generations for explicit source updates.

Preparation has no selection or profile-discovery side effects. Promotion is the
only publication step, through the same durable selection seam as normal sync.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

from pm.environments import install_key, install_state_dir, runtime_facts_path
from pm.filesystem import file_digest
from pm.lock import Facts
from pm.packages import Venv
from pm.store import tree_digest
from pm.workspace import members_stamp


def _head(owner: Path) -> str:
    return subprocess.run(["git", "-C", str(owner), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def _owner(owner: Path) -> Path:
    owner = Path(owner).resolve()
    if not (owner / ".git").exists():
        raise ValueError("Candidates require a Git-backed source owner")
    return owner


def prepare_candidate(owner: Path, source_snapshot: Path, exact_sha: str,
                      extras: list[str], plugin_snapshot) -> dict:
    """Build from explicit frozen inputs without publishing a runtime selection.

    The caller owns snapshot provenance (an advertised Git object) and the update
    lock. Plugins must be explicit: this API never initializes profile/config state.
    """
    owner, source = _owner(owner), Path(source_snapshot).resolve()
    if not re.fullmatch(r"[a-f0-9]{40}", exact_sha):
        raise ValueError("An exact 40-character Git SHA is required")
    if source == owner or not source.is_relative_to(install_state_dir(owner).resolve() / "candidates"):
        raise ValueError("Source snapshot must be private candidate input for this owner")
    if file_digest(source / "pm" / "lock.json") != file_digest(owner / "pm" / "lock.json"):
        raise ValueError("PM toolchain changes require the ordinary updater")
    enabled = sorted(set(extras))
    plugins = {str(Path(k).resolve()): str(Path(v).resolve()) for k, v in plugin_snapshot.items()}
    for path in plugins.values():
        if not Path(path).is_relative_to(source.parent / "plugins"):
            raise ValueError("Plugin snapshot escapes candidate inputs")
    base = _head(owner)
    source_hash = tree_digest(source)
    plugin_stamp = members_stamp(plugins)
    stamp = Venv(source).expected_stamp(enabled, plugin_dirs=plugins)
    result = Venv(owner).apply(enabled, explicit=True, plugin_dirs=plugins, source_snapshot=source)
    if (tree_digest(source) != source_hash or members_stamp(plugins) != plugin_stamp
            or Venv(source).expected_stamp(enabled, plugin_dirs=plugins) != stamp):
        raise ValueError("Candidate inputs changed during preparation; selection was not changed")
    generation = Path(result["environment"]).parent
    return {
        "schema": 1, "owner": str(owner), "install_key": install_key(owner),
        "base_head": base, "target_sha": exact_sha, "source": str(source),
        "source_hash": source_hash, "extras": enabled, "plugins": plugins,
        "plugin_stamp": plugin_stamp, "stamp": stamp,
        "environment": str(result["environment"]), "resolved_lock": str(result["resolved_lock"]),
        "lock_hash": file_digest(Path(result["resolved_lock"])), "generation_hash": tree_digest(generation),
    }


def validate_candidate(owner: Path, manifest: dict, *, promoted: bool = False) -> dict:
    """Validate owner, immutable inputs and complete output before any selection write."""
    owner = _owner(owner)
    if manifest.get("schema") != 1 or manifest.get("owner") != str(owner) or manifest.get("install_key") != install_key(owner):
        raise ValueError("Candidate belongs to a different install")
    state = install_state_dir(owner).resolve()
    environment = Path(manifest["environment"])
    generation = environment.parent.resolve()
    source = Path(manifest["source"])
    lock = Path(manifest["resolved_lock"])
    if (generation.parent != state / "environments" or environment.resolve() != generation / "venv"
            or lock.resolve() != generation / "workspace" / "uv.lock"
            or not source.resolve().is_relative_to(state / "candidates")
            or not (generation / ".lease-managed").is_file()):
        raise ValueError("Candidate paths escape their owning install")
    if (file_digest(lock) != manifest["lock_hash"] or tree_digest(generation) != manifest["generation_hash"]
            or tree_digest(source) != manifest["source_hash"]):
        raise ValueError("Candidate bytes changed after preparation")
    plugins = manifest["plugins"]
    if (not re.fullmatch(r"[a-f0-9]{40}", manifest["target_sha"])
            or any(not Path(path).resolve().is_relative_to(source.parent / "plugins") for path in plugins.values())):
        raise ValueError("Candidate pin or plugin paths are invalid")
    if (members_stamp(plugins) != manifest["plugin_stamp"]
            or Venv(source).expected_stamp(manifest["extras"], plugin_dirs=plugins) != manifest["stamp"]):
        raise ValueError("Candidate dependency inputs changed")
    if _head(owner) != manifest["target_sha" if promoted else "base_head"]:
        raise ValueError("Owner HEAD changed after preparation")
    return {"environment": environment, "resolved_lock": lock}


def promote_candidate(owner: Path, manifest: dict) -> None:
    """Select a verified prebuilt generation; never rebuild or rediscover profiles.

    Caller holds the native update lock. The dependency lock excludes a concurrent
    lazy install between validation and the normal Facts/flush publication seam.
    """
    from hermes_cli.runtime_state import runtime_lock
    from pm.install import _commit_selection

    owner = _owner(owner)
    with runtime_lock(owner, timeout=None) as held:
        if not held:
            raise RuntimeError("Dependency publication lock is busy")
        result = validate_candidate(owner, manifest, promoted=True)
        _commit_selection(Venv(owner), Facts(runtime_facts_path(owner), strict=True), None,
                          enabled=manifest["extras"], stamp=manifest["stamp"],
                          inputs={"plugin_dirs": manifest["plugins"]}, current=False,
                          repair=False, explicit=True, prepared=result)
