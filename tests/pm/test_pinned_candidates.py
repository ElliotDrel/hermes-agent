"""Owner-bound candidate preparation/promotion with real Git and PM generations."""
import os
from pathlib import Path
import subprocess

import pytest

from pm.candidates import prepare_candidate, promote_candidate, validate_candidate
from pm.environments import install_state_dir, runtime_facts_path
from pm.lock import Facts
from pm.packages import Venv


from tests.pm._pinned_fixtures import git

pytest_plugins = ["tests.pm._pinned_fixtures"]


def test_prepare_is_inert_and_promotion_selects_prebuilt_generation(candidate_owner, monkeypatch):
    owner, source, base, target, initial = candidate_owner
    before = runtime_facts_path(owner).read_bytes()
    manifest = prepare_candidate(owner, source, target, [], {})
    assert git(owner, "rev-parse", "HEAD") == base
    assert runtime_facts_path(owner).read_bytes() == before
    assert Path(initial["environment"]).is_dir()
    assert Path(manifest["environment"]).is_dir()
    assert str(install_state_dir(owner)) in manifest["environment"]
    # The real generated interpreter can load target bytes, from its own snapshot.
    python = Path(manifest["environment"]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    output = subprocess.run([str(python), "-I", "-c",
                             "import sys; sys.path.insert(0,sys.argv[1]); import payload; print(payload.VALUE)",
                             str(Path(manifest["resolved_lock"]).parent)],
                            capture_output=True, text=True, check=True, timeout=30)
    assert output.stdout.strip() == "prepared target"
    git(owner, "merge", "--ff-only", target)
    def no_build(*args, **kwargs):
        pytest.fail("Promotion must never rebuild the prepared generation")
    monkeypatch.setattr(Venv, "apply", no_build)
    promote_candidate(owner, manifest)
    selected = Facts(runtime_facts_path(owner), strict=True).get("venv")
    assert selected["environment"] == manifest["environment"]
    assert Path(initial["environment"]).is_dir(), "The previous generation remains available"


@pytest.mark.parametrize("tamper", ["lock", "source", "environment", "owner", "head", "path"])
def test_tamper_or_owner_change_cannot_publish(candidate_owner, tamper):
    owner, source, _base, target, initial = candidate_owner
    manifest = prepare_candidate(owner, source, target, [], {})
    before = runtime_facts_path(owner).read_bytes()
    mutations = {
        "lock": lambda: Path(manifest["resolved_lock"]).write_text("changed"),
        "source": lambda: (source / "payload.py").write_text("changed"),
        "environment": lambda: (Path(manifest["environment"]) / "injected.txt").write_text("changed"),
        "owner": lambda: manifest.update(install_key="foreign"),
        "head": lambda: git(owner, "merge", "--ff-only", target),
        "path": lambda: manifest.update(environment=str(owner / "foreign" / "venv")),
    }
    mutations[tamper]()
    with pytest.raises(ValueError):
        validate_candidate(owner, manifest)
    assert runtime_facts_path(owner).read_bytes() == before
    assert Path(initial["environment"]).is_dir()


def test_failed_promotion_recovers_only_the_explicit_owner(candidate_owner, monkeypatch, tmp_path):
    owner, source, _base, target, _initial = candidate_owner
    manifest = prepare_candidate(owner, source, target, [], {})
    before = runtime_facts_path(owner).read_bytes()
    git(owner, "merge", "--ff-only", target)
    from pm import paths
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path / "foreign-source")
    recovered = []
    monkeypatch.setattr("hermes_cli.runtime_state.recover_publication", lambda root: recovered.append(root))
    def fail_flush():
        raise OSError("flush failed")
    monkeypatch.setattr("pm.environments.flush_before_selecting", fail_flush)
    with pytest.raises(OSError, match="flush failed"):
        promote_candidate(owner, manifest)
    assert recovered == [owner]
    assert runtime_facts_path(owner).read_bytes() == before



@pytest.mark.platforms("windows")
@pytest.mark.parametrize("phase", ["prepare", "validate"])
def test_mutable_nested_junction_is_rejected(candidate_owner, tmp_path, phase):
    owner, source, _base, target, _initial = candidate_owner
    plugin = source.parent / "plugins" / "attached"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname="attached"\nversion="1.0"\nrequires-python=">=3.14"\n'
        '[tool.uv]\npackage=false\n', encoding="utf-8")
    plugins = {str(tmp_path / "installed-plugin"): str(plugin)}
    manifest = prepare_candidate(owner, source, target, [], plugins) if phase == "validate" else None
    before = runtime_facts_path(owner).read_bytes()
    external = tmp_path / "mutable-external"
    external.mkdir()
    payload = external / "payload.py"
    payload.write_text("VALUE = 'before'\n", encoding="utf-8")
    subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J", str(plugin / "nested"), str(external)],
                   check=True, capture_output=True, text=True, timeout=30)
    payload.write_text("VALUE = 'mutated external bytes'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symlink or junction"):
        if manifest is None:
            prepare_candidate(owner, source, target, [], plugins)
        else:
            validate_candidate(owner, manifest)
    assert runtime_facts_path(owner).read_bytes() == before
    assert payload.read_text() == "VALUE = 'mutated external bytes'\n"


def test_nested_symlink_input_is_rejected_before_build(candidate_owner, tmp_path):
    owner, source, _base, target, _initial = candidate_owner
    plugin = source.parent / "plugins" / "attached"
    plugin.mkdir(parents=True)
    external = tmp_path / "mutable.py"
    external.write_text("VALUE = 1\n", encoding="utf-8")
    try:
        (plugin / "payload.py").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"Host does not allow creation of a real symlink: {exc}")
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(ValueError, match="symlink or junction"):
        prepare_candidate(owner, source, target, [], {str(tmp_path / "identity"): str(plugin)})
    assert runtime_facts_path(owner).read_bytes() == before


def test_native_gc_retains_prepared_and_previous_generations(candidate_owner):
    owner, source, _base, target, initial = candidate_owner
    manifest = prepare_candidate(owner, source, target, [], {})
    orphan = Path(manifest["environment"]).parent.parent / "unselected-orphan"
    orphan.mkdir()
    (orphan / ".lease-managed").touch()
    from hermes_cli.runtime_state import collect_generations
    assert collect_generations(owner, min_age_seconds=0) == [orphan]
    assert Path(manifest["environment"]).is_dir()
    assert not (Path(initial["environment"]).parent / ".update-retained").exists(), "Prepare leaves active generation untouched"
    git(owner, "merge", "--ff-only", target)
    promote_candidate(owner, manifest)
    assert collect_generations(owner, min_age_seconds=0) == []
    assert Path(initial["environment"]).is_dir()
    assert Path(manifest["environment"]).is_dir()
    from pm.candidates import finish_candidate
    finish_candidate(owner, manifest)
    assert collect_generations(owner, min_age_seconds=0) == [Path(initial["environment"]).parent]
    assert Path(manifest["environment"]).is_dir()


def test_success_releases_only_its_own_generation_retention(candidate_owner):
    owner, source, _base, target, initial = candidate_owner
    from pm.candidates import _retention, finish_candidate
    manifest = prepare_candidate(owner, source, target, [], {})
    previous = Path(initial["environment"]).parent
    _retention(previous, "another-prepared-activation")
    git(owner, "merge", "--ff-only", target)
    promote_candidate(owner, manifest)
    finish_candidate(owner, manifest)
    import json
    assert json.loads((previous / ".update-retained").read_text())["owners"] == ["another-prepared-activation"]
    from hermes_cli.runtime_state import collect_generations
    assert collect_generations(owner, min_age_seconds=0) == []


def test_promotion_refuses_dirty_published_owner(candidate_owner):
    owner, source, _base, target, _initial = candidate_owner
    manifest = prepare_candidate(owner, source, target, [], {})
    git(owner, "merge", "--ff-only", target)
    (owner / "payload.py").write_text("VALUE = 'unreviewed edit'\n", encoding="utf-8")
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(ValueError, match="dirty"):
        promote_candidate(owner, manifest)
    assert runtime_facts_path(owner).read_bytes() == before


def test_abandonment_preserves_foreign_retention_owner(candidate_owner):
    owner, source, _base, target, _initial = candidate_owner
    from pm.candidates import _retention, abandon_candidate
    import json
    manifest = prepare_candidate(owner, source, target, [], {})
    generation = Path(manifest["environment"]).parent
    _retention(generation, "another-prepared-activation")
    abandon_candidate(owner, manifest)
    assert json.loads((generation / ".update-retained").read_text())["owners"] == ["another-prepared-activation"]


def test_final_lock_hash_failure_does_not_orphan_a_gc_pin(candidate_owner, monkeypatch):
    owner, source, _base, target, initial = candidate_owner
    from pm import candidates
    real_digest = candidates.file_digest
    def fail_lock_hash(path):
        if Path(path).name == "uv.lock" and Path(path).parent.name == "workspace":
            raise OSError("final resolved lock hash unavailable")
        return real_digest(path)
    monkeypatch.setattr(candidates, "file_digest", fail_lock_hash)
    before = runtime_facts_path(owner).read_bytes()
    with pytest.raises(OSError, match="final resolved lock hash"):
        prepare_candidate(owner, source, target, [], {})
    generations = Path(initial["environment"]).parent.parent
    unselected = [path for path in generations.iterdir() if path != Path(initial["environment"]).parent]
    assert len(unselected) == 1
    assert not (unselected[0] / ".update-retained").exists()
    from hermes_cli.runtime_state import collect_generations
    assert collect_generations(owner, min_age_seconds=0) == unselected
    assert runtime_facts_path(owner).read_bytes() == before
    assert Path(initial["environment"]).is_dir()
