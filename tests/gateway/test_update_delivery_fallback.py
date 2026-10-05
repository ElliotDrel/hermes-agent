"""Update receipts survive delivery failure and use the configured main chat."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner

@pytest.mark.asyncio
@pytest.mark.parametrize('primary', ['refused', 'exception', 'missing'])
async def test_update_receipt_falls_back_to_main(tmp_path, primary):
    runner=object.__new__(GatewayRunner)
    runner.adapters={}; runner._profile_configs={}
    runner.config=SimpleNamespace(platforms={Platform.DISCORD:SimpleNamespace(home_channel=SimpleNamespace(chat_id='900',thread_id='stale-home-thread'))})
    adapter=SimpleNamespace(send=AsyncMock(side_effect=([SendResult(success=True)] if primary=='missing' else [RuntimeError('gone') if primary=='exception' else SendResult(success=False,error='gone'),SendResult(success=True)])))
    runner._authorization_adapter=lambda *a:adapter
    pending=tmp_path/'.update_pending.json'
    pending.write_text(json.dumps(dict(platform='discord',chat_id=None if primary=='missing' else '100',thread_id='100')))
    (tmp_path/'.update_exit_code').write_text('0')
    with patch('gateway.run._hermes_home',tmp_path):
        assert await runner._send_update_notification() is True
    args=adapter.send.await_args
    assert args.args[0]=='900'
    assert args.kwargs['metadata']['non_conversational'] is True
    assert not args.kwargs['metadata'].get('thread_id')
    assert not pending.exists()

@pytest.mark.asyncio
async def test_undelivered_update_receipt_survives_for_retry(tmp_path):
    runner=object.__new__(GatewayRunner); runner.adapters={}; runner._profile_configs={}
    runner.config=SimpleNamespace(platforms={Platform.DISCORD:SimpleNamespace(home_channel=SimpleNamespace(chat_id='900',thread_id=None))})
    adapter=SimpleNamespace(send=AsyncMock(return_value=SendResult(success=False,error='offline')))
    runner._authorization_adapter=lambda *a:adapter
    pending=tmp_path/'.update_pending.json'; pending.write_text(json.dumps(dict(platform='discord',chat_id='100',thread_id='100')))
    exit_path=tmp_path/'.update_exit_code'; exit_path.write_text('1')
    with patch('gateway.run._hermes_home',tmp_path):
        assert await runner._send_update_notification() is False
    assert pending.exists() and exit_path.exists()

@pytest.mark.asyncio
async def test_watcher_preserves_failed_completion(tmp_path):
    import asyncio
    runner=object.__new__(GatewayRunner); runner.adapters={}; runner._profile_configs={}
    runner.config=SimpleNamespace(platforms={Platform.DISCORD:SimpleNamespace(home_channel=SimpleNamespace(chat_id='900',thread_id=None))})
    adapter=SimpleNamespace(send=AsyncMock(return_value=SendResult(success=False,error='offline')))
    runner._authorization_adapter=lambda *a:adapter
    runner._peek_session_state=lambda *a:None
    pending=tmp_path/'.update_pending.json'; pending.write_text(json.dumps(dict(platform='discord',chat_id='100',thread_id='100')))
    exit_path=tmp_path/'.update_exit_code'; exit_path.write_text('0')
    with patch('gateway.run._hermes_home',tmp_path), patch('gateway.run_notifications.asyncio.sleep',side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await runner._watch_update_progress()
    assert pending.exists() and exit_path.exists()

@pytest.mark.asyncio
async def test_same_parent_id_still_falls_back_out_of_thread():
    runner=object.__new__(GatewayRunner); runner._profile_configs={}
    runner.config=SimpleNamespace(platforms={Platform.DISCORD:SimpleNamespace(home_channel=SimpleNamespace(chat_id='900',thread_id='old'))})
    adapter=SimpleNamespace(send=AsyncMock(side_effect=[SendResult(success=False,error='gone'),SendResult(success=True)]))
    assert await runner._deliver_update_completion(dict(chat_id='900',thread_id='100'),Platform.DISCORD,adapter,'Done')
    assert adapter.send.await_count==2
    assert not adapter.send.await_args.kwargs['metadata'].get('thread_id')

@pytest.mark.asyncio
async def test_update_stream_falls_back_to_main(tmp_path):
    runner=object.__new__(GatewayRunner); runner._profile_configs={}
    runner.config=SimpleNamespace(platforms={Platform.DISCORD:SimpleNamespace(home_channel=SimpleNamespace(chat_id='900',thread_id=None))})
    adapter=SimpleNamespace(send=AsyncMock(side_effect=[SendResult(success=False,error='gone'),SendResult(success=True)]))
    runner._authorization_adapter=lambda *a:adapter
    (tmp_path/'.update_pending.json').write_text(json.dumps(dict(platform='discord',chat_id='100',thread_id='100')))
    with patch('gateway.run._hermes_home',tmp_path):
        target=runner._resolve_update_target(runner._update_paths())
        await runner._send_update_output(target,'Waiting for 2 active agents')
    assert adapter.send.await_args.args[0]=='900'
    assert adapter.send.await_args.kwargs['metadata']['non_conversational'] is True

@pytest.mark.asyncio
@pytest.mark.parametrize('has_target',[True,False])
async def test_monitor_deadline_never_fabricates_install_exit(tmp_path,has_target):
    runner=object.__new__(GatewayRunner); runner._profile_configs={}; runner.adapters={}
    runner.config=SimpleNamespace(platforms={})
    adapter=SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    runner._authorization_adapter=lambda *a:adapter if has_target else None
    runner._peek_session_state=lambda *a:None
    pending=tmp_path/'.update_pending.json';pending.write_text(json.dumps(dict(platform='discord',chat_id='100',thread_id='100')))
    with patch('gateway.run._hermes_home',tmp_path),patch('hermes_cli.gateway._get_restart_exit_wait_budget',return_value=0):
        await runner._watch_update_progress(timeout=0)
    assert pending.exists()
    assert not (tmp_path/'.update_exit_code').exists()
    if has_target:
        assert '30 minutes' not in adapter.send.await_args.args[1]


def test_failed_update_does_not_invent_running_version():
    from gateway.run_notifications import _UPDATE_FAILED_NOTICE
    assert 'previous version is still running' not in _UPDATE_FAILED_NOTICE
