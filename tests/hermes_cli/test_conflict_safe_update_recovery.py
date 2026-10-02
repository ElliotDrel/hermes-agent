"""Conflict recovery must not combine cached old launchers with rebased helpers."""
import ast
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import Mock

import pytest

from hermes_cli import update_cmd_windows as recovery

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_real_gateway_processes(monkeypatch):
    # Guard baseline RED too: its recovery branch still calls the installed helpers.
    from hermes_cli import gateway
    monkeypatch.setattr(gateway, 'subprocess', types.SimpleNamespace(
        Popen=Mock(return_value=types.SimpleNamespace(pid=123)), DEVNULL=-3))


def _functions(source, names):
    tree = ast.parse(source)
    return '\n\n'.join(ast.get_source_segment(source, node) for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name in names)


@pytest.fixture
def snapshot(tmp_path):
    """Real pre-update helper bodies, with only process creation/dependencies faked."""
    root = tmp_path / 'verified recovery tree'
    package = root / 'hermes_cli'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    evidence = tmp_path / 'launch.jsonl'
    helpers = _functions((ROOT / 'hermes_cli/gateway.py').read_text(encoding='utf-8'), {
        'launch_detached_profile_gateway_restart', 'launch_detached_gateway_restart_by_cmdline',
        '_spawn_gateway_restart_watcher', '_gateway_run_args_for_profile',
    })
    (package / 'gateway.py').write_text('''import json, os, sys, textwrap, types
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
def get_python_path():
    return sys.executable
def _popen(argv, **kwargs):
    with open(os.environ['RECOVERY_EVIDENCE'], 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'argv': argv, 'kwargs': kwargs, 'cwd': os.getcwd(),
            'home': os.environ.get('HERMES_HOME'), 'pythonpath': os.environ.get('PYTHONPATH'),
            'venv': os.environ.get('VIRTUAL_ENV'), 'bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE')}) + '\\n')
    return types.SimpleNamespace(pid=123)
subprocess = types.SimpleNamespace(Popen=_popen, DEVNULL=-3)
''' + helpers, encoding='utf-8')
    spec = _functions((ROOT / 'hermes_cli/gateway_windows.py').read_text(encoding='utf-8'), {
        'windowless_gateway_restart_spec',
    })
    (package / 'gateway_windows.py').write_text('''import os, sys
from pathlib import Path
def _resolve_detached_python(python):
    return python, Path(os.environ['VIRTUAL_ENV']), []
def _hermes_home():
    return Path(os.environ['HERMES_HOME'])
def _prepend_pythonpath(overlay, paths):
    overlay['PYTHONPATH'] = os.pathsep.join(paths)
def _stable_gateway_working_dir(root):
    return str(root)
''' + spec, encoding='utf-8')
    (package / '_subprocess_compat.py').write_text('''def windows_detach_popen_kwargs():
    return {}
def windows_detach_flags_without_breakaway():
    return 0
''', encoding='utf-8')
    return root, evidence


def _token(root):
    return {'resume_needed': True, 'restart_source_root': str(root),
            'profiles': {'school': 101}, 'unmapped': [
                {'pid': 202, 'argv': ['python.exe', '-m', 'hermes_cli.main', 'gateway', 'run']}]}


@pytest.fixture
def launch_env(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'custom profile home'))
    monkeypatch.setenv('VIRTUAL_ENV', str(tmp_path / 'original venv'))
    # A mutable installed tree must not become the watcher's import fallback.
    monkeypatch.setenv('PYTHONPATH', str(ROOT))


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows cross-generation restart spec')
@pytest.mark.parametrize('profile', ['default', 'school'])
def test_real_cross_generation_helpers_launch_from_recovery(snapshot, launch_env, monkeypatch, profile):
    root, evidence = snapshot
    monkeypatch.setenv('RECOVERY_EVIDENCE', str(evidence))
    # Reproduce the production boundary: old gateway cached, new spec imported later.
    from hermes_cli import gateway
    # Frozen target-generation signature at the known f97608f178 boundary.
    # Its body cannot run here: argument binding must reject source_root first.
    new_source = "def windowless_gateway_restart_spec(run_argv: list[str]):\n    raise AssertionError('must fail argument binding')\n"
    namespace = {'Path': Path}
    exec(_functions(new_source, {'windowless_gateway_restart_spec'}), namespace)
    newer = types.ModuleType('hermes_cli.gateway_windows')
    newer.windowless_gateway_restart_spec = namespace['windowless_gateway_restart_spec']
    monkeypatch.setitem(sys.modules, 'hermes_cli.gateway_windows', newer)
    monkeypatch.setattr(gateway.sys, 'platform', 'win32')
    never_spawn = Mock(side_effect=AssertionError('must not spawn a real watcher'))
    monkeypatch.setattr(gateway, 'subprocess', types.SimpleNamespace(Popen=never_spawn, DEVNULL=-3))
    with pytest.raises(TypeError, match='source_root'):
        newer.windowless_gateway_restart_spec(['python.exe'], source_root=str(root))
    assert gateway.launch_detached_profile_gateway_restart('school', 101, source_root=str(root)) is False
    token = _token(root)
    token['profiles'] = {profile: 101}
    assert recovery._relaunch_paused_gateways(token, token['profiles'], token['unmapped']) == ([profile], 1)
    never_spawn.assert_not_called()
    records = [json.loads(line) for line in evidence.read_text(encoding='utf-8').splitlines()]
    assert len(records) == 2
    for record in records:
        assert Path(record['cwd']) == root
        assert record['home'] == os.environ['HERMES_HOME']
        assert record['venv'] == os.environ['VIRTUAL_ENV']
        assert record['bytecode'] == '1'
        assert record['pythonpath'].split(os.pathsep)[0] == str(root)
        assert str(ROOT) not in record['pythonpath'].split(os.pathsep)
        watcher = record['argv'][2]
        assignments = {node.targets[0].id: ast.literal_eval(node.value)
                       for node in ast.parse(watcher).body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                       and node.targets[0].id in {'_respawn_cwd', '_respawn_env_overlay'}}
        assert Path(assignments['_respawn_cwd']) == root
        assert assignments['_respawn_env_overlay']['PYTHONPATH'] == str(root)
        assert assignments['_respawn_env_overlay']['HERMES_HOME'] == os.environ['HERMES_HOME']
        assert 'from gateway.status import _pid_exists' in watcher
    assert records[0]['argv'][3] == '101'
    assert records[0]['argv'][-3:] == ['gateway', 'run', '--replace']
    if profile == 'default':
        assert '--profile' not in records[0]['argv']
    else:
        assert records[0]['argv'][-5:-3] == ['--profile', profile]
    assert records[1]['argv'][3] == '202'
    assert token['profiles'] == {} and token['unmapped'] == []


def test_fresh_child_uses_snapshot_package_only(snapshot, launch_env, monkeypatch):
    root, evidence = snapshot
    monkeypatch.setenv('RECOVERY_EVIDENCE', str(evidence))
    poison = types.ModuleType('hermes_cli.gateway')
    poison.launch_detached_profile_gateway_restart = Mock(side_effect=AssertionError('cached module used'))
    poison.launch_detached_gateway_restart_by_cmdline = poison.launch_detached_profile_gateway_restart
    monkeypatch.setitem(sys.modules, 'hermes_cli.gateway', poison)
    token = _token(root)
    assert recovery._relaunch_paused_gateways(token, token['profiles'], token['unmapped']) == (['school'], 1)
    poison.launch_detached_profile_gateway_restart.assert_not_called()
    assert evidence.exists()


@pytest.mark.parametrize('failure', ['false', 'raise', 'typeerror'])
def test_helper_failure_retains_recovery_obligation(snapshot, launch_env, monkeypatch, caplog, failure):
    root, _ = snapshot
    package = root / 'hermes_cli/gateway.py'
    secret = 'token=do-not-print argv=private env=private'
    exception = 'TypeError' if failure == 'typeerror' else 'RuntimeError'
    action = 'return False' if failure == 'false' else f"raise {exception}({secret!r})"
    package.write_text('def launch_detached_profile_gateway_restart(*args, **kwargs):\n    ' + action +
                       '\ndef launch_detached_gateway_restart_by_cmdline(*args, **kwargs):\n    ' + action,
                       encoding='utf-8')
    token = _token(root)
    expected = json.loads(json.dumps(token))
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match='every paused'):
        recovery._relaunch_paused_gateways(token, token['profiles'], token['unmapped'])
    assert token['profiles'] == expected['profiles']
    assert token['unmapped'] == expected['unmapped']
    assert token['resume_needed'] is True
    assert token['relaunched_profiles'] == []
    assert 'Conflict-safe' in caplog.text
    for stage in ('profile-helper', 'cmdline-helper'):
        assert f'stage={stage}' in caplog.text
    assert ('reason=helper-false' if failure == 'false' else f'reason=exception class={exception}') in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize('missing', ['tree', 'helper'])
def test_missing_recovery_tree_refuses_before_launch(snapshot, monkeypatch, missing):
    root, _ = snapshot
    if missing == 'tree':
        root = root / 'missing'
    else:
        (root / 'hermes_cli/gateway.py').unlink()
    run = Mock(side_effect=AssertionError('missing recovery must not launch'))
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    token = _token(root)
    with pytest.raises(RuntimeError, match='Conflict-safe.*missing'):
        recovery._relaunch_paused_gateways(token, token['profiles'], token['unmapped'])
    run.assert_not_called()
    assert token['profiles'] == {'school': 101}
    assert token['resume_needed'] is True


@pytest.mark.parametrize('failure', ['timeout', 'exit', 'launch'])
def test_subprocess_failure_is_bounded_visible_and_retains_tokens(snapshot, monkeypatch, caplog, failure):
    root, _ = snapshot
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs['stdout'] == subprocess.DEVNULL
        assert kwargs['stderr'] != subprocess.PIPE and hasattr(kwargs['stderr'], 'read')
        if failure == 'launch':
            raise OSError('token=secret-launch')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        return subprocess.CompletedProcess(argv, 1)
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    token = _token(root)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        recovery._relaunch_paused_gateways(token, token['profiles'], token['unmapped'])
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv[0] == sys.executable
        assert 0 < kwargs['timeout'] <= 30
        assert Path(kwargs['cwd']) == root
        assert kwargs['env']['PYTHONPATH'].split(os.pathsep)[0] == str(root)
    assert token['profiles'] == {'school': 101}
    assert len(token['unmapped']) == 1
    assert 'Conflict-safe' in caplog.text
    assert f'reason={failure}' in caplog.text
    assert 'secret-launch' not in caplog.text


def test_stderr_is_bounded_and_raw_details_are_not_logged(snapshot, monkeypatch, caplog):
    root, _ = snapshot

    def run(argv, **kwargs):
        # Malformed vocabulary and unrelated helper output are not trusted diagnostics.
        kwargs['stderr'].write(b'{"reason": [], "stage": "private", "class": "secret"}\n')
        kwargs['stderr'].write(b'token=do-not-print\n' + b'x' * 1100)
        kwargs['stderr'].write(b'\n{"reason": "exception", "stage": "profile-helper", "class": "TypeError"}\n')
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(recovery.subprocess, 'run', run)
    with caplog.at_level(logging.WARNING):
        assert recovery._launch_conflict_safe_gateway_restart(str(root), {'profile': 'school', 'old_pid': 101}) is False
    assert 'reason=exit' in caplog.text
    assert 'TypeError' not in caplog.text, "Read exceeded the 1024-byte diagnostic bound"
    assert 'do-not-print' not in caplog.text and 'private' not in caplog.text


def test_normal_relaunch_does_not_create_recovery_subprocess(monkeypatch):
    from hermes_cli import gateway
    profile = Mock(return_value=True)
    cmdline = Mock(return_value=True)
    monkeypatch.setattr(gateway, 'launch_detached_profile_gateway_restart', profile)
    monkeypatch.setattr(gateway, 'launch_detached_gateway_restart_by_cmdline', cmdline)
    run = Mock(side_effect=AssertionError('normal path must not create subprocess'))
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    token = _token('unused')
    del token['restart_source_root']
    assert recovery._relaunch_paused_gateways(token, token['profiles'], token['unmapped']) == (['school'], 1)
    profile.assert_called_once_with('school', 101)
    cmdline.assert_called_once_with(202, ['python.exe', '-m', 'hermes_cli.main', 'gateway', 'run'])
    run.assert_not_called()
