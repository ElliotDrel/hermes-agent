"""Regression: regex backslashes must survive shell quoting on Windows.

``ShellFileOperations._escape_shell_arg`` routes its argument through
``_bash_safe_path``, which is a PATH translator: on Windows it rewrites
every backslash to a forward slash so bash does not eat ``\\U`` in a
native path like ``C:\\Users\\x``. That is correct for paths.

It is wrong for the search *pattern*, which is a regex, not a path. A
pattern such as ``def register\\(`` is rewritten to ``def register/(``
before it reaches ripgrep, which then fails to parse it:

    rg: regex parse error:
        (?:def register/()
        ^
    error: unclosed group

Real production impact: a scheduled Hermes audit cron issued
``(?:PluginManager\\(|_create_adapter\\(|platform_reconnect_watcher)`` and
got ``error: unclosed group`` back, producing no report at all. Every
``\\(``, ``\\d``, ``\\b``, ``\\s``, ``\\.`` and ``\\|`` in any content or
zero-match-probe search is silently corrupted on this host.

The fix quotes patterns with a path-agnostic shell quoter that leaves
backslashes intact, keeping ``_escape_shell_arg`` for real paths.
"""

import shutil

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


def _ops(root):
    return ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))


@pytest.fixture
def regex_tree(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def register(app):\n"
        "    return 1\n"
        "def register_late(app):\n"
        "    return 2\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_escaped_paren_pattern_is_not_path_translated(regex_tree):
    """``def register\\(`` must match, not blow up as ``def register/(``."""
    ops = _ops(regex_tree)
    result = ops.search(r"def register\(", path=str(regex_tree), target="content")

    assert result.error is None, result.error
    assert result.total_count == 1, result
    assert "def register(app):" in result.matches[0].content


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_alternation_group_with_escaped_parens_survives(regex_tree):
    """The exact production pattern shape that returned 'unclosed group'."""
    ops = _ops(regex_tree)
    result = ops.search(
        r"register\(|register_late\(", path=str(regex_tree), target="content"
    )

    assert result.error is None, result.error
    assert result.total_count == 2, result


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_backslash_d_class_survives(tmp_path):
    """``\\d+`` must stay a digit class, not become ``/d+``."""
    (tmp_path / "nums.txt").write_text("value 42\nvalue x\n", encoding="utf-8")
    ops = _ops(tmp_path)
    result = ops.search(r"value \d+", path=str(tmp_path), target="content")

    assert result.error is None, result.error
    assert result.total_count == 1, result


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_file_glob_backslash_is_not_translated(tmp_path):
    """A --glob value is not a path either; it must not be rewritten."""
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    ops = _ops(tmp_path)
    result = ops.search(
        "needle", path=str(tmp_path), target="content", file_glob="*.py"
    )

    assert result.error is None, result.error
    assert result.total_count == 1, result
