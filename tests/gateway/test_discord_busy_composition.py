"""Discord physical edits seal through the real native admission FIFO."""
import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def composition_lane(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from plugins.platforms.discord.adapter import DiscordAdapter
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42,43')
    runner = GatewayRunner(GatewayConfig(group_sessions_per_user=False))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.config import load_gateway_config
    homes=[tmp_path/'profile-a',tmp_path/'profile-b']
    for home,seconds in zip(homes,[20,0]):
        home.mkdir()
        (home/'config.yaml').write_text('platforms:\n  discord:\n    enabled: true\ndiscord:\n  busy_composition_window_seconds: '+str(seconds)+'\n',encoding='utf-8')
    for home,seconds in [(homes[0],20),(homes[1],0),(homes[0],20)]:
        scope=set_hermes_home_override(home)
        try:
            native=load_gateway_config().platforms[Platform.DISCORD]
        finally:
            reset_hermes_home_override(scope)
        assert native.extra.get("busy_composition_window_seconds",0)==seconds
        from plugins.platforms.discord.composition import window
        adapter=DiscordAdapter(native)
        assert window(adapter)==seconds
    adapter.config.typing_indicator=False
    adapter.config.extra['group_sessions_per_user']=False
    adapter._client=SimpleNamespace(user=SimpleNamespace(id=99))
    async def send(*args, **kwargs):
        return SendResult(success=True, message_id='sent')
    monkeypatch.setattr(adapter, 'send', send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter, busy_text_mode='queue')
    started, release = asyncio.Event(), asyncio.Event()
    executed = []
    async def turn(event, source, quick_key, run_generation):
        executed.append((event.text, event.message_id, event.reply_anchor_override))
        if event.text == 'A':
            started.set()
            await release.wait()
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    def event(text, number, sender='42'):
        return MessageEvent(text=text, source=adapter.build_source(chat_id='room', chat_type='group', user_id=sender),
                            message_id=str(number))
    await adapter.handle_message(event('A', 0))
    await asyncio.wait_for(started.wait(), 10)
    yield runner, adapter, authority, event, executed, release
    release.set()
    await async_wait_until(lambda: not authority.executing, timeout=10, what='turn settled')
    await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['edited', 'mention', 'disabled', 'cutoff', 'revoked', 'empty', 'deleted', 'private', 'media', 'capacity', 'unauthorized', 'command', 'mention-command'])
async def test_physical_edits_seal_once_with_first_owner_and_last_anchor(monkeypatch, composition_lane, mode):
    from gateway.session_ingress_burst import _flush, discard_busy_burst, holds_busy_burst
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord import composition
    runner, adapter, authority, event, executed, release = composition_lane
    now = [time.monotonic()]
    monkeypatch.setattr(composition, '_now', lambda: now[0])
    if mode == 'disabled':
        adapter.config.extra['busy_composition_window_seconds'] = 0
    first, second = event('first', 1), event('second', 2)
    key = adapter._event_session_key(first)
    tasks = [asyncio.create_task(adapter.handle_message(first))]
    await async_wait_until(lambda: holds_busy_burst(adapter, key), timeout=5, what='first physical input held')
    tasks.append(asyncio.create_task(adapter.handle_message(second)))
    await async_wait_until(lambda: len(adapter._shared_bursts[key].absorbed) == 1, timeout=5, what='second physical input joined')
    burst = adapter._shared_bursts[key]
    deadline = getattr(burst.composition, 'deadline', None)
    def edit(text, *, sender='42'):
        return SimpleNamespace(id=1, content=text, author=SimpleNamespace(id=sender, bot=False),
                               channel=SimpleNamespace(id='room'), attachments=[], edited_at=None)
    if mode == 'cutoff':
        now[0] += 21
    await adapter._on_platform_message_edit(None, edit('forged', sender='43'))
    await adapter._on_platform_message_edit(None, edit('<@99> corrected' if mode=='mention' else 'corrected'))
    adapter.config.extra['draft_markers']=['Draft']
    if not callable(getattr(adapter,'_is_draft_message',None)):
        # Integration also runs this case with PA29's native classifier instead of this injected capability.
        adapter._is_draft_message=lambda text:text.strip().startswith('Draft')
    media_edit=edit('new attachment');media_edit.attachments=[object()]
    actions={
        'command':lambda:adapter._on_platform_message_edit(None,edit('/stop')),
        'mention-command':lambda:adapter._on_platform_message_edit(None,edit('<@99> /stop')),
        'empty':lambda:adapter._on_platform_message_edit(None,edit(' ')),
        'deleted':lambda:adapter._on_platform_message_delete(edit('corrected')),
        'private':lambda:adapter._on_platform_message_edit(None,edit('<@99> Draft private')),
        'media':lambda:adapter._on_platform_message_edit(None,media_edit),
    }
    if action:=actions.get(mode):
        await action()
    if mode=='unauthorized':
        runner._is_user_authorized=lambda source:False
    if mode=='capacity':
        import hermes_state_runtime
        monkeypatch.setattr(hermes_state_runtime,'ADMISSION_QUEUE_MAX',0)
    assert getattr(burst.composition, 'deadline', None) == deadline
    if mode == 'revoked':
        discard_busy_burst(adapter, key)
    elif mode not in ('empty','deleted','private','media','command','mention-command'):
        await _flush(adapter, key, burst)
    await asyncio.gather(*tasks)
    owner = runner.session_store.peek_session_id(runner.session_store._generate_session_key(first.source))
    rows = list_session_admissions(authority.db, session_id=owner, pending_only=False)
    if mode in ('revoked','empty','deleted','private','media','capacity','unauthorized','command','mention-command'):
        assert len(rows) == 1 and not first._gateway_accepted and not second._gateway_accepted
        return
    expected = 'corrected\nsecond' if mode in ('edited','mention') else 'first\nsecond'
    assert len(rows) == 2 and rows[1]['payload']['text'] == expected
    assert first._gateway_accepted and second._gateway_accepted
    await adapter._on_platform_message_edit(None, edit('too late'))
    release.set()
    await async_wait_until(lambda: len(executed) == 2, timeout=10, what='sealed input executed')
    assert executed[1] == (expected, '1', None if mode == 'disabled' else '2')


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['sender','media','context'])
async def test_composition_never_crosses_sender_or_fifo_boundaries(composition_lane,tmp_path,boundary):
    from gateway.session_ingress_burst import _flush, holds_busy_burst
    from hermes_state_runtime import list_session_admissions
    runner, adapter, authority, event, executed, release = composition_lane
    first, other, last = event('first', 1), event('other', 2, '43' if boundary=='sender' else '42'), event('last', 3)
    if boundary=='media':
        from gateway.platforms.event import MessageType
        image=tmp_path/'image.png';image.write_bytes(b'\x89PNG\r\n\x1a\n'+b'\x00'*24)
        other.message_type=MessageType.PHOTO;other.media_urls=[str(image)];other.media_types=['image/png']
    elif boundary=='context':
        other.channel_prompt='Different pinned channel policy' 
    key = adapter._event_session_key(first)
    tasks = [asyncio.create_task(adapter.handle_message(first))]
    await async_wait_until(lambda: holds_busy_burst(adapter, key), timeout=5, what='first held')
    for message in (other, last):
        tasks.append(asyncio.create_task(adapter.handle_message(message)))
        await async_wait_until(lambda: key in adapter._shared_bursts and adapter._shared_bursts[key].head is message,
                               timeout=5, what='sender boundary sealed predecessor')
    await _flush(adapter, key, adapter._shared_bursts[key])
    await asyncio.gather(*tasks)
    owner = runner.session_store.peek_session_id(runner.session_store._generate_session_key(first.source))
    rows = list_session_admissions(authority.db, session_id=owner, pending_only=False)
    assert [r['payload']['text'] for r in rows] == ['A', 'first', 'other', 'last']
    assert all(e._gateway_accepted for e in (first, other, last))
    release.set()
    await async_wait_until(lambda: len(executed) == 4, timeout=10, what='FIFO executed')
    assert [row[0] for row in executed] == ['A', 'first', 'other', 'last']
