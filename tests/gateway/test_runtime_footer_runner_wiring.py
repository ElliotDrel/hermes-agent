"""Regression tests for runtime-footer producer/consumer wiring."""

from pathlib import Path
from types import SimpleNamespace

import gateway.runtime_footer_usage as usage
import hermes_constants
from gateway.run_turn_runner import _resolve_runtime_footer_metadata


def test_runner_resolves_footer_usage_inside_runtime_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        usage,
        "get_cached",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "snapshot",
    )
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: Path("/profiles/routed"))

    result = _resolve_runtime_footer_metadata(
        SimpleNamespace(
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_key="runtime-secret",
        ),
        {"display": {"runtime_footer": {"enabled": True, "fields": ["account"]}}},
        "feishu",
    )

    assert result["provider"] == "anthropic"
    assert result["base_url"] == "https://api.anthropic.com"
    assert result["account_usage"] == "snapshot"
    assert result["footer_config"]["fields"] == ["account"]
    assert "api_key" not in result
    assert calls == [
        (
            ("anthropic",),
            {
                "base_url": "https://api.anthropic.com",
                "api_key": "runtime-secret",
                "hermes_home": str(Path("/profiles/routed")),
            },
        )
    ]


def test_runner_does_not_fetch_when_footer_usage_is_not_requested(monkeypatch):
    monkeypatch.setattr(
        usage,
        "get_cached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected refresh")),
    )

    result = _resolve_runtime_footer_metadata(
        SimpleNamespace(provider="anthropic", base_url="", api_key="secret"),
        {"display": {"runtime_footer": {"enabled": True, "fields": ["model"]}}},
        "feishu",
    )

    assert result["account_usage"] is None
    assert "api_key" not in result


import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_footer_is_after_actual_hooks_and_sqlite_persistence(monkeypatch,tmp_path):
    from tests.gateway.test_first_turn_session_meta_rebaseline import _bootstrap,_event,_source,SESSION_ID,SESSION_KEY
    from hermes_state import SessionDB
    import gateway.run as gateway_run
    db=SessionDB(tmp_path/"delivery.db")
    db.create_session(SESSION_ID,source="telegram")
    runner=_bootstrap(monkeypatch,tmp_path,db)
    cfg={"display":{"runtime_footer":{"enabled":True,"fields":["model"]}}}
    monkeypatch.setattr(gateway_run,"_load_gateway_config",lambda:cfg)
    messages=[{"role":"user","content":"hello world"},{"role":"assistant","content":"Original answer"}]
    original={"final_response":"Original answer","messages":messages,"tools":[],"history_offset":0,"model":"route/model","footer_config":{"enabled":True,"fields":["model"]}}
    captures=[]
    async def run(**kwargs):
        captures.append(kwargs)
        return original
    runner._run_agent=run
    response=await runner._handle_message_with_agent(_event(),_source(),SESSION_KEY,1)
    assert response=="Original answer\n\nmodel"
    end=[c.args[1] for c in runner.hooks.emit.await_args_list if c.args[0]=="agent:end"]
    assert end and end[-1]["response"]=="Original answer"
    assert original["final_response"]=="Original answer" and messages[-1]["content"]=="Original answer"
    assert all("\n\nmodel" not in str(row.get("content")) for row in db.get_messages(SESSION_ID))
    assert captures[0]["message"] and captures[0]["history"]==[]
    db.close()


@pytest.mark.asyncio
async def test_interleaved_snapshot_uses_effective_agent_reasoning_and_unknown_never_borrows(monkeypatch):
    import asyncio
    import gateway.run as gateway_run
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
    cfg={"display":{"runtime_footer":{"enabled":True,"fields":["model","reasoning"]}}}
    monkeypatch.setattr(gateway_run,"_load_gateway_config",lambda:cfg)
    shared=SimpleNamespace(_reasoning_config={"effort":"high"})
    waiting=asyncio.Event();release=asyncio.Event()
    agent_a=SimpleNamespace(reasoning_config={"enabled":True,"effort":"high"})
    agent_b=SimpleNamespace(reasoning_config={"enabled":False,"effort":"low"})
    async def a():
        waiting.set();await release.wait()
        return _resolve_runtime_footer_metadata(agent_a,cfg,"discord")
    task=asyncio.create_task(a())
    await waiting.wait()
    shared._reasoning_config={"effort":"xhigh"}
    b=_resolve_runtime_footer_metadata(agent_b,cfg,"discord")
    release.set();a=await task
    assert a["reasoning_effort"]=="high" and b["reasoning_effort"]=="none"
    source=SessionSource(platform=Platform.DISCORD,chat_id="123")
    unknown={**a,"model":"model-a","reasoning_effort":None}
    line=GatewayTurnPersistenceMixin._hmwa_runtime_footer_line(shared,unknown,source,1)
    assert line=="model-a"
    # Changing the shared runner after the snapshot cannot change either captured result.
    shared._reasoning_config={"effort":"low"}
    assert a["reasoning_effort"]=="high" and b["reasoning_effort"]=="none"
