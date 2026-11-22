"""A job created in a thread warns when its delivery silently loses that thread, but not when the user
wrote a thread-less ``platform:chat`` target on purpose (a deliberate top-level channel post)."""

import logging

import pytest

import cron.scheduler_delivery as sd
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import SendResult


class _Relay:
    def fronts_platform(self, _platform):
        return True

    async def send_for_platform(self, platform, chat_id, content, metadata=None):
        return SendResult(success=True, message_id="m1")


ORIGIN = {"platform": "discord", "chat_id": "thread-1", "thread_id": "thread-1"}


@pytest.mark.parametrize("resolved_from, warns", [("explicit", False), ("origin", True)])
def test_lost_thread_warning_only_for_non_deliberate_targets(caplog, resolved_from, warns):
    target = {"platform": "discord", "chat_id": "channel-1", "thread_id": None, "_resolved_from": resolved_from}
    with caplog.at_level(logging.WARNING, logger=sd.logger.name):
        sd._prepare_target_delivery(
            {"id": "job-1", "origin": ORIGIN, "deliver": "discord:channel-1"}, target,
            adapters={Platform.RELAY: _Relay()}, loop=None, config=GatewayConfig(), notify_delivery=True,
            mirror_enabled=False, mirror_text="", delivery_errors=[])
    assert any("delivery target lost it" in r.getMessage() for r in caplog.records) is warns
