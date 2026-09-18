from unittest.mock import MagicMock, patch

from agent.title_generator import (
    MAX_REGENERATED_TITLE_CONTEXT_CHARS,
    format_regenerated_title_context,
    generate_regenerated_title,
)


def test_regenerated_title_context_labels_user_and_assistant_turns():
    context = format_regenerated_title_context([
        {"role": "system", "content": "ignore this"},
        {"role": "user", "content": "Fix reconnect behavior"},
        {"role": "assistant", "content": "The stale websocket is the cause."},
        {"role": "tool", "content": "ignore this tool output"},
    ])

    assert context == (
        "USER:\nFix reconnect behavior\n\n"
        "ASSISTANT:\nThe stale websocket is the cause."
    )


def test_regenerated_title_context_pins_opening_user_goal_when_truncated():
    context = format_regenerated_title_context([
        {"role": "user", "content": "Review subagent monitoring risks. " + "opening " * 400},
        {"role": "assistant", "content": "middle " * 200},
        {"role": "user", "content": "Latest finding: " + "detail " * 1_200},
    ])

    assert context.startswith("USER:\nReview subagent monitoring risks.")
    assert "[First user message truncated]" in context
    assert "[Earlier content truncated]" in context
    assert context.endswith("detail " * 4 + "detail")
    assert len(context) <= MAX_REGENERATED_TITLE_CONTEXT_CHARS


def test_generate_regenerated_title_includes_previous_title_and_history():
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = '{"title":"Fix Reconnect State"}'
    history = [
        {"role": "user", "content": "Fix reconnect state after a restart."},
        {"role": "assistant", "content": "The stale connection is retained."},
    ]

    with patch("agent.title_generator.call_llm", return_value=response) as call_llm:
        title = generate_regenerated_title(history, "Investigate Reconnect Regressions")

    assert title == "Fix Reconnect State"
    request = call_llm.call_args.kwargs["messages"]
    assert '"Investigate Reconnect Regressions"' in request[0]["content"]
    assert request[1]["content"] == (
        "USER:\nFix reconnect state after a restart.\n\n"
        "ASSISTANT:\nThe stale connection is retained."
    )
