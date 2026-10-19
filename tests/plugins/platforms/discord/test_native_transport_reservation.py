"""A trusted secondary callback reserves its own DB with explicit transport provenance."""
import asyncio
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_native_callback_reservation_preserves_secondary_transport(monkeypatch, tmp_path):
    import hermes_state
    from gateway.config import load_gateway_config, Platform
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.waiting import async_wait_until

    root = tmp_path / '.hermes'
    beta = root / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    for home in (root, beta):
        (home / 'config.yaml').write_text('discord:\n  allow_from: ["42"]\ngateway:\n  multiplex_profiles: true\nplatforms:\n  discord:\n    enabled: true\n    token: synthetic-token\n', encoding='utf-8')
    process_ownership.reserve([root, beta])
    runner = None
    try:
        with _profile_runtime_scope(root, {}):
            config = load_gateway_config()
            config._runtime_profile_homes = (('default', root), ('beta', beta))
            runner = GatewayRunner(config)
            await initialize_gateway_runtime(runner)
            first = DiscordAdapter(config.platforms[Platform.DISCORD])
        with _profile_runtime_scope(beta, {}):
            second = DiscordAdapter(load_gateway_config().platforms[Platform.DISCORD])
        first.gateway_runner = second.gateway_runner = runner
        second.set_owner_profile('beta')
        runner.adapters = {Platform.DISCORD: first}
        runner._profile_adapters = {'beta': {Platform.DISCORD: second}}
        runner._wire_adapter_handlers(second, busy_text_mode='queue',
            message_handler=runner._make_profile_message_handler('beta'),
            busy_session_handler=runner._make_profile_busy_session_handler('beta'),
            authorization_check=runner._make_adapter_auth_check(Platform.DISCORD, 'beta'))
        delivered = []
        async def turn(event, source, key, generation):
            delivered.append(source)
            return 'confirmed native turn'
        async def send(*args, **kwargs):
            return SendResult(success=True, message_id='synthetic')
        async def noop(*args, **kwargs):
            pass
        monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
        monkeypatch.setattr(second, 'send', send)
        monkeypatch.setattr(second, 'send_typing', noop)
        monkeypatch.setattr(second, 'stop_typing', noop)
        with _profile_runtime_scope(beta, {}):
            source = second.build_source(chat_id='secondary', user_id='42')
            await second.handle_message(MessageEvent(text='actual callback', message_id='secondary-human', source=source))
        await async_wait_until(lambda: delivered, timeout=10, what='secondary native callback')
        authority = runner.session_authorities.for_home(beta)
        with authority.db._read_ctx() as conn:
            rows = conn.execute("SELECT origin_json,transport_profile,user_id FROM sessions WHERE source='discord'").fetchall()
        assert len(rows) == 1
        assert rows[0]['transport_profile'] == 'beta'
        assert rows[0]['user_id'] == '42'
        assert json.loads(rows[0]['origin_json'])['profile'] == 'beta'
        with runner.session_authorities.for_home(root).db._read_ctx() as conn:
            assert conn.execute("SELECT count(*) FROM sessions WHERE source='discord'").fetchone()[0] == 0
    finally:
        if runner is not None:
            for adapter in [*runner.adapters.values(), *getattr(runner, '_profile_adapters', {}).get('beta', {}).values()]:
                tasks = [task for task in adapter._background_tasks if not task.done()]
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            for authority in runner.session_authorities:
                authority.db.close()
        process_ownership.release(beta)
        process_ownership.release(root)
