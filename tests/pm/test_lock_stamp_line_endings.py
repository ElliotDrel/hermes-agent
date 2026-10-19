"""A published Git lock and its Windows checkout represent the same PM graph."""
from pm.packages import Venv


def test_archive_and_windows_checkout_line_endings_share_dependency_stamp(tmp_path):
    archive, checkout = tmp_path / "archive", tmp_path / "checkout"
    archive.mkdir()
    checkout.mkdir()
    raw = b'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n'
    (archive / "uv.lock").write_bytes(raw)
    (checkout / "uv.lock").write_bytes(raw.replace(b"\n", b"\r\n"))
    assert Venv(archive).expected_stamp([], plugin_dirs=[]) == Venv(checkout).expected_stamp([], plugin_dirs=[])
