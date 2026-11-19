"""Dependency features carried from the active install into a staged candidate (HERMES-FORK-028).

Topical sibling of ``update_pin_stage``. Staging and activation must not rebuild the environment
from PM's defaults: whatever optional extras the active install runs (a ``messaging`` SDK, a
gateway platform's library) have to survive the swap, and a platform the profile has configured
must have its dependencies in the staged environment before the old gateway is drained.
Everything here is read-only on the active install: it reads dist-info names and PM's recorded
facts, and never imports or executes anything from the active tree.
"""
from __future__ import annotations

from pathlib import Path
import re

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_distributions(root: Path) -> set[str]:
    """Canonical names of every distribution in the install's own ``venv``/``.venv`` site-packages."""
    names: set[str] = set()
    for venv in (root / "venv", root / ".venv"):
        for site in (*venv.glob("lib/python*/site-packages"), venv / "Lib" / "site-packages"):
            if site.is_dir():
                names |= {_canonical(entry.name[:-len(".dist-info")].rsplit("-", 1)[0])
                          for entry in site.glob("*.dist-info")}
    return names


def _applicable(marker: str) -> bool:
    """Does a requirement marker hold here? Without ``packaging`` it is assumed to, which only ever
    leaves an extra uncarried (its distribution is then simply not found)."""
    if not marker.strip():
        return True
    try:
        from packaging.markers import Marker
        return bool(Marker(marker).evaluate())
    except ImportError:
        return True
    except Exception:
        return False


def _leaf_requirements(table: dict, extra: str, project: str, seen: set[str] | None = None) -> list[str]:
    """Canonical distribution names an extra needs on this platform, self-referencing extras expanded."""
    seen = seen if seen is not None else set()
    if extra in seen:
        return []
    seen.add(extra)
    self_ref = re.compile(rf"^{re.escape(project)}\[([^\]]+)\]", re.IGNORECASE)
    needed: list[str] = []
    for spec in table.get(extra, ()):
        requirement, _, marker = spec.partition(";")
        if not _applicable(marker):
            continue
        nested = self_ref.match(requirement.replace(" ", ""))
        if nested:
            for member in nested.group(1).split(","):
                needed += _leaf_requirements(table, member, project, seen)
        elif match := _NAME.match(requirement.strip()):
            needed.append(_canonical(match.group(0)))
    return needed


def _satisfied_extras(venv_root: Path, candidate: Path, extras: list[str], *, allow_empty: bool = False) -> list[str]:
    """Declared extras whose every distribution (dist-info of ``venv_root``'s venv) is installed."""
    import tomllib
    with (candidate / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    table = project.get("optional-dependencies", {})
    present = _installed_distributions(venv_root)
    satisfied = []
    for extra in extras:
        needed = _leaf_requirements(table, extra, project.get("name", ""))
        if (needed or allow_empty) and all(name in present for name in needed):
            satisfied.append(extra)
    return satisfied


def active_features(root: Path, candidate: Path) -> tuple[list[str], str]:
    """``(extras, source)`` the active install carries beyond ``all``, restricted to extras the
    candidate declares outside ``all``'s closure. ``source`` is ``"pm"`` when PM recorded a
    selection (PM's recorded extras plus anything its committed generation holds on top), else
    ``"scan"`` for a pre-PM install (dist-info of its own venv)."""
    from pm.extras import committed_generation_extras, droppable_extras, venv_selection_recorded
    optional = droppable_extras(candidate)
    if not venv_selection_recorded(root):
        return _satisfied_extras(root, candidate, optional), "scan"
    from pm.environments import runtime_facts_path, store_root
    from pm.lock import Facts
    recorded: list[str] = []
    for path in (runtime_facts_path(root), store_root(root) / "facts.json"):
        row = Facts(path, strict=True).get("venv")
        if row is not None:
            recorded = [str(extra) for extra in row.get("extras") or ()]
            break
    wanted = {*recorded, *committed_generation_extras(root)}
    return [extra for extra in optional if extra in wanted], "pm"


# Platform value -> extra when it is not the same name with ``_`` as ``-``.
_PLATFORM_EXTRA = {"wecom_callback": "wecom"}


def missing_platform_extras(candidate: Path) -> dict[str, str]:
    """``{extra: platform}`` for each platform the profile has configured whose extra the staged
    environment does not satisfy. Platforms with no declared extra (webhook, plugin adapters) and
    extras this machine cannot install are not judged."""
    from gateway.config import load_gateway_config
    from pm.extras import extra_supported
    from pm.features import declared_extras
    declared = set(declared_extras(candidate))
    wanted = {}
    for platform in load_gateway_config().get_connected_platforms():
        extra = _PLATFORM_EXTRA.get(platform.value, str(platform.value).replace("_", "-"))
        if extra in declared and extra_supported(extra, importable=lambda _anchor: False):
            wanted[extra] = str(platform.value)
    if not wanted:
        return {}
    # Declared distributions, not an import-name guess: an extra such as ``sms`` (aiohttp) has no anchor.
    present = set(_satisfied_extras(candidate, candidate, sorted(wanted), allow_empty=True))
    return {extra: platform for extra, platform in wanted.items() if extra not in present}
