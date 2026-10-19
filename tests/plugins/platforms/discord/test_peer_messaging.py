"""Discord peer tools use the native authority FIFO and exact foreground caller."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('visibility', ['refused', 'none', 'unknown', 'exception'])
async def test_native_peer_tool_admission_visibility_dedup_and_foreground(monkeypatch, tmp_path, visibility):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tools.registry import registry
    from agent.agent_runtime_helpers import invoke_tool
    from agent.delegation_context import delegated_child_context
    from tests.waiting import async_wait_until

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42,43')
    runner = GatewayRunner(GatewayConfig(group_sessions_per_user=True))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False,
                                           extra={'peer_messaging': True}))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter, busy_text_mode='queue')
    sent, results, received, snapshots = [], {}, [], []
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append((chat_id, content))
        # The admission must remain accepted even when its optional visible handoff is refused.
        if content.startswith('[Peer session'):
            if visibility == 'exception':
                raise OSError('synthetic acknowledgement lost')
            return {'refused': SendResult(success=False, error='synthetic visible refusal'),
                    'none': None, 'unknown': SimpleNamespace(success=None)}[visibility]
        return SendResult(success=True, message_id='sent')
    monkeypatch.setattr(adapter, 'send', send)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(adapter, 'send_typing', noop)
    monkeypatch.setattr(adapter, 'stop_typing', noop)

    def source(chat, user='42'):
        return adapter.build_source(chat_id=chat, chat_type='dm', user_id=user)
    # Other transports keep their native tool catalog; no core/global tool registration.
    from gateway.session_tools import platform_tool_scope
    from gateway.session import SessionSource
    with platform_tool_scope(SimpleNamespace(_runner=runner, _ctx=SimpleNamespace(
            source=SessionSource(platform=Platform.TELEGRAM, chat_id='other', user_id='42')))):
        assert registry.get_entry('discord_peer_message') is None
    target = source('target')
    foreign = source('foreign', '43')
    target_ref = authority.register(target)
    foreign_ref = authority.register(foreign)
    pairing = runner._pairing_store_for(target)
    if visibility == 'unknown':
        from gateway import pairing as pairing_module
        monkeypatch.setattr(pairing_module, '_sync_allowlist_add', lambda *args: None)
        monkeypatch.setenv('DISCORD_ALLOWED_USERS', '43')
        code = pairing.generate_code('discord', '42')
        assert pairing.approve_code('discord', code)['user_id'] == '42'
        assert pairing.is_approved('discord', '42')

    def execute(turn):
        ctx = turn._ctx
        sid = authority.logical_owner(ctx.session_id)
        agent = SimpleNamespace(session_id=sid, _current_turn_id='turn-' + sid,
                                _current_api_request_id='', _memory_manager=None, _context_engine_tool_names=set(),
                                valid_tool_names={'discord_peer_message'}, quiet_mode=False, enabled_toolsets=ctx.enabled_toolsets,
                                disabled_toolsets=[], _interrupt_requested=False)
        authority.adopt_agent(sid, authority.executing[sid], agent)
        entry = registry.get_entry('discord_peer_message')
        snapshots.append(entry.schema if entry else None)
        if ctx.message == 'start':
            assert entry is not None
            def call(args, caller=agent):
                if caller is not agent:
                    from agent.tool_executor import _resolve_sequential_dispatch
                    ref = SimpleNamespace(name='discord_peer_message', args=args, task_id='fork-task', call_id='fork-call', trace=[])
                    dispatch = _resolve_sequential_dispatch(caller, ref, [])
                    return json.loads(dispatch.execute(args))
                return json.loads(invoke_tool(caller, 'discord_peer_message', args, 'task',
                                             pre_tool_block_checked=True, skip_tool_request_middleware=True,
                                             skip_tool_execution_middleware=True))
            peers = call({'action': 'list'})['sessions']
            assert [p['session_id'] for p in peers] == [target_ref.session_id]
            fork = SimpleNamespace(**vars(agent))  # background review intentionally shares this session id
            results['fork'] = call({'action': 'list'}, fork)
            with delegated_child_context(sid):
                results['delegated'] = call({'action': 'list'})
            results['foreign'] = call({'action': 'send', 'request_id': 'foreign',
                                       'target_session_id': foreign_ref.session_id, 'message': 'refused'})
            # Advance state inside the real SQLite write, after the loop-side precheck.
            from plugins.platforms.discord import peer_messaging
            original_guard = peer_messaging.write_guard
            for fault in ('generation', 'route', 'epoch', 'cancel', 'revoke', 'grant'):
                def factory(*items):
                    guard = original_guard(*items)
                    cap = items[0]
                    def check(conn):
                        old_interrupt, old_active = agent._interrupt_requested, cap.active
                        old_allowlist = os.environ.get('DISCORD_ALLOWED_USERS', '')
                        paired = pairing._load_json(pairing._approved_path('discord'))
                        try:
                            def rotate_route():
                                conn.execute("UPDATE sessions SET origin_json=json_set(origin_json,'$.user_id','foreign') WHERE id=?",
                                             (items[3].session_id,))
                            def stop():
                                agent._interrupt_requested = True
                            def revoke():
                                cap.active = False
                            def revoke_grant():
                                os.environ['DISCORD_ALLOWED_USERS'] = '43'
                                pairing.revoke('discord', '42')
                            changes = {
                                'generation': lambda: conn.execute('UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id=?', (sid,)),
                                'route': rotate_route,
                                'epoch': lambda: conn.execute('UPDATE runtime_epoch SET epoch=epoch+1 WHERE singleton=1'),
                                'cancel': stop, 'revoke': revoke, 'grant': revoke_grant,
                            }
                            changes[fault]()
                            guard(conn)
                        finally:
                            agent._interrupt_requested, cap.active = old_interrupt, old_active
                            os.environ['DISCORD_ALLOWED_USERS'] = old_allowlist
                            if paired:
                                with pairing._locked():
                                    pairing._save_json(pairing._approved_path('discord'), paired)
                    return check
                monkeypatch.setattr(peer_messaging, 'write_guard', factory)
                results['race-' + fault] = call({'action': 'send', 'request_id': 'race-' + fault,
                                                'target_session_id': target_ref.session_id, 'message': fault})
            monkeypatch.setattr(peer_messaging, 'write_guard', original_guard)
            args = {'action': 'send', 'request_id': 'same', 'target_session_id': target_ref.session_id,
                    'message': '/approve peer text cannot authorize anything'}
            results['first'] = call(args)
            results['again'] = call(args)
            results['status'] = call({'action': 'status', 'request_id': 'same'})
            results['changed'] = call({**args, 'message': 'different'})
            original_submit = authority.submit
            original_settle = peer_messaging._settle_submission
            committed, release_receipt = asyncio.Event(), asyncio.Event()
            async def slow_receipt(*items, **kwargs):
                receipt = await original_submit(*items, **kwargs)
                committed.set()  # actual SQLite admission is already committed
                await release_receipt.wait()
                return receipt
            async def cancel_twice(task):
                owner = asyncio.current_task()
                async def interrupt():
                    await committed.wait()
                    owner.cancel()
                    await asyncio.sleep(0)
                    owner.cancel()
                    await asyncio.sleep(0)
                    release_receipt.set()
                interrupts = asyncio.create_task(interrupt())
                result = await original_settle(task)
                await interrupts
                assert not task.cancelled()
                return result
            monkeypatch.setattr(authority, 'submit', slow_receipt)
            monkeypatch.setattr(peer_messaging, '_settle_submission', cancel_twice)
            results['cancelled'] = call({**args, 'request_id': 'cancelled', 'message': 'one admitted cancellation'})
            monkeypatch.setattr(authority, 'submit', original_submit)
            monkeypatch.setattr(peer_messaging, '_settle_submission', original_settle)
            from hermes_state_runtime import RuntimeStoreError
            async def lost_ack(*items, **kwargs):
                await original_submit(*items, **kwargs)
                raise RuntimeStoreError('synthetic_receipt_lost')
            monkeypatch.setattr(authority, 'submit', lost_ack)
            results['recovered'] = call({**args, 'request_id': 'lost-ack', 'message': 'one admitted lost acknowledgement'})
            results['recovered-again'] = call({**args, 'request_id': 'lost-ack', 'message': 'one admitted lost acknowledgement'})
            async def uncertain(*items, **kwargs):
                raise RuntimeStoreError('synthetic_unknown_write_outcome')
            monkeypatch.setattr(authority, 'submit', uncertain)
            results['uncertain'] = call({**args, 'request_id': 'uncertain', 'message': 'unknown write outcome'})
            monkeypatch.setattr(authority, 'submit', original_submit)
            # A stored closure cannot be used after this foreground generation ends.
            results['handler'] = entry.handler
        return {'final_response': 'ok', 'messages': [], 'completed': True}

    monkeypatch.setattr(TurnRunner, '_run_sync_scoped', execute)

    async def turn(event, src, key, generation):
        sid = runner.session_store.peek_session_id(runner.session_store._generate_session_key(src))
        ctx = TurnContext(source=src, session_id=sid, session_key=key, message=event.text, history=[],
                          enabled_toolsets=[], disabled_toolsets=[], _voice_ack_loop=loop,
                          _run_still_current=lambda: runner._is_session_run_current(key, generation))
        result = await asyncio.to_thread(TurnRunner(runner, ctx).run_sync)
        if event.text.startswith('[Peer session'):
            received.append((event.text, event.allow_gateway_control, event.internal, event.metadata))
            done.set()
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = result
        return result['final_response']
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        event = MessageEvent(text='start', source=source('sender'), message_id='human-start')
        await adapter.handle_message(event)
        await asyncio.wait_for(done.wait(), 10)
        await async_wait_until(lambda: results.get('uncertain') is not None, timeout=10, what='settled sender receipt')
        assert results['fork']['error'] == results['delegated']['error'] == 'permission_denied'
        assert results['foreign']['accepted'] is False
        assert all(results['race-' + fault]['accepted'] is False for fault in ('generation', 'route', 'epoch', 'cancel', 'revoke', 'grant'))
        assert results['first']['accepted'] is True
        assert results['first']['visible'] is (False if visibility == 'refused' else None)
        assert results['first']['admission_id'] == results['again']['admission_id'] == results['status']['admission_id']
        assert results['first']['remaining'] == 7  # authoritative foreign refusal refunded exactly once
        assert results['changed']['error'] == 'admission_conflict'
        assert len([content for _, content in sent if content.startswith('[Peer session')]) == 1
        rows = list_session_admissions(authority.db, session_id=target_ref.session_id, pending_only=False)
        assert len(rows) == 3 and rows[0]['payload']['peer_message_v1']['human_id'] == '42'
        assert results['cancelled']['accepted'] is True and results['cancelled']['visible'] is False
        assert results['cancelled']['error'] == 'cancelled_after_admission'
        assert results['cancelled']['remaining'] == 6
        assert results['recovered']['accepted'] is True and results['recovered']['visible'] is None
        assert results['recovered']['admission_id'] == results['recovered-again']['admission_id']
        assert results['uncertain']['accepted'] is None and results['uncertain']['remaining'] == 4
        assert results['uncertain']['error'] == 'admission_uncertain'
        assert 'local_operator_v1' not in rows[0]['payload']
        assert received[0][1:3] == (False, False)
        assert received[0][3]['turn_author']['is_bot'] is True
        assert json.loads(results['handler']({'action': 'list'}))['error'] == 'permission_denied'
    finally:
        active = [task for task in adapter._background_tasks if not task.done()]
        if active:
            await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), 10)
        await asyncio.to_thread(authority.db.close)


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_capabilities', [False, True, 'queued-route', 'queued-connector'],
                         ids=['bounded-chain', 'lost-process-capabilities', 'queued-route', 'queued-connector'])
async def test_profile_loader_registry_frozen_schema_chain_and_native_status(monkeypatch, tmp_path, lose_capabilities):
    from pathlib import Path
    import hermes_state
    from gateway.config import load_gateway_config, Platform
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_identity import resolve_identity
    from gateway.profile_routing import parse_profile_routes
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tools.registry import registry
    from agent.agent_runtime_helpers import invoke_tool
    from tests.waiting import async_wait_until

    root = tmp_path / '.hermes'; beta = root / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    for home, enabled in [(root, True), (beta, False)]:
        (home / 'config.yaml').write_text('discord:\n  allow_from: [\"42\"]\n  require_mention: false\ngateway:\n  multiplex_profiles: true\nplatforms:\n  discord:\n    enabled: true\n    token: synthetic-token-never-used-for-network\n    extra:\n      peer_messaging: ' + str(enabled).lower() + '\n', encoding='utf-8')
    homes = [('default', root), ('beta', beta)]
    process_ownership.reserve([root, beta])
    runner = None
    try:
        with _profile_runtime_scope(root, {}):
            config = load_gateway_config()
            config._runtime_profile_homes = tuple(homes)
            config.profile_routes = parse_profile_routes([
                {'name': 'different-transport', 'platform': 'discord', 'bot_profile': 'beta',
                 'profile': 'default', 'chat_id': 'transport-other'},
                {'name': 'different-runtime', 'platform': 'discord', 'profile': 'beta', 'chat_id': 'runtime-other'}])
            runner = GatewayRunner(config)
            await initialize_gateway_runtime(runner)
            first = DiscordAdapter(config.platforms[Platform.DISCORD])
        with _profile_runtime_scope(beta, {}):
            cfg_b = load_gateway_config()
            second = DiscordAdapter(cfg_b.platforms[Platform.DISCORD])
        first.gateway_runner = second.gateway_runner = runner
        second.set_owner_profile('beta')
        runner.adapters = {Platform.DISCORD: first}
        runner._profile_adapters = {'beta': {Platform.DISCORD: second}}
        runner._wire_adapter_handlers(first, busy_text_mode='queue')
        runner._wire_adapter_handlers(second, busy_text_mode='queue',
            message_handler=runner._make_profile_message_handler('beta'),
            busy_session_handler=runner._make_profile_busy_session_handler('beta'),
            authorization_check=runner._make_adapter_auth_check(Platform.DISCORD, 'beta'))
        authority_a = runner.session_authorities.for_home(root)
        authority_b = runner.session_authorities.for_home(beta)
        observed, receipts, peer_remaining, schemas, visible, frozen_tools = {}, [], [], {}, [], []
        finish_peers, first_done, all_peers = asyncio.Event(), asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        peer_count = 0
        claim_gate, claim_refused = asyncio.Event(), asyncio.Event()
        queued_rotation = isinstance(lose_capabilities, str)
        if not queued_rotation:
            claim_gate.set()
        async def send(chat_id, content, reply_to=None, metadata=None):
            if content.startswith('[Peer session'):
                visible.append(content)
            return SendResult(success=True, message_id='sent')
        async def noop(*args, **kwargs):
            pass
        for adapter in (first, second):
            monkeypatch.setattr(adapter, 'send', send)
            monkeypatch.setattr(adapter, 'send_typing', noop)
            monkeypatch.setattr(adapter, 'stop_typing', noop)
        def source(adapter, chat):
            src = adapter.build_source(chat_id=chat, chat_type='dm', user_id='42')
            resolve_identity(src, runner=runner)
            return src
        with _profile_runtime_scope(root, {}):
            target = source(first, 'target')
            target_ref = authority_a.register(target)
            wrong_transport = authority_a.register(source(second, 'transport-other'))
        with _profile_runtime_scope(beta, {}):
            wrong_runtime = authority_b.register(source(first, 'runtime-other'))
        original_claim = authority_a._claim_next
        async def held_claim(ref, live):
            if ref.session_id == target_ref.session_id:
                await claim_gate.wait()
            try:
                return await original_claim(ref, live)
            except Exception:
                if ref.session_id == target_ref.session_id:
                    claim_refused.set()
                raise
        monkeypatch.setattr(authority_a, '_claim_next', held_claim)
        def execute(turn):
            from gateway.session_authorities import active_authority
            ctx = turn._ctx; authority = active_authority(runner)
            sid = authority.logical_owner(ctx.session_id)
            agent = SimpleNamespace(session_id=sid, _current_turn_id='native-' + sid, _current_api_request_id='',
                                    _memory_manager=None, _context_engine_tool_names=set(), valid_tool_names={'discord_peer_message'},
                                    enabled_toolsets=ctx.enabled_toolsets, disabled_toolsets=[], _interrupt_requested=False)
            authority.adopt_agent(sid, authority.executing[sid], agent)
            from agent.agent_init import _load_tools
            agent.quiet_mode = True
            agent.platform = 'discord'
            _load_tools(agent, ctx.enabled_toolsets, [])
            entry = registry.get_entry('discord_peer_message')
            schema = json.dumps(agent.tools, sort_keys=True) if entry else None
            frozen_tools.append((agent.tools, json.dumps(agent.tools, sort_keys=True)))
            def call(args):
                return json.loads(invoke_tool(agent, 'discord_peer_message', args, 'task', pre_tool_block_checked=True,
                                             skip_tool_request_middleware=True, skip_tool_execution_middleware=True))
            if ctx.message in ('A1', 'A2', 'B1', 'B2'):
                schemas[ctx.message] = schema
            if ctx.message == 'A1':
                assert entry is not None
                for label, ref in [('runtime', wrong_runtime), ('transport', wrong_transport)]:
                    observed[label] = call({'action': 'send', 'request_id': label,
                                            'target_session_id': ref.session_id, 'message': 'must refuse'})
                from plugins.platforms.discord import peer_messaging
                original_guard = peer_messaging.write_guard
                for fault in ('profile-route', 'connector'):
                    def factory(*items):
                        guard = original_guard(*items)
                        def check(conn):
                            old_routes, old_token = runner.config.profile_routes, first.config.token
                            try:
                                if fault == 'profile-route':
                                    runner.config.profile_routes = parse_profile_routes([
                                        {'name': 'revoked-target', 'platform': 'discord', 'profile': 'beta', 'chat_id': 'target'}])
                                else:
                                    first.config.token = 'rotated-synthetic-token'
                                guard(conn)
                            finally:
                                runner.config.profile_routes, first.config.token = old_routes, old_token
                        return check
                    monkeypatch.setattr(peer_messaging, 'write_guard', factory)
                    observed[fault] = call({'action': 'send', 'request_id': fault,
                        'target_session_id': target_ref.session_id, 'message': 'refused after actual configuration rotation'})
                monkeypatch.setattr(peer_messaging, 'write_guard', original_guard)
                for index in range(8):
                    receipts.append(call({'action': 'send', 'request_id': str(index),
                                          'target_session_id': target_ref.session_id, 'message': 'bounded ' + str(index)}))
                observed['exhausted'] = call({'action': 'send', 'request_id': 'ninth',
                                               'target_session_id': target_ref.session_id, 'message': 'must refuse'})
            elif ctx.message == 'A2':
                observed['reset'] = call({'action': 'list'})
                observed['completed'] = call({'action': 'status', 'admission_id': receipts[0]['admission_id']})
            elif ctx.message.startswith('[Peer session'):
                peer_remaining.append(call({'action': 'list'}))
            return {'final_response': 'ok', 'messages': [], 'completed': True}
        monkeypatch.setattr(TurnRunner, '_run_sync_scoped', execute)
        async def turn(event, src, key, generation):
            nonlocal peer_count
            if event.text.startswith('[Peer session'):
                await finish_peers.wait()
            sid = runner.session_store.peek_session_id(runner.session_store._generate_session_key(src))
            ctx = TurnContext(source=src, session_id=sid, session_key=key, message=event.text, history=[],
                              enabled_toolsets=[], disabled_toolsets=[], _voice_ack_loop=loop,
                              _run_still_current=lambda: runner._is_session_run_current(key, generation))
            result = await asyncio.to_thread(TurnRunner(runner, ctx).run_sync)
            if execution_result.get() is not None:
                execution_result.get()['result'] = result
            if event.text == 'A1':
                first_done.set()
            elif event.text.startswith('[Peer session'):
                peer_count += 1
                if peer_count == 8:
                    all_peers.set()
            return 'ok'
        monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
        with _profile_runtime_scope(root, {}):
            await first.handle_message(MessageEvent(text='A1', message_id='human-A1', source=source(first, 'sender')))
        await asyncio.wait_for(first_done.wait(), 20)
        assert all(receipt['accepted'] is True and receipt['visible'] is True for receipt in receipts), receipts
        assert observed['runtime']['accepted'] is observed['transport']['accepted'] is False
        assert observed['exhausted']['error'] == 'peer_budget_exhausted'
        assert len(visible) == 8
        assert observed['profile-route']['accepted'] is observed['connector']['accepted'] is False
        if queued_rotation:
            from gateway import session_peer
            original_claim_guard = session_peer.claim_guard
            def claim_factory(*items):
                guard = original_claim_guard(*items)
                def rotate_inside_write(conn):
                    if lose_capabilities == 'queued-route':
                        runner.config.profile_routes = parse_profile_routes([
                            {'name': 'revoked-queued', 'platform': 'discord', 'profile': 'beta', 'chat_id': 'target'}])
                    else:
                        first.config.token = 'rotated-after-admission'
                    guard(conn)
                return rotate_inside_write if guard else None
            monkeypatch.setattr(session_peer, 'claim_guard', claim_factory)
            claim_gate.set()
            await asyncio.wait_for(claim_refused.wait(), 10)
            assert peer_remaining == [] and peer_count == 0
            rows = list_session_admissions(authority_a.db, session_id=target_ref.session_id, pending_only=False)
            assert len(rows) == 8 and all(row['status'] == 'queued' for row in rows)
            return  # current policy refused execution; accepted receipts remain durable, not completed
        if lose_capabilities:
            first._peer_sessions['chains'].clear()  # a restarted process cannot recover these capabilities
        finish_peers.set()
        await asyncio.wait_for(all_peers.wait(), 20)
        await async_wait_until(lambda: all(row['status'] == 'terminal' for row in list_session_admissions(
            authority_a.db, session_id=target_ref.session_id, pending_only=False)), timeout=10, what='peer model completion')
        if lose_capabilities:
            assert all(item.get('error') == 'permission_denied' for item in peer_remaining)
        else:
            assert all(item['remaining'] == 0 for item in peer_remaining)  # successors never reset the chain
        flags = []
        for label, home, adapter in [('B1', beta, second), ('A2', root, first), ('B2', beta, second)]:
            with _profile_runtime_scope(home, {}):
                loaded = load_gateway_config()
                flags.append(loaded.platforms[Platform.DISCORD].extra.get('peer_messaging'))
                await adapter.handle_message(MessageEvent(text=label, message_id='human-' + label,
                                                          source=source(adapter, 'sender')))
            await async_wait_until(lambda: label in schemas, timeout=10, what=label + ' tool snapshot')
            if label == 'B1':
                second.config.extra['peer_messaging'] = True  # the existing B conversation stays frozen off
        assert flags == [False, True, False]
        assert schemas['A1'] == schemas['A2'] and schemas['B1'] is schemas['B2'] is None
        assert all(json.dumps(tools, sort_keys=True) == snapshot for tools, snapshot in frozen_tools)
        assert registry.get_entry('discord_peer_message') is None
        assert observed['reset']['remaining'] == 8
        assert observed['completed']['model_completed'] is True
        assert observed['completed']['visible'] is None  # a fresh human chain does not invent the old send acknowledgement
        assert len(list_session_admissions(authority_a.db, session_id=target_ref.session_id, pending_only=False)) == 8
    finally:
        if 'finish_peers' in locals():
            finish_peers.set()
            claim_gate.set()
        if runner is not None:
            for adapter in [*runner.adapters.values(), *getattr(runner, '_profile_adapters', {}).get('beta', {}).values()]:
                tasks = [task for task in adapter._background_tasks if not task.done()]
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            for authority in runner.session_authorities:
                authority.db.close()
        process_ownership.release(beta);process_ownership.release(root)
