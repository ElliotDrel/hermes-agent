"""Regression tests for default-on Discord message-wide link-preview suppression (#91657).

``extra.suppress_link_previews`` sets ``SUPPRESS_EMBEDS`` on assistant text and captions; the
edit-path contract it relies on is documented once, under ``discord.suppress_link_previews`` in
``website/docs/user-guide/messaging/discord.md``.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter

MAX = DiscordAdapter.MAX_MESSAGE_LENGTH  # 2000


def _make_adapter(suppress_link_previews=None):
    extra = {}
    if suppress_link_previews is not None:
        extra["suppress_link_previews"] = suppress_link_previews
    return DiscordAdapter(PlatformConfig(enabled=True, token="***", extra=extra))


def _wire_channel(adapter, *, original_msg=None):
    """Fake client whose channel records every ``channel.send`` call (kwargs
    included) and returns ``original_msg`` from ``get_partial_message``."""
    sends = []

    async def fake_send(*, content, reference=None, suppress_embeds=False):
        sends.append({"content": content, "suppress_embeds": suppress_embeds})
        return SimpleNamespace(id=9000 + len(sends))

    channel = SimpleNamespace(
        id=555,
        get_partial_message=MagicMock(return_value=original_msg),
        send=AsyncMock(side_effect=fake_send),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    return channel, sends


# --------------------------------------------------------------------------- #
# Config coercion — default on, override off, string spellings
# --------------------------------------------------------------------------- #


class TestConfig:
    @pytest.mark.parametrize("raw", [None, True, "true", "yes", "on", "1"])
    def test_absent_null_and_truthy_values_suppress(self, raw):
        assert _make_adapter(raw)._suppress_link_previews is True

    def test_yaml_null_keeps_the_default(self):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***", extra={"suppress_link_previews": None}))
        assert adapter._suppress_link_previews is True

    @pytest.mark.parametrize("raw", [False, "false", "0", "no", "off", ""])
    def test_falsey_values_keep_previews(self, raw):
        assert _make_adapter(raw)._suppress_link_previews is False


# --------------------------------------------------------------------------- #
# Send path — the flag rides on channel.send per config
# --------------------------------------------------------------------------- #


class TestSendSuppression:
    @pytest.mark.asyncio
    async def test_send_suppresses_by_default(self):
        adapter = _make_adapter()
        _, sends = _wire_channel(adapter)
        result = await adapter.send("555", "see https://example.com for docs")
        assert result.success is True
        assert len(sends) == 1
        assert sends[0]["suppress_embeds"] is True

    @pytest.mark.asyncio
    async def test_send_keeps_previews_when_disabled(self):
        adapter = _make_adapter(False)
        channel, _ = _wire_channel(adapter)
        result = await adapter.send("555", "see https://example.com for docs")
        assert result.success is True
        assert "suppress_embeds" not in channel.send.await_args.kwargs

    @pytest.mark.asyncio
    async def test_overflow_continuations_carry_the_flag(self):
        # A finalized oversized edit splits into a first-chunk edit plus
        # continuation channel.send() calls — those continuations are part of
        # the same streamed reply and must stay suppressed too.
        adapter = _make_adapter(True)
        partial = SimpleNamespace(
            id=42,
            edit=AsyncMock(),
            to_reference=MagicMock(return_value=SimpleNamespace(kind="ref")),
        )
        _, sends = _wire_channel(adapter, original_msg=partial)
        result = await adapter.edit_message("555", "42", "link " * 1200, finalize=True)
        assert result.success is True
        assert sends, "expected overflow continuations"
        assert all(s["suppress_embeds"] is True for s in sends)


    @pytest.mark.asyncio
    async def test_forum_posts_and_media_captions_carry_the_flag(self, tmp_path, monkeypatch):
        adapter = _make_adapter(True)
        thread_channel = SimpleNamespace(id=7, send=AsyncMock(return_value=SimpleNamespace(id=8)))
        forum = SimpleNamespace(id=6, create_thread=AsyncMock(return_value=SimpleNamespace(thread=thread_channel)))
        await adapter._send_to_forum(forum, "see https://example.com\n" + "x" * (MAX + 10))
        assert forum.create_thread.await_args.kwargs["suppress_embeds"] is True
        assert thread_channel.send.await_args.kwargs["suppress_embeds"] is True

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"fake")
        sent = SimpleNamespace(id=9, attachments=[SimpleNamespace(filename="clip.mp4", url="https://cdn.example/c")])
        channel = SimpleNamespace(send=AsyncMock(return_value=sent), type=0)
        adapter._client = SimpleNamespace(get_channel=lambda _cid: channel, fetch_channel=AsyncMock())
        monkeypatch.setattr(adapter, "_is_forum_parent", lambda _ch: False)
        result = await adapter._send_file_attachment("555", str(clip), caption="see https://example.com")
        assert result.success is True
        assert channel.send.await_args.kwargs["suppress_embeds"] is True

    @pytest.mark.asyncio
    async def test_media_url_fallback_keeps_its_preview(self):
        # The base fallback posts the media URL as text; its preview card IS the media.
        adapter = _make_adapter()
        _channel, sends = _wire_channel(adapter)
        result = await adapter.send_image("555", "http://127.0.0.1/cat.png", caption="cat")
        assert result.success is True
        assert sends[-1]["content"].endswith("http://127.0.0.1/cat.png")
        assert sends[-1]["suppress_embeds"] is False


# --------------------------------------------------------------------------- #
# Edit path — the content-only edit never clears the flag
# --------------------------------------------------------------------------- #


class TestEditPreservesSuppression:
    @pytest.mark.asyncio
    async def test_streaming_edit_sends_no_suppress_or_flags_kwarg(self):
        """The streaming edit must not pass ``suppress``/``flags`` to
        ``PartialMessage.edit``.  Passing ``suppress=False`` (or ``flags`` with
        the bit cleared) is exactly what makes Discord regenerate the embed
        mid-stream; a content-only edit leaves the send-time suppression intact.
        """
        adapter = _make_adapter(True)
        edit_calls = []

        async def record_edit(**kwargs):
            edit_calls.append(kwargs)

        msg = SimpleNamespace(id=42, edit=AsyncMock(side_effect=record_edit))
        _wire_channel(adapter, original_msg=msg)

        result = await adapter.edit_message("555", "42", "streamed https://example.com")
        assert result.success is True
        assert len(edit_calls) == 1
        assert edit_calls[0] == {"content": "streamed https://example.com"}
        assert "suppress" not in edit_calls[0]
        assert "flags" not in edit_calls[0]
