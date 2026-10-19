"""Runtime metadata is presentation only, including native Discord final edits."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import discord
from gateway.config import Platform,PlatformConfig
from gateway.session import SessionSource
from gateway.platforms.base import _thread_metadata_for_event
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from gateway.run_turn_runner import _resolve_runtime_footer_metadata
from gateway.runtime_footer import build_footer_line
from plugins.platforms.discord.adapter import DiscordAdapter

@pytest.mark.asyncio
async def test_native_metadata_send_edit_stream_and_profile_snapshots(tmp_path,monkeypatch):
    cfg={"display":{"runtime_footer":{"enabled":True,"fields":["provider","model","compaction_trigger","compactions"]}}}
    class CachedCompressor:
        _threshold_tokens=100
        @property
        def threshold_tokens(self): raise AssertionError("lazy threshold on delivery")
    for provider,model,count in [("a","model-a",2),("b","model-b",5),("a","model-a",2)]:
        comp=CachedCompressor();comp._threshold_tokens=100
        agent=SimpleNamespace(provider=provider,model=model,context_compressor=comp,session_id="s",_session_db=SimpleNamespace(get_session_model_config_value=lambda *a:count))
        snapshot=_resolve_runtime_footer_metadata(agent,cfg,"discord")
        footer=build_footer_line(user_config={},platform_key="discord",model=model,context_tokens=125,context_length=200,**{k:v for k,v in snapshot.items() if k in ("provider","compaction_trigger_tokens","compaction_count")},resolved_config=snapshot["footer_config"])
        assert "compact 125%" in footer and f"compactions {count}" in footer and model in footer
    unknown=build_footer_line(user_config=cfg,platform_key="discord",model=None,context_tokens=125,context_length=None)
    assert "compact" not in unknown
    adapter=DiscordAdapter(PlatformConfig(enabled=True))
    sent=[]
    async def send(**kwargs):
        sent.append(kwargs);return SimpleNamespace(id=len(sent))
    existing=discord.Embed(description="Keep existing media")
    partial=SimpleNamespace(edit=AsyncMock(),fetch=AsyncMock(return_value=SimpleNamespace(embeds=[existing])))
    channel=SimpleNamespace(id=123,send=send,get_partial_message=lambda mid:partial)
    adapter._client=SimpleNamespace(user=SimpleNamespace(id=9))
    adapter._resolve_channel=AsyncMock(return_value=channel)
    adapter._is_forum_parent=lambda channel:False
    adapter._record_response_async=AsyncMock(side_effect=lambda reply,result,*a,**kw:result)
    source=SessionSource(platform=Platform.DISCORD,chat_id="123",user_id="42")
    event=SimpleNamespace(source=source,reply_anchor_override=None,_runtime_footer_line=footer)
    metadata=_thread_metadata_for_event(event)
    answer="User-authored compactions 999 is ordinary text"
    result=await adapter.send("123",answer,metadata=metadata)
    assert result.success and sent[-1]["content"]==answer
    assert sent[-1]["embed"].footer.text==footer
    assert footer not in sent[-1]["content"]
    result=await adapter.edit_message("123","1",answer,metadata=metadata,finalize=True)
    assert result.success
    assert partial.edit.call_args.kwargs["content"]==answer
    assert partial.edit.call_args.kwargs["embeds"][0] is existing
    assert partial.edit.call_args.kwargs["embeds"][-1].footer.text==footer
    # Actual delivery consumer does not append footer to returned content; streaming sends embed only.
    holder=SimpleNamespace(_delivery_adapter_for=lambda src:adapter,_should_send_voice_reply=lambda *a,**kw:False,_event_thread_metadata=lambda *a:None,_deliver_media_from_response=AsyncMock())
    entry=SimpleNamespace(session_id="s")
    event=SimpleNamespace(source=source)
    result=await GatewayTurnPersistenceMixin._hmwa_deliver_turn_response(holder,event,source,entry,"key",1,{},[],answer,footer,False)
    assert result==answer and event._runtime_footer_line==footer
    result=await GatewayTurnPersistenceMixin._hmwa_deliver_turn_response(holder,event,source,entry,"key",1,{"already_sent":True,"media_already_delivered":True},[],answer,footer,False)
    assert result is None and not sent[-1]["content"] and sent[-1]["embed"].footer.text==footer

    # Non-Discord stays on its existing text footer, after hooks and persistence.
    source.platform=Platform.TELEGRAM
    event=SimpleNamespace(source=source)
    result=await GatewayTurnPersistenceMixin._hmwa_deliver_turn_response(holder,event,source,entry,"key",1,{},[],answer,footer,False)
    assert result==answer+"\n\n"+footer
    before=len(sent)
    await GatewayTurnPersistenceMixin._hmwa_deliver_turn_response(holder,event,source,entry,"key",1,{"already_sent":True},[],"",footer,True)
    assert len(sent)==before


@pytest.mark.asyncio
async def test_real_sdk_loopback_http_footer_payloads(tmp_path,monkeypatch):
    import asyncio
    import aiohttp
    from aiohttp import web
    requests=[]
    bodies={}
    async def endpoint(request):
        payload=await request.json() if request.method in ("POST","PATCH") else {}
        requests.append((request.method,request.path,payload))
        mid=request.match_info.get("mid") or "1"
        body=bodies.setdefault(mid,{"id":mid,"channel_id":"123","content":"","embeds":[],"attachments":[],"edited_timestamp":None,"timestamp":"2026-10-19T00:00:00+00:00","type":0,"pinned":False,"tts":False,"mention_everyone":False,"mentions":[],"mention_roles":[],"author":{"id":"9","username":"Hermes","discriminator":"0","avatar":None}})
        body.update(payload)
        import json
        return web.Response(body=json.dumps(body).encode("utf-8"),headers={"Content-Type":"application/json"})
    app=web.Application()
    app.router.add_route("*","/channels/{cid}/messages",endpoint)
    app.router.add_route("*","/channels/{cid}/messages/{mid}",endpoint)
    server=web.AppRunner(app);await server.setup()
    site=web.TCPSite(server,"127.0.0.1",0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(discord.http.Route,"BASE",f"http://127.0.0.1:{port}")
    client=discord.Client(intents=discord.Intents.none())
    client.http._global_over=asyncio.Event();client.http._global_over.set()
    client.http.token="synthetic-token"
    session=aiohttp.ClientSession();client.http._HTTPClient__session=session
    guild=discord.Guild(data={"id":"1","name":"Synthetic"},state=client._connection)
    channel=discord.TextChannel(state=client._connection,guild=guild,data={"id":"123","name":"Synthetic","type":0,"position":0,"permission_overwrites":[]})
    adapter=DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client=client
    adapter._resolve_channel=AsyncMock(return_value=channel)
    adapter._record_response_async=AsyncMock(side_effect=lambda reply,result,*a,**kw:result)
    try:
        footer="model-a · compact 125% · compactions 2"
        result=await adapter.send("123","Original answer",metadata={"runtime_footer":footer})
        assert result.success
        assert requests[0][2]["content"]=="Original answer" and requests[0][2]["embeds"][0]["footer"]["text"]==footer
        result=await adapter.edit_message("123","1","Original edited",metadata={"runtime_footer":footer},finalize=True)
        assert result.success
        assert [r[0] for r in requests]==["POST","GET","PATCH"]
        assert requests[-1][2]["content"]=="Original edited"
        assert all(footer not in r[2].get("content","") for r in requests)
        assert len(requests[-1][2]["embeds"])==1
        # The same finalization receipt never appends a second owned footer.
        assert (await adapter.edit_message("123","1","Original edited",metadata={"runtime_footer":footer},finalize=True)).success
        assert len(requests[-1][2]["embeds"])==1
        media=[{"description":f"Media {i}"} for i in range(10)]
        bodies["1"]["embeds"]=media
        assert (await adapter.edit_message("123","1","Original edited",metadata={"runtime_footer":footer},finalize=True)).success
        assert requests[-1][2]["embeds"]==[discord.Embed.from_dict(e).to_dict() for e in media]

    finally:
        await session.close();await server.cleanup()
