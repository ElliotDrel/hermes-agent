"""Regenerated titles use conversation text, never injected skill bodies."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import title_generator as titles
from agent.skill_commands import _build_skill_message


def _auto_skill(original, *, recorded=True):
    body = _build_skill_message(
        {"content": "SKILL_BODY_ONLY " + "s" * 12000}, None,
        '[IMPORTANT: The "coding-prefs" skill is auto-loaded. Follow its instructions for this session.]',
    )
    row = {"role": "user", "content": body + "\n\n" + original}
    if recorded:
        row["display_metadata"] = {"title_user_message": original}
    return row


@pytest.mark.parametrize("kind", ["auto", "auto-reloaded", "empty-auto", "slash", "slash-sender", "bare-slash", "legacy", "legacy-sender", "legacy-backfill", "ordinary", "assistant"])
def test_title_request_omits_skill_bodies_and_never_mutates_history(kind, tmp_path):
    original = "Set Up Universal Spell Check"
    if kind in {"auto", "auto-reloaded", "empty-auto"}:
        row = _auto_skill("" if kind == "empty-auto" else original)
        expected = None if kind == "empty-auto" else original
        if kind == "auto-reloaded":
            from hermes_state import SessionDB
            path = tmp_path / "sessions.db"
            db = SessionDB(db_path=path)
            db.create_session("rename-test", source="discord")
            db.append_message("rename-test", **row)
            db.close()
            db = SessionDB(db_path=path)
            try:
                row = db.get_messages("rename-test", include_compacted=True)[0]
            finally:
                db.close()
    elif kind in {"slash", "slash-sender", "bare-slash"}:
        row = {"role": "user", "content": _build_skill_message(
            {"content": "SKILL_BODY_ONLY " + "s" * 12000}, None,
            '[IMPORTANT: The user has invoked the "coding-prefs" skill. The full skill content is loaded below.]',
            user_instruction="" if kind == "bare-slash" else original,
        )}
        if kind == "slash-sender":
            row["content"] = "[Elliot] " + row["content"]
        expected = None if kind == "bare-slash" else original
    elif kind.startswith("legacy"):
        row = _auto_skill(original, recorded=False)
        if kind == "legacy-sender":
            row["content"] = "[Elliot] " + row["content"]
        elif kind == "legacy-backfill":
            row["content"] = "Earlier channel messages\n\n[New message]\n[Elliot] " + row["content"]
        expected = None  # Old auto-loads have no reliable boundary to recover the opener.
    elif kind == "assistant":
        row = {"role": "assistant", "content": "Review the spell-check API."}
        expected = row["content"]
    else:
        row = {"role": "user", "content": "Explain why coding-prefs is auto-loaded."}
        expected = row["content"]
    history = [row, {"role": "user", "content": "Keep spell checking local."}]
    before = copy.deepcopy(history)
    context = titles.format_regenerated_title_context(history)
    assert "SKILL_BODY_ONLY" not in context
    assert "[IMPORTANT:" not in context
    assert "Keep spell checking local." in context
    if expected:
        assert expected in context
    else:
        assert original not in context
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"title":"Set Up Local Spell Checking"}'))])
    with patch.object(titles, "call_llm", return_value=response) as call:
        assert titles.generate_regenerated_title(history, "Old Title") == "Set Up Local Spell Checking"
    assert call.call_args.kwargs["task"] == "title_generation"
    assert call.call_args.kwargs["messages"][1]["content"] == context
    assert "SKILL_BODY_ONLY" not in json.dumps(call.call_args.kwargs["messages"])
    assert history == before


def test_rename_takes_ten_thousand_characters_preserving_opener_and_recent_turn():
    opener = "Keep Universal Spell Check as the topic. " + "o" * 3000
    recent = "Current follow-up " + "r" * 12000 + " LAST_TURN_END"
    history = [
        _auto_skill(opener),
        {"role": "assistant", "content": "middle " * 2000},
        {"role": "user", "content": recent},
    ]
    before = copy.deepcopy(history)
    context = titles.format_regenerated_title_context(history)
    assert len(context) == 10000
    assert context.startswith("USER:\nKeep Universal Spell Check as the topic.")
    assert "[First user message truncated]" in context
    assert "[Earlier content truncated]" in context
    assert context.endswith(" LAST_TURN_END")
    assert "SKILL_BODY_ONLY" not in context
    assert history == before
    assert len(titles.build_title_input("x" * 12000)) == 5000
