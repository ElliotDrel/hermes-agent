"""Standalone Discord progress follows receipt batches through queue and handoff."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from tests.gateway.test_discord_receipt_reactions import Board, _adapter, _event


def setup(monkeypatch):
    monkeypatch.setattr('hermes_cli.config.read_raw_config_readonly', lambda: {
        'display': {'platforms': {'discord': {'progress_compositor': 'single_message'}}}})
    board = Board()
    adapter = _adapter(board)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='working'))
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True))
    return adapter, board


def receive(adapter, board, tmp_path, mid, text):
    event = _event(adapter, board, tmp_path, mid, text)
    event.raw_message.content = text
    event.raw_message.jump_url = f'https://discord.com/channels/10/20/{mid}'
    event.raw_message.attachments = []
    adapter._acknowledge_receipt(event.raw_message, event.source)
    return event


@pytest.mark.asyncio
async def test_standalone_header_queue_merge_drop_and_handoff(monkeypatch, tmp_path):
    adapter, board = setup(monkeypatch)
    first = receive(adapter, board, tmp_path, 100, 'First  task\nplease')
    key = adapter._event_session_key(first)
    c = adapter.take_receipt_progress(key, 1, ['100'])
    await c._start_task
    assert adapter.send.call_args.kwargs.get('reply_to') is None
    assert c._fit_render() == '↪ **["First task please"](https://discord.com/channels/10/20/100)**\n⏳ Working…'
    adapter._active_sessions[key] = asyncio.Event()
    second = receive(adapter, board, tmp_path, 101, 'Second task')
    receive(adapter, board, tmp_path, 102, 'extra detail')
    adapter.on_busy_burst_closed(second.source, ['101', '102'])
    receive(adapter, board, tmp_path, 103, 'Third task')
    assert 'Queue - **2** - 1️⃣ ["Second task"](https://discord.com/channels/10/20/101) +1 | 2️⃣' in c._fit_render()
    await c.flush(force=True)
    assert adapter.send.await_count == 1
    assert adapter.edit_message.call_args.kwargs['content'] == c._fit_render()
    await adapter.flush_reactions()
    assert board.marks([101, 102, 103]) == {101: {'1️⃣'}, 102: {'1️⃣'}, 103: {'⏳', '2️⃣'}}
    kept = adapter.keep_receipt_progress(c)
    next_c = adapter.take_receipt_progress(key, 2, ['101', '102'])
    assert next_c is kept
    assert next_c._fit_render().startswith('↪ **["Second task"](https://discord.com/channels/10/20/101)** +1 more\nQueue - **1** - 2️⃣')
    adapter.on_messages_dropped(second.source, ['103'])
    assert 'Queue -' not in next_c._fit_render()
    await adapter.flush_reactions()


@pytest.mark.asyncio
async def test_attachment_escape_queue_cap_and_atomic_fitting(monkeypatch, tmp_path):
    adapter, board = setup(monkeypatch)
    first = _event(adapter, board, tmp_path, 200)
    first.raw_message.content = ''
    first.raw_message.attachments = [SimpleNamespace(filename='a[*_~|`) ].png')]
    first.raw_message.jump_url = 'https://discord.com/channels/10/20/200'
    adapter._acknowledge_receipt(first.raw_message, first.source)
    key = adapter._event_session_key(first)
    c = adapter.take_receipt_progress(key, 1, ['200'])
    await c._start_task
    assert '📎 a\\[\\*\\_\\~\\|\\`\\) \\].png' in c._fit_render()
    adapter._active_sessions[key] = asyncio.Event()
    for mid in range(201, 206):
        event = receive(adapter, board, tmp_path, mid, 'queued ' * 10)
        adapter.on_busy_burst_closed(event.source, [str(mid)])
    full = c._fit_render()
    assert 'Queue - **5**' in full and ' | +2 more' in full
    assert '3️⃣' in full and '4️⃣' not in full
    c._text_limit = 200
    fitted = c._fit_render()
    assert len(fitted) <= 200 and 'Queue -' not in fitted
    assert '](https://discord.com/channels/10/20/200)**' in fitted
    c.publish_activity('activity ' * 400)
    fitted = c._fit_render()
    assert len(fitted) <= 200 and '](https://discord.com/channels/10/20/200)**' in fitted
    await adapter.flush_reactions()


@pytest.mark.asyncio
async def test_turn_created_progress_is_standalone_with_id_fallback(monkeypatch, tmp_path):
    from gateway.run_turn_progress import GatewayTurnProgressMixin

    adapter, board = setup(monkeypatch)
    event = _event(adapter, board, tmp_path, 300, 'A restored task')
    event.raw_message = None
    event.source.guild_id = '10'
    event.source.chat_id = '20'
    key = adapter._event_session_key(event)
    adapter._record_discord_processing_start = lambda *args, **kwargs: None
    await adapter.on_processing_start(event)
    ctx = SimpleNamespace(progress_compositor=None, session_key=key, run_generation=1,
                          source=event.source, inbound_message_ids=['300'],
                          _progress_reply_to='300', _progress_metadata={}, _cleanup_progress=False)
    turn = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
        _session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(progress_compositor=None)),
        _delivery_adapter_for=lambda source: adapter), _track_compositor_id=lambda c: None)
    c = GatewayTurnProgressMixin.start_progress_compositor(turn)
    await c._start_task
    assert adapter.send.call_args.kwargs['reply_to'] is None
    assert c._fit_render().startswith('↪ **["A restored task"](https://discord.com/channels/10/20/300)**')
