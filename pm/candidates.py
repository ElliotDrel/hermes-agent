"""Inert, owner-bound dependency generations for explicit source updates.

Preparation has no selection or profile-discovery side effects. Promotion is the
only publication step, through the same durable selection seam as normal sync.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from pm.environments import install_key, install_state_dir, runtime_facts_path
from pm.filesystem import durable_write_bytes, file_digest, is_junction
from pm.lock import Facts
from pm.packages import Venv
from pm.store import tree_digest
from pm.workspace import members_stamp


def _head(owner: Path) -> str:
    from hermes_platform.resolver import locate_command
    command = locate_command("git").command
    if not command:
        raise ValueError("Git is unavailable for this source owner")
    return subprocess.run([*command, "-C", str(owner), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def _clean_owner(owner: Path) -> None:
    from hermes_platform.resolver import locate_command
    command = locate_command("git").command
    if not command:
        raise ValueError("Git is unavailable for this source owner")
    status = subprocess.run([*command, "-C", str(owner), "status", "--porcelain", "--untracked-files=all"],
                            check=True, capture_output=True, text=True, timeout=30)
    if status.stdout.strip():
        raise ValueError("Candidate source owner is dirty")


def _owner(owner: Path) -> Path:
    owner = Path(owner).resolve()
    if not (owner / ".git").exists():
        raise ValueError("Candidates require a Git-backed source owner")
    return owner


def reject_plugin_links(root: Path) -> None:
    """A plugin snapshot cannot depend on mutable content behind a link/junction."""
    root = Path(root)
    if root.is_symlink() or is_junction(root):
        raise ValueError(f"Plugin candidate contains a symlink or junction: {root}")
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            if entry.is_symlink() or is_junction(entry):
                raise ValueError(f"Plugin candidate contains a symlink or junction: {entry}")


def _retention(generation: Path, retention_owner: str, *, release: bool = False) -> None:
    marker = generation / ".update-retained"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"schema": 1, "owners": []}
    if (not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("owners"), list)
            or any(not isinstance(value, str) or not value for value in data["owners"])):
        raise ValueError("Unknown generation retention record; refusing to replace it")
    owners = set(data["owners"])
    if release:
        owners.discard(retention_owner)
    else:
        owners.add(retention_owner)
    if owners:
        durable_write_bytes(marker, json.dumps({"schema": 1, "owners": sorted(owners)}).encode())
    else:
        marker.unlink(missing_ok=True)


def prepare_candidate(owner: Path, source_snapshot: Path, exact_sha: str,
                      extras: list[str], plugin_snapshot) -> dict:
    """Build inert immutable inputs under the native dependency/GC lock."""
    from hermes_cli.runtime_state import runtime_lock

    owner = _owner(owner)
    with runtime_lock(owner, timeout=None) as held:
        if not held:
            raise RuntimeError("Dependency preparation lock is busy")
        return _prepare_candidate(owner, source_snapshot, exact_sha, extras, plugin_snapshot)


def _prepare_candidate(owner: Path, source_snapshot: Path, exact_sha: str,
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
    if (json.loads((source / "pm" / "lock.json").read_text(encoding="utf-8-sig"))
            != json.loads((owner / "pm" / "lock.json").read_text(encoding="utf-8-sig"))):
        raise ValueError("PM toolchain changes require the ordinary updater")
    enabled = sorted(set(extras))
    plugins = {str(Path(k).resolve()): str(Path(v).resolve()) for k, v in plugin_snapshot.items()}
    for path in plugins.values():
        reject_plugin_links(Path(path))
        if not Path(path).is_relative_to(source.parent / "plugins"):
            raise ValueError("Plugin snapshot escapes candidate inputs")
    base = _head(owner)
    prior = Facts(runtime_facts_path(owner), strict=True).get("venv") or {}
    selection_hash = file_digest(runtime_facts_path(owner))
    source_hash = tree_digest(source)
    plugin_stamp = members_stamp(plugins)
    stamp = Venv(source).expected_stamp(enabled, plugin_dirs=plugins)
    result = Venv(owner).apply(enabled, explicit=True, plugin_dirs=plugins, source_snapshot=source)
    if (file_digest(runtime_facts_path(owner)) != selection_hash
            or tree_digest(source) != source_hash or members_stamp(plugins) != plugin_stamp
            or Venv(source).expected_stamp(enabled, plugin_dirs=plugins) != stamp):
        raise ValueError("Candidate inputs changed during preparation; selection was not changed")
    generation = Path(result["environment"]).parent
    # A preparation outlives this process and its leases; native GC must retain it.
    retention_owner = str(source.parent)
    _retention(generation, retention_owner)
    try:
        generation_hash = tree_digest(generation)
        return {
            "schema": 1, "owner": str(owner), "install_key": install_key(owner),
            "base_head": base, "target_sha": exact_sha, "source": str(source),
            "retention_owner": retention_owner, "previous_environment": prior.get("environment"),
            "source_hash": source_hash, "selection_hash": selection_hash, "extras": enabled, "plugins": plugins,
            "plugin_stamp": plugin_stamp, "stamp": stamp,
            "environment": str(Path(result["environment"]).resolve()), "resolved_lock": str(Path(result["resolved_lock"]).resolve()),
            "lock_hash": file_digest(Path(result["resolved_lock"])), "generation_hash": generation_hash,
        }
    except BaseException:
        _retention(generation, retention_owner, release=True)
        raise


def _manifest_schema(manifest: dict) -> None:
    text = ("owner", "install_key", "base_head", "target_sha", "source", "retention_owner",
            "stamp", "environment", "resolved_lock", "lock_hash", "generation_hash", "source_hash", "plugin_stamp")
    if (not isinstance(manifest, dict) or any(not isinstance(manifest.get(key), str) or not manifest[key] for key in text)
            or not isinstance(manifest.get("extras"), list)
            or any(not isinstance(extra, str) for extra in manifest["extras"])
            or not isinstance(manifest.get("plugins"), dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in manifest["plugins"].items())
            or not {"previous_environment", "selection_hash"} <= manifest.keys()):
        raise ValueError("Candidate manifest is incomplete or malformed")
    if manifest["retention_owner"] != str(Path(manifest["source"]).parent):
        raise ValueError("Candidate retention identity changed")


def validate_candidate(owner: Path, manifest: dict, *, promoted: bool = False) -> dict:
    """Validate owner, immutable inputs and complete output before any selection write."""
    owner = _owner(owner)
    _manifest_schema(manifest)
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
    for path in plugins.values():
        reject_plugin_links(Path(path))
    if (members_stamp(plugins) != manifest["plugin_stamp"]
            or Venv(source).expected_stamp(manifest["extras"], plugin_dirs=plugins) != manifest["stamp"]):
        raise ValueError("Candidate dependency inputs changed")
    _clean_owner(owner)
    if _head(owner) != manifest["target_sha" if promoted else "base_head"]:
        raise ValueError("Owner HEAD changed after preparation")
    if file_digest(runtime_facts_path(owner)) != manifest["selection_hash"]:
        raise ValueError("Active dependency selection changed since preparation")
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
        facts = Facts(runtime_facts_path(owner), strict=True)
        previous = (facts.get("venv") or {}).get("environment")
        if previous:
            generation = Path(previous).resolve().parent
            if generation.parent != install_state_dir(owner).resolve() / "environments":
                raise ValueError("Previous generation does not belong to this owner")
            _retention(generation, manifest["retention_owner"])
        _commit_selection(Venv(owner), facts, None,
                          enabled=manifest["extras"], stamp=manifest["stamp"],
                          inputs={"plugin_dirs": manifest["plugins"]}, current=False,
                          repair=False, explicit=True, prepared=result, project_root=owner)


def finish_candidate(owner: Path, manifest: dict) -> None:
    """After verified activation, release only this run's GC pins.

    Prepared/incomplete runs retain both pins. Shared/foreign owners survive;
    the newly selected generation remains protected by normal native selection.
    """
    from hermes_cli.runtime_state import runtime_lock

    owner = _owner(owner)
    _manifest_schema(manifest)
    if manifest["owner"] != str(owner) or manifest["install_key"] != install_key(owner):
        raise ValueError("Candidate belongs to a different install")
    with runtime_lock(owner, timeout=None) as held:
        if not held:
            raise RuntimeError("Dependency retention lock is busy")
        selected = (Facts(runtime_facts_path(owner), strict=True).get("venv") or {}).get("environment")
        if selected != manifest["environment"] or _head(owner) != manifest["target_sha"]:
            raise ValueError("Only a completed candidate selection may release its retention")
        if manifest["retention_owner"] != str(Path(manifest["source"]).parent):
            raise ValueError("Candidate retention identity changed")
        for environment in (manifest["environment"], manifest.get("previous_environment")):
            if environment is None:
                continue
            generation = Path(environment).resolve().parent
            if generation.parent != install_state_dir(owner).resolve() / "environments":
                raise ValueError("Retained generation belongs to a different install")
            _retention(generation, manifest["retention_owner"], release=True)


def abandon_candidate(owner: Path, manifest: dict) -> None:
    """Release a failed preparation's own pin, without deleting its evidence."""
    from hermes_cli.runtime_state import runtime_lock

    owner = _owner(owner)
    _manifest_schema(manifest)
    if manifest["owner"] != str(owner) or manifest["install_key"] != install_key(owner):
        raise ValueError("Candidate belongs to a different install")
    generation = Path(manifest["environment"]).resolve().parent
    if generation.parent != install_state_dir(owner).resolve() / "environments":
        raise ValueError("Candidate belongs to a different install")
    with runtime_lock(owner, timeout=None) as held:
        if not held:
            raise RuntimeError("Dependency retention lock is busy")
        selected = (Facts(runtime_facts_path(owner), strict=True).get("venv") or {}).get("environment")
        if selected and Path(selected).resolve() == generation / "venv":
            raise ValueError("Cannot abandon a selected candidate")
        _retention(generation, manifest["retention_owner"], release=True)
