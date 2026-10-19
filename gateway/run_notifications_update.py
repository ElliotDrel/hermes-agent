"""``hermes update --gateway`` progress/completion notifications for GatewayRunner.

Split out of ``gateway/run_notifications.py``; ``GatewayNotificationsMixin`` inherits it, so
these stay bound onto ``GatewayRunner`` via the MRO. ``gateway.run`` internals are imported
lazily inside method bodies (import cycle), so ``patch("gateway.run.X")`` keeps intercepting them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from agent.i18n import t
from gateway.config import Platform
from gateway.run_shutdown import _log_suppressed

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# A failed /update leaves the previous version running; the full pip/git log stays on the host
# (`hermes update` re-runs it in the terminal) and only a short tail is quoted in chat.
def _update_failed_notice() -> str:
    return t("gateway.update.failed_notice")

def _update_output_tail(output: str, limit: int) -> str:
    """Last ``limit`` chars of an update log, prefixed with an ellipsis when cut."""
    return output if len(output) <= limit else "…" + output[-limit:]


class GatewayUpdateNotificationsMixin:
    """Update watcher: streams ``hermes update --gateway`` output, forwards prompts, sends the final notice."""

    @dataclasses.dataclass
    class _UpdatePaths:
        """Marker files ``hermes update --gateway`` and its watcher exchange under HERMES_HOME."""

        pending: Path
        claimed: Path
        output: Path
        exit_code: Path
        prompt: Path
        response: Path

        def any_pending(self) -> bool:
            return self.pending.exists() or self.claimed.exists()

    @dataclasses.dataclass
    class _UpdateTarget:
        """Resolved delivery target for update watcher messages."""

        adapter: Any
        chat_id: Any
        session_key: Optional[str]
        metadata: Any
        platform: Any
        run_id: str = ""  # invocation correlation; empty for legacy home-root markers

        def send_metadata(self):
            from gateway.run import _non_conversational_metadata
            return _non_conversational_metadata(self.metadata, platform=self.platform)

        async def send(self, text: str):
            return await self.adapter.send(self.chat_id, text, metadata=self.send_metadata())

    def _schedule_update_notification_watch(self) -> None:
        """One watcher per pending invocation, including receipts left by a previous process."""
        tasks = getattr(self, "_update_notification_tasks", None)
        if tasks is None:
            tasks = self._update_notification_tasks = {}
        for key in list(tasks):
            if tasks[key].done():
                del tasks[key]
        for paths in self._pending_update_paths():
            key = str(paths.pending)
            if key not in tasks or tasks[key].done():
                tasks[key] = asyncio.create_task(self._watch_update_progress(paths=paths))

    @classmethod
    def _update_paths(cls, home=None) -> GatewayUpdateNotificationsMixin._UpdatePaths:
        from gateway.run import _hermes_home
        home = _hermes_home if home is None else home
        return cls._UpdatePaths(
            pending=home / ".update_pending.json", claimed=home / ".update_pending.claimed.json",
            output=home / ".update_output.txt", exit_code=home / ".update_exit_code",
            prompt=home / ".update_prompt.json", response=home / ".update_response",
        )

    def _pending_update_paths(self):
        from gateway.run import _hermes_home
        from hermes_cli.update_gateway_io import pending_run_homes
        paths = [self._update_paths(), *(self._update_paths(home) for home in pending_run_homes(_hermes_home))]
        return [item for item in paths if item.any_pending()]

    @classmethod
    def _marker_transport_profile(cls, data: dict) -> Optional[str]:
        """The bot that RECEIVED the request (``transport_profile``) answers it, as
        ``_delivery_adapter_for`` does live; markers without one fall back to the owning profile."""
        return str(data.get("transport_profile") or "").strip() or cls._marker_profile(data)

    @staticmethod
    def _marker_profile(data: dict) -> Optional[str]:
        """Owning profile of a persisted restart/update marker: explicit ``profile``, else the
        ``agent:<profile>:`` lane of its ``session_key`` (markers written before ``profile`` was
        persisted); ``None`` = default profile."""
        profile = str(data.get("profile") or "").strip()
        if profile:
            return profile
        from gateway.session import profile_from_session_key_namespace
        parts = str(data.get("session_key") or "").split(":")
        if len(parts) >= 5 and parts[0] == "agent" and parts[1] not in ("main", ""):
            return profile_from_session_key_namespace(parts[1])
        return None

    def _resolve_update_target(self, paths: _UpdatePaths) -> Optional[_UpdateTarget]:
        """Resolve adapter/chat/session for update watcher messages from the pending marker."""
        for path in (paths.claimed, paths.pending):
            if not path.exists():
                continue
            with suppress(Exception):
                pending = json.loads(path.read_text(encoding="utf-8-sig"))
                platform_str = pending.get("platform")
                chat_id = pending.get("chat_id")
                session_key = pending.get("session_key")
                if not (platform_str and chat_id):
                    continue  # BASE: an incomplete marker falls through to the next path, not "unresolved"
                platform = Platform(platform_str)
                # The requester's OWN profile bot (marker ``profile``, else the ``agent:<profile>:`` key
                # lane); a bare self.adapters lookup is the default bot under multiplex.
                adapter = self._authorization_adapter(platform, self._marker_transport_profile(pending))
                if not adapter:
                    return None
                metadata = self._pending_marker_metadata(platform, chat_id, pending, adapter)
                # Fallback session key if not stored (old pending files)
                return self._UpdateTarget(
                    adapter, chat_id, session_key or f"{platform_str}:{chat_id}", metadata, platform,
                    str(pending.get("run_id") or ""),
                )
        return None

    def _pending_marker_metadata(self, platform, chat_id, data: dict, adapter):
        """Thread metadata for a persisted update/restart marker (thread_id/chat_type/message_id keys)."""
        return self._thread_metadata_for_target(
            platform, chat_id, data.get("thread_id"), chat_type=data.get("chat_type"),
            reply_to_message_id=data.get("message_id"), adapter=adapter,
        )

    async def _watch_update_completion_only(self, paths: _UpdatePaths, deadline: float, poll_interval: float) -> None:
        """Fallback when no adapter/chat can be resolved: wait for the exit code, then notify."""
        logger.warning("Update watcher: cannot resolve adapter/chat_id, falling back to completion-only")
        # Poll until _send_update_notification delivers (it returns False while the platform reconnects).
        loop = asyncio.get_running_loop()
        while paths.any_pending() and loop.time() < deadline:
            if paths.exit_code.exists() and await self._send_update_notification(paths):
                return
            await asyncio.sleep(poll_interval)
        if paths.any_pending() and not paths.exit_code.exists():
            logger.warning("Update reporting wait expired; retaining the pending invocation")

    @staticmethod
    def _update_exit_code(paths: _UpdatePaths) -> int:
        return int(paths.exit_code.read_text(encoding="utf-8-sig").strip() or "1")

    @staticmethod
    def _read_update_output_since(path: Path, offset: int) -> tuple[str, int]:
        """Read update output defensively; logs may contain invalid UTF-8."""
        try:
            data = path.read_bytes()
        except OSError:
            return "", offset
        if len(data) <= offset:
            return "", len(data)
        return data[offset:].decode("utf-8", errors="replace"), len(data)

    async def _send_update_output(self, target: _UpdateTarget, text: str) -> None:
        """Send buffered update output as fenced chunks that fit message limits (Telegram: 4096)."""
        from tools.ansi_strip import strip_ansi
        clean = strip_ansi(text).strip()
        if not clean:
            return
        max_chunk = 3500
        for i in range(0, len(clean), max_chunk):
            with _log_suppressed(logging.DEBUG, "Update stream send failed: %s"):
                await target.send(f"```\n{clean[i:i + max_chunk]}\n```")

    async def _forward_update_prompt(self, target: _UpdateTarget, prompt_text: str, default: str) -> None:
        """Forward an update prompt: platform-native buttons first (Discord, Telegram), else text.

        Buttons carry ``metadata["update_run_id"]`` so their answer reaches the invocation that asked.
        """
        sent_buttons = False
        adapter = target.adapter
        if getattr(type(adapter), "send_update_prompt", None) is not None:
            metadata = dict(target.send_metadata() or {})
            if target.run_id:
                metadata["update_run_id"] = target.run_id
            with _log_suppressed(logging.DEBUG, "Button-based update prompt failed: %s"):
                result = await adapter.send_update_prompt(
                    chat_id=target.chat_id, prompt=prompt_text, default=default,
                    session_key=target.session_key, metadata=metadata,
                )
                sent_buttons = getattr(result, "success", None) is True
        if not sent_buttons:
            default_hint = t("gateway.update.prompt_default", default=default) if default else ""
            _p = getattr(adapter, "typed_command_prefix", "/")
            result = await target.send(t("gateway.update.prompt", prompt=prompt_text, default_hint=default_hint, prefix=_p))
            if getattr(result, "success", None) is not True:
                return
        # Keep the prompt marker on disk until answered so a restarted watcher can re-forward it.
        state = self._session_state(target.session_key).persistent
        state.update_prompt_pending = True
        state.update_prompt_run_id = target.run_id
        logger.info("Forwarded update prompt to %s: %s", target.session_key, prompt_text[:80])

    def _clear_update_prompt_state(self, session_key: Optional[str], run_id: str) -> None:
        """Release the chat from prompt-reply mode, unless another invocation's prompt owns it."""
        state = self._peek_session_state(session_key) if session_key else None
        if state is not None and getattr(state.persistent, "update_prompt_run_id", "") == run_id:
            state.persistent.update_prompt_pending = False
            state.persistent.update_prompt_run_id = ""

    async def _watch_update_progress(
        self, poll_interval: float = 2.0, stream_interval: float = 4.0, timeout: float = 1800.0, *, paths=None
    ) -> None:
        """Watch ``hermes update --gateway``, streaming output + forwarding prompts.

        Polls ``.update_output.txt`` for new content and sends chunks to the user periodically;
        detects ``.update_prompt.json`` (written when the update process needs input) and forwards it.
        """
        paths = paths or self._update_paths()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        target = self._resolve_update_target(paths)
        if target is None:
            await self._watch_update_completion_only(paths, deadline, poll_interval)
            return
        session_key = target.session_key
        bytes_sent = 0
        last_stream_time = loop.time()
        buffer = ""

        async def _flush_buffer() -> None:
            nonlocal buffer, last_stream_time
            text, buffer = buffer, ""
            if text.strip():
                last_stream_time = loop.time()
                await self._send_update_output(target, text)

        def _read_new_output() -> None:
            nonlocal buffer, bytes_sent
            if paths.output.exists():
                with suppress(OSError):
                    chunk, bytes_sent = self._read_update_output_since(paths.output, bytes_sent)
                    buffer += chunk

        retry_delay = poll_interval
        while loop.time() < deadline:
            if paths.exit_code.exists():
                _read_new_output()
                await _flush_buffer()
                if await self._send_update_notification(paths, include_output=False):
                    return
                # Undelivered: retry with backoff; the receipt stays on disk for the next start.
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)
                continue
            _read_new_output()
            if buffer.strip() and (loop.time() - last_stream_time) >= stream_interval:
                await _flush_buffer()
            # Forward a prompt only when none is pending, else every poll re-forwards the same prompt.
            _pending_state = self._peek_session_state(session_key) if session_key else None
            if paths.prompt.exists() and session_key and not getattr(
                getattr(_pending_state, "persistent", None), "update_prompt_pending", False
            ):
                try:
                    prompt_data = json.loads(paths.prompt.read_text(encoding="utf-8-sig"))
                    prompt_text = prompt_data.get("prompt", "")
                    if prompt_text:
                        await _flush_buffer()  # user sees context before the prompt
                        await self._forward_update_prompt(target, prompt_text, prompt_data.get("default", ""))
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read update prompt: %s", e)
            await asyncio.sleep(poll_interval)
        if not paths.exit_code.exists():
            logger.warning("Update watcher timed out after %.0fs", timeout)
            await _flush_buffer()
            with suppress(Exception):
                await target.send(t("gateway.update.timed_out"))
            # A reporting timeout is not an updater exit: no invented code, and the pending
            # invocation stays on disk for the next watcher or gateway start. The chat stops
            # treating replies as answers to this invocation's prompt.
            self._clear_update_prompt_state(session_key, target.run_id)

    @staticmethod
    def _update_report_text(exit_code: int, output: str) -> str:
        from tools.ansi_strip import strip_ansi
        output = strip_ansi(output).strip()
        if exit_code == 0:
            msg = t("gateway.update.finished_success")
            return f"{msg}\n\n```\n{_update_output_tail(output, 3500)}\n```" if output else msg
        msg = _update_failed_notice()
        return t("gateway.update.last_lines", msg=msg, tail=_update_output_tail(output, 800)) if output else msg

    def _consume_delivered_update(self, paths: _UpdatePaths, pending: dict) -> None:
        run_id = str(pending.get("run_id") or "")
        # A correlated invocation keeps its log, exit code and delivery receipt for diagnosis;
        # legacy home-root markers are shared, so they are consumed whole.
        cleared = (paths.prompt, paths.response) if run_id else (paths.output, paths.exit_code)
        for p in (paths.claimed, *cleared):
            p.unlink(missing_ok=True)
        self._clear_update_prompt_state(pending.get("session_key"), run_id)
        if run_id:
            from gateway.run import _hermes_home
            from hermes_cli.update_gateway_io import prune_delivered_runs
            prune_delivered_runs(_hermes_home)

    async def _send_update_notification(self, paths=None, *, include_output=True) -> bool:
        """If an update finished, notify the user.

        False while the update is still running or its report was not accepted (caller may
        retry; the invocation stays pending); True after a confirmed delivery or nothing to send.
        """
        if paths is None:
            outstanding = self._pending_update_paths()
            if not outstanding:
                return False
            results = [await self._send_update_notification(item) for item in outstanding]
            return all(results)
        if not paths.any_pending():
            return False

        def _defer(reason: str, *args) -> bool:
            logger.info(reason, *args)
            paths.claimed.replace(paths.pending)
            return False

        try:
            if paths.pending.exists():
                try:
                    paths.pending.replace(paths.claimed)
                except FileNotFoundError:
                    if not paths.claimed.exists():
                        return True
            elif not paths.claimed.exists():
                return True
            pending = json.loads(paths.claimed.read_text(encoding="utf-8-sig"))
            platform_str = pending.get("platform")
            chat_id = pending.get("chat_id")
            if not paths.exit_code.exists():
                return _defer("Update notification deferred: update still running")
            exit_code = self._update_exit_code(paths)
            output = paths.output.read_bytes().decode("utf-8", errors="replace") if paths.output.exists() else ""
            platform = Platform(platform_str)
            adapter = self._authorization_adapter(platform, self._marker_transport_profile(pending))
            if not chat_id:
                return _defer("Update notification deferred: missing destination")
            if not adapter:
                return _defer("Update notification deferred: %s adapter not connected yet", platform_str)
            metadata = self._pending_marker_metadata(platform, chat_id, pending, adapter)
            msg = self._update_report_text(exit_code, output if include_output else "")
            from gateway.update_report_delivery import deliver_report
            if not await deliver_report(self, pending, adapter, platform, msg, metadata, paths.pending.parent):
                return _defer("Update notification deferred: delivery failed for %s:%s", platform_str, chat_id)
            logger.info("Sent post-update notification to %s:%s (exit=%s)", platform_str, chat_id, exit_code)
            self._consume_delivered_update(paths, pending)
        except Exception as e:
            logger.warning("Post-update notification failed: %s", e)
            return False
        return True
