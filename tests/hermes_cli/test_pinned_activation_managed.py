"""Real pinned activation through the selected-generation interpreter, within the per-file budget."""
import pytest

from tests.hermes_cli._pinned_activation_process import assert_whole_activation

pytest_plugins = ["tests.pm._pinned_fixtures", "tests.hermes_cli.test_update_completion_process"]


@pytest.mark.parametrize("mode", ["normal", "lost-result", "promotion-failure"])
def test_whole_activation(transition, candidate_owner, monkeypatch, mode):
    assert_whole_activation(transition, candidate_owner, monkeypatch, mode, "selected-generation")
