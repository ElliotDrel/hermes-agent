"""Opt-in title callback keeps the manual thread initial-name provenance across restart."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from gateway.config import Platform, GatewayConfig, load_gateway_config
from gateway.session import SessionSource, SessionStore
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.platforms.discord.adapter import DiscordAdapter

@pytest.mark.asyncio
async def test_manual_title_native_callback_optin_guard_and_durable_pin(tmp_path):
    scope=set_hermes_home_override(tmp_path)
    store=None
    try:
        store=SessionStore(tmp_path/"sessions",GatewayConfig())
        source=SessionSource(platform=Platform.DISCORD,chat_id="123",thread_id="123",chat_type="thread",user_id="42")
        entry=store.get_or_create_session(source)
        scheduled=[]
        cached=SimpleNamespace(name="Human opening")
        remote=SimpleNamespace(name="Human opening",edit=AsyncMock())
        homes=[tmp_path/"profile-a",tmp_path/"profile-b"]
        for home,enabled in zip(homes,[True,False]):
            home.mkdir()
            (home/"config.yaml").write_text("platforms:\n  discord:\n    enabled: true\ndiscord:\n  auto_title_manual_threads: "+str(enabled).lower()+"\n",encoding="utf-8")
        for home,enabled in [(homes[0],True),(homes[1],False),(homes[0],True)]:
            profile_scope=set_hermes_home_override(home)
            try:
                config=load_gateway_config()
            finally:
                reset_hermes_home_override(profile_scope)
            adapter=DiscordAdapter(config.platforms[Platform.DISCORD])
            adapter._client=SimpleNamespace(get_channel=lambda cid:cached,fetch_channel=AsyncMock(return_value=remote))
            runner=GatewayRunner.__new__(GatewayRunner)
            runner.session_store=store
            runner.adapters={Platform.DISCORD:adapter}
            runner._delivery_adapter_for=lambda src:adapter
            runner._schedule_discord_semantic_thread_rename=lambda src,sid,title:scheduled.append((src,sid,title))
            holder=SimpleNamespace(_runner=runner)
            agent=SimpleNamespace(session_id=entry.session_id)
            TurnRunner._attach_session_title_callback(holder,agent,SimpleNamespace(source=source,session_key=entry.session_key))
            callback=getattr(agent,"_on_session_title",None)
            assert callable(callback) is enabled
            if not enabled:
                continue
            callback("Derived title","derived")
            assert not scheduled
            callback("Model title","llm")
            captured,sid,title=scheduled.pop()
            assert captured.auto_thread_initial_name=="Human opening" and not captured.auto_thread_created
            # A human edit while the auxiliary call was running wins over stale SDK cache.
            remote.name="Human changed"
            await runner._rename_discord_auto_thread_for_session_title(captured,sid,title)
            remote.edit.assert_not_awaited()
            # Retry and process restart retain the original pin, even with a newer SDK cache.
            store.close_all_db_handles()
            store=SessionStore(tmp_path/"sessions",GatewayConfig())
            runner.session_store=store
            cached.name="Human changed"
            prepared=runner._prepare_discord_manual_title_source(source,entry.session_key)
            assert prepared.auto_thread_initial_name=="Human opening"
            await runner._rename_discord_auto_thread_for_session_title(prepared,sid,title)
            remote.edit.assert_not_awaited()
            # Unchanged remote name is eligible; repeated same title requires no further edit.
            remote.name="Human opening"
            async def edit(**kwargs):
                remote.name=kwargs["name"]
                return remote
            remote.edit.side_effect=edit
            await runner._rename_discord_auto_thread_for_session_title(prepared,sid,title)
            assert remote.edit.await_count==1
            await runner._rename_discord_auto_thread_for_session_title(prepared,sid,title)
            assert remote.edit.await_count==1
            adapter._client.fetch_channel.side_effect=RuntimeError("unreadable")
            assert not await adapter.rename_thread("123","Another",only_if_current_name="Human opening")
            assert remote.edit.await_count==1
            cached.name="Human opening"
            remote.name="Human opening"
            remote.edit.reset_mock()
    finally:
        if store is not None: store.close_all_db_handles()
        reset_hermes_home_override(scope)
