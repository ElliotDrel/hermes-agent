"""Only descriptive assistant links lose previews, across accumulated stream edits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_discord_send import DiscordAdapter


@pytest.mark.asyncio
async def test_send_and_stream_edits_preserve_destinations_and_media():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    message = SimpleNamespace(id=42, edit=AsyncMock())
    channel = SimpleNamespace(id=555, send=AsyncMock(return_value=message), get_partial_message=lambda _: message)
    adapter._client = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock())
    source = "[Read [details]](https://example.com/a_(b)?q=1#part) https://example.com/bare [clip](https://example.com/a.mp4)"
    expected = source.replace("](https://example.com/a_(b)?q=1#part)", "](<https://example.com/a_(b)?q=1#part>)")
    assert (await adapter.send("555", source)).success
    assert channel.send.await_args.kwargs["content"] == expected
    for snapshot in ("[Read [details]](https://example.com/a_(", source):
        assert (await adapter.edit_message("555", "42", snapshot)).success
        assert message.edit.await_args.kwargs["content"] == (expected if snapshot == source else snapshot)
    assert (await adapter.edit_message("555", "42", source, finalize=True)).success
    assert message.edit.await_args.kwargs == {"content": expected}
    assert source.startswith("[Read [details]](https://"), "source/history content was changed"


@pytest.mark.parametrize("protected", [
    "`[code](https://example.com/x)`", "``[code](https://example.com/x)``",
    "```python\n[code](https://example.com/x)\n```", "~~~\n[code](https://example.com/x)\n~~~",
    "```\n[incomplete](https://example.com/x)", "`[incomplete](https://example.com/x)",
    r"\[escaped](https://example.com/x)", "![image](https://example.com/x)",
    "[https://example.com/x](https://example.com/x)", "[example.com/x](https://example.com/x)",
    "[photo](https://example.com/x.png?q=1)", '[title](https://example.com/x "Title")',
    "[already](<https://example.com/x>)", "https://example.com/x",
    "<https://example.com/[label](https://other.example/x)>",
    "https://example.com/[label](https://other.example/x)",
    "[" * 20_000 + "unfinished", "[" * 20_000 + "]" * 20_000,
    "[a](https://example.com/" + "[" * 20_000,

], ids=lambda text: f"protected-{len(text)}-{text[:12]}")
def test_formatter_preserves_protected_markdown_and_is_idempotent(protected):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    assert adapter.format_message(protected) == protected
    rendered = adapter.format_message("[docs](https://example.com/docs)")
    assert rendered == "[docs](<https://example.com/docs>)"
    assert adapter.format_message(rendered) == rendered
