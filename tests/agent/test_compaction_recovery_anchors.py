"""Offline regressions for committed prune recovery and canonical priced prefixes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_compression import CompressionCommitFence, compress_context
from agent.turn_context import _preflight_request_tokens
from agent.turn_usage import record_response_usage
from agent.usage_anchor import (
    USAGE_ANCHOR_MODEL_CONFIG_KEY, anchored_context_tokens, capture_usage_anchor,
    message_fingerprint, persisted_anchor_tokens, restore_usage_anchor, set_usage_anchor,
)
from hermes_state import SessionDB
from run_agent import AIAgent


def _agent(db, sid):
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI"),
          patch("agent.model_metadata.get_model_context_length", return_value=1_050_000),
          patch("agent.context_compressor.get_model_context_length", return_value=1_050_000)):
        agent = AIAgent(api_key="offline-test-key", base_url="https://openrouter.ai/api/v1",
                        model="test/model", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_db=db, session_id=sid, save_trajectories=False)
    agent._session_db_created = True
    agent._compression_feasibility_checked = True
    agent._cached_system_prompt = "Stable system"
    agent._cached_system_prompt_static = "Stable system"
    agent.compression_in_place = True
    agent.context_compressor.threshold_tokens = 231_200
    agent.context_compressor.context_length = 1_050_000
    agent.client = None
    return agent


def _usage(tokens):
    return SimpleNamespace(usage=None if tokens is None else SimpleNamespace(
        prompt_tokens=tokens, completion_tokens=10, total_tokens=tokens + 10))


def _tool_history():
    rows = [{"role": "user", "content": "Inspect the output"}]
    for i in range(2):
        rows.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}",
             "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "large output " * 2_000},
        ])
    rows.extend([{"role": "assistant", "content": "Inspected"},
                 {"role": "user", "content": "Continue"},
                 {"role": "assistant", "content": "Working"},
                 {"role": "user", "content": "Latest request"}])
    return rows


def _seed(db, sid, rows):
    db.create_session(sid, source="cli", model="test/model")
    for row in rows:
        db.append_message(sid, row["role"], row.get("content"),
                          tool_calls=row.get("tool_calls"), tool_call_id=row.get("tool_call_id"))
    return db.get_messages_as_conversation(sid)


@pytest.mark.parametrize("tokens,rearmed", [(68_085, True), (231_200, False),
                                             (300_000, False), (None, False)])
def test_real_pruning_commit_arms_only_provider_confirmed_recovery(tmp_path, tokens, rearmed):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        sid = "prune-recovery"
        messages = _seed(db, sid, _tool_history())
        original = deepcopy(messages)
        agent = _agent(db, sid)
        cc = agent.context_compressor
        # Deterministic structural endpoint; actual pruning and DB commit are exercised.
        cc._fallback_compression_streak = 1
        cc._persist_fallback_compression_streak()
        with (patch.object(cc, "_compress_window", return_value=(1, 1)),
              patch("agent.context_compressor.call_llm", side_effect=AssertionError("No model calls"))):
            compressed, _ = compress_context(agent, messages, "Stable system", approx_tokens=300_000)
        assert len(compressed) == len(original)
        assert [m.get("content") for m in compressed] != [m.get("content") for m in original]
        assert cc._last_compression_made_progress is False  # no summary success fabricated
        assert cc._verify_compaction_cleared_threshold is True
        assert cc.awaiting_real_usage_after_compression is True
        assert cc._structural_no_op_backoff_until > 0
        assert cc._fallback_compression_streak == 1
        assert cc.should_defer_preflight_to_real_usage(300_000) is True
        assert agent.session_id == sid
        durable = db.get_messages_as_conversation(sid)
        assert [m.get("content") for m in durable] == [m.get("content") for m in compressed]
        assert len(db.get_messages(sid, include_inactive=True)) == 2 * len(original)
        agent._flush_messages_to_session_db(compressed, None)
        assert len(db.get_messages(sid)) == len(original)  # no duplicate flush
        outcome = record_response_usage(agent, _usage(tokens), messages=compressed,
            api_call_count=1, api_duration=0, compression_attempts=3, max_compression_attempts=3)
        assert outcome.rearmed is rearmed
        assert outcome.compression_attempts == (0 if rearmed else 3)
        assert cc._verify_compaction_cleared_threshold is False
        assert cc.threshold_tokens == 231_200
        assert cc.context_length == 1_050_000


@pytest.mark.parametrize("kind", ["noop", "abort", "empty", "grow", "failed_commit", "lock", "cancelled"])
def test_noncommitted_attempts_never_arm_recovery(tmp_path, kind):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        sid = "no-recovery"
        messages = _seed(db, sid, _tool_history())
        agent = _agent(db, sid)
        cc = agent.context_compressor
        original = deepcopy(messages)
        fence = CompressionCommitFence()
        def candidate(rows, **kwargs):
            if kind == "cancelled":
                fence.cancel_before_commit()
            if kind == "abort":
                cc._last_compress_aborted = True
                return rows
            if kind == "noop":
                return deepcopy(rows)
            if kind == "empty":
                return []
            changed = deepcopy(rows)
            changed[2]["content"] = "shortened output" if kind != "grow" else "growth " * 100_000
            return changed
        if kind == "lock":
            assert db.try_acquire_compression_lock(sid, "other-worker")
        with (patch.object(cc, "compress", side_effect=candidate),
              patch.object(db, "archive_and_compact", side_effect=RuntimeError("commit failed"))
              if kind == "failed_commit" else patch.object(agent, "commit_memory_session")):
            result, _ = compress_context(agent, messages, "Stable system", approx_tokens=300_000,
                                         commit_fence=fence)
        assert cc._verify_compaction_cleared_threshold is False
        assert [m.get("content") for m in db.get_messages_as_conversation(sid)] == [m.get("content") for m in original]
        if kind != "empty":
            assert [m.get("content") for m in result] == [m.get("content") for m in original]


@pytest.mark.parametrize("cache", [False, True])
def test_real_finalize_persist_resume_preserves_anchor_and_provider_prefix(tmp_path, cache):
    from agent.prompt_caching import strip_anthropic_cache_control
    with SessionDB(db_path=tmp_path / "state.db") as db:
        sid = "canonical-resume"
        db.create_session(sid, source="cli")
        agent = _agent(db, sid)
        agent.compression_enabled = False
        agent._use_prompt_caching = cache
        agent._use_native_cache_layout = False
        agent._cache_ttl = "5m"
        requests = []
        def complete(**kwargs):
            requests.append(deepcopy(kwargs))
            msg = SimpleNamespace(content="offline answer", tool_calls=None, reasoning=None,
                                  reasoning_content=None, reasoning_details=None)
            response = _usage(301_417)
            response.choices = [SimpleNamespace(message=msg, finish_reason="stop")]
            response.model = "test/model"
            return response
        agent.client = MagicMock()
        agent.client.chat.completions.create.side_effect = complete
        with (patch("hermes_cli.plugins.invoke_hook", side_effect=lambda hook, **kw:
                    [{"context": "PLUGIN-CTX"}] if hook == "pre_llm_call" else []),
              patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
            result = agent.run_conversation("API-only note\n\nclean question", conversation_history=[],
                                            persist_user_message="clean question")
        assert result["completed"] is True
        history = db.get_messages_as_conversation(sid)
        assert history[0]["content"] == "clean question"
        assert history[0]["api_content"] == "API-only note\n\nclean question\n\nPLUGIN-CTX"
        assert anchored_context_tokens(result["messages"], agent._usage_anchor) == 301_427
        assert persisted_anchor_tokens(db, sid, history) == 301_427
        fresh = _agent(db, sid)
        restore_usage_anchor(fresh, history)
        assert fresh._usage_anchor == agent._usage_anchor
        assert _preflight_request_tokens(fresh, history, "Stable system") == 301_427
        fresh.compression_enabled = False
        fresh._use_prompt_caching = cache
        fresh._use_native_cache_layout = False
        fresh._cache_ttl = "5m"
        fresh.client = MagicMock()
        fresh.client.chat.completions.create.side_effect = complete
        with patch.object(fresh, "_save_trajectory"), patch.object(fresh, "_cleanup_task_resources"):
            fresh.run_conversation("follow up", conversation_history=history)
        sent = strip_anthropic_cache_control(deepcopy(requests[0]["messages"]))
        replay = strip_anthropic_cache_control(deepcopy(requests[1]["messages"]))
        assert replay[:len(sent)] == sent
        assert all("api_content" not in m for r in requests for m in r["messages"])
        assert {k: v for k, v in requests[0].items() if k != "messages"} == {
            k: v for k, v in requests[1].items() if k != "messages"}
        assert agent._cached_system_prompt == fresh._cached_system_prompt == "Stable system"


@pytest.mark.parametrize("change", ["content", "api_content", "role", "tool_call_id", "tool_calls", "reorder"])
def test_entire_priced_prefix_rewrites_fail_closed_even_when_last_row_unchanged(tmp_path, change):
    rows = _tool_history()
    anchor = capture_usage_anchor(100_000, 20, rows)
    rewritten = deepcopy(rows)
    if change == "reorder":
        rewritten[0], rewritten[3] = rewritten[3], rewritten[0]
    else:
        rewritten[0][change] = [{"id": "new-call"}] if change == "tool_calls" else "different"
        if change == "api_content":
            rewritten[0]["role"] = "user"
    assert rewritten[-1] == rows[-1]
    assert anchored_context_tokens(rewritten, anchor) is None
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("prefix", source="cli")
        agent = _agent(db, "prefix")
        set_usage_anchor(agent, anchor)
        fresh = _agent(db, "prefix")
        restore_usage_anchor(fresh, rewritten)
        assert fresh._usage_anchor is None
        assert db.get_session_model_config_value("prefix", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is None


@pytest.mark.parametrize("sidecar", [None, "", 42, []])
def test_unusable_sidecar_keeps_content_identity(sidecar):
    msg = {"role": "user", "content": "original", "api_content": sidecar}
    anchor = capture_usage_anchor(100, 1, [msg])
    assert anchored_context_tokens([dict(msg, content="changed")], anchor) is None


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_canonical_sidecar_identity_ignores_display_metadata_but_not_wire_bytes(role):
    msg = {"role": role, "content": "display", "api_content": " exact wire bytes\n"}
    original = deepcopy(msg)
    anchor = capture_usage_anchor(100, 1, [msg])
    reloaded = dict(msg, content="clean display", timestamp=42, _row_id=7, display_kind="steer")
    assert anchored_context_tokens([reloaded], anchor) == 101
    assert message_fingerprint(msg) == message_fingerprint({"role": role, "content": msg["api_content"]})
    assert anchored_context_tokens([dict(reloaded, api_content="changed")], anchor) is None
    assert msg == original  # fingerprinting cannot mutate the cached transcript


def test_multimodal_content_rewrite_invalidates_priced_prefix():
    rows = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "unchanged last row"}]
    anchor = capture_usage_anchor(100, 1, rows)
    rewritten = deepcopy(rows)
    rewritten[0]["content"][0]["image_url"]["url"] = "data:image/png;base64,BBBB"
    assert anchored_context_tokens(rewritten, anchor) is None


def test_tool_sidecar_is_not_provider_content_and_legacy_anchor_fails_closed():
    msg = {"role": "tool", "content": "actual", "api_content": "ignored"}
    assert message_fingerprint(msg) == message_fingerprint(dict(msg, api_content="also ignored"))
    anchor = capture_usage_anchor(100, 1, [msg])
    anchor.pop("base_prefix_fp")
    assert anchored_context_tokens([msg], anchor) is None
