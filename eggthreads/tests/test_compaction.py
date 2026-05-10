from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.command_catalog import CommandContext, create_default_command_registry
from eggthreads.runner import RunnerConfig, ThreadRunner
from eggthreads.tools import create_tool_registry


def _new_thread(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    return db, tid


def _events(db, tid, type_="thread.compaction"):
    cur = db.conn.execute(
        "SELECT event_seq, payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
        (tid, type_),
    )
    return [(int(seq), json.loads(payload)) for seq, payload in cur.fetchall()]


def _snapshot_messages(db, tid):
    return ts.create_snapshot(db, tid)["messages"]


def test_commit_compaction_resolves_default_and_last_user(tmp_path):
    db, tid = _new_thread(tmp_path)
    user1 = ts.append_message(db, tid, "user", "first")
    assistant1 = ts.append_message(db, tid, "assistant", "reply")
    user2 = ts.append_message(db, tid, "user", "second")

    result = ts.commit_thread_compaction(db, tid, created_by="test")

    assert result.success is True
    assert result.start_msg_id == user2
    assert _events(db, tid)[0][1]["start_msg_id"] == user2

    # A second compaction can only move the provider start forward.
    assistant2 = ts.append_message(db, tid, "assistant", "after compact")
    result2 = ts.commit_thread_compaction(db, tid, "last_llm", created_by="test")
    assert result2.success is True
    assert result2.start_msg_id == assistant2

    # Starting at the old assistant would expand context, so reject it.
    result3 = ts.commit_thread_compaction(db, tid, assistant1, created_by="test")
    assert result3.success is False
    assert "would not reduce" in result3.message

    assert user1


def test_compaction_rejects_hidden_and_tool_messages(tmp_path):
    db, tid = _new_thread(tmp_path)
    hidden = ts.append_message(db, tid, "user", "secret", extra={"no_api": True})
    tool = ts.append_message(db, tid, "tool", "tool output")

    hidden_result = ts.commit_thread_compaction(db, tid, hidden, created_by="test")
    assert hidden_result.success is False
    assert "hidden" in hidden_result.message

    tool_result = ts.commit_thread_compaction(db, tid, tool, created_by="test")
    assert tool_result.success is False
    assert "role" in tool_result.message

    assert _events(db, tid) == []


def test_compact_thread_tool_is_registered_and_emits_event(tmp_path):
    db, tid = _new_thread(tmp_path)
    user = ts.append_message(db, tid, "user", "hello")

    registry = create_tool_registry()
    assert "compact_thread" in registry._tools

    out = registry.execute("compact_thread", {}, thread_id=tid, db=db)

    assert "Compaction committed" in out
    events = _events(db, tid)
    assert len(events) == 1
    assert events[0][1]["start_msg_id"] == user


def test_compact_thread_tool_schema_guides_non_spontaneous_use() -> None:
    registry = create_tool_registry()
    spec = registry._tools["compact_thread"]["spec"]["function"]
    description = spec["description"]

    assert "does not delete" in description
    assert "user asks" in description
    assert "automatic compaction" in description
    assert "context pressure" in description
    assert "do not compact" in description
    assert "write it first as normal assistant content" in description
    assert "start_message omitted" in description
    assert "last_user" in description


def test_default_compaction_tools_only_expose_compact_thread() -> None:
    registry = create_tool_registry()

    assert "compact_thread" in registry._tools
    assert "show_compaction_start" not in registry._tools
    assert "search_compaction_sources" not in registry._tools
    assert "fetch_compaction_source" not in registry._tools


def test_compact_command_uses_core_helper(tmp_path):
    db, tid = _new_thread(tmp_path)
    user = ts.append_message(db, tid, "user", "hello")
    seen: list[str] = []

    registry = create_default_command_registry()
    assert "compact" in registry.names()

    result = registry.execute(
        "compact",
        CommandContext(db=db, current_thread=tid, log_system=seen.append),
        "last_user",
    )

    assert result.clear_input is True
    assert seen and "Compaction committed" in seen[-1]
    assert _events(db, tid)[0][1]["start_msg_id"] == user


def test_compact_with_summary_command_appends_model_visible_request(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "hello")
    seen: list[str] = []
    started: list[str] = []

    registry = create_default_command_registry()
    assert "compactWithSummary" in registry.names()

    result = registry.execute(
        "compactWithSummary",
        CommandContext(db=db, current_thread=tid, log_system=seen.append, start_scheduler=started.append),
        "",
    )

    assert result.clear_input is True
    assert result.start_schedulers == (tid,)
    assert result.message is None
    assert started == [tid]
    assert seen and "Queued compaction summary request" in seen[-1]

    messages = _snapshot_messages(db, tid)
    request = messages[-1]
    assert request["role"] == "user"
    assert request["content"] == ts.COMPACTION_SUMMARY_REQUEST
    assert request["compaction_summary_request"] is True
    assert request["created_by"] == "user_command"
    assert "concise continuation summary" in request["content"]
    assert "compact_thread()" in request["content"]
    assert "start_message omitted" in request["content"]
    assert _events(db, tid) == []


def test_compact_with_summary_command_returns_confirmation_without_logger(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "hello")

    registry = create_default_command_registry()
    result = registry.execute(
        "compactWithSummary",
        CommandContext(db=db, current_thread=tid),
        "",
    )

    assert result.clear_input is True
    assert result.message is not None
    assert "Queued compaction summary request" in result.message
    assert result.start_schedulers == (tid,)
    assert _snapshot_messages(db, tid)[-1]["content"] == ts.COMPACTION_SUMMARY_REQUEST


def test_snapshot_records_event_seq_for_provider_filtering(tmp_path):
    db, tid = _new_thread(tmp_path)
    first = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary")

    snapshot = ts.create_snapshot(db, tid)
    by_id = {m["msg_id"]: m for m in snapshot["messages"]}

    assert isinstance(by_id[first]["event_seq"], int)
    assert isinstance(by_id[start]["event_seq"], int)


def test_provider_context_filter_starts_at_compaction_message(tmp_path):
    db, tid = _new_thread(tmp_path)
    system = ts.append_message(db, tid, "system", "rules")
    old = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary")
    after = ts.append_message(db, tid, "user", "after")
    hidden = ts.append_message(db, tid, "user", "hidden", extra={"no_api": True})
    ts.commit_thread_compaction(db, tid, start, created_by="test")
    snapshot = ts.create_snapshot(db, tid)

    filtered = ts.filter_messages_for_compaction_provider_context(db, tid, snapshot["messages"])

    assert [m["msg_id"] for m in filtered] == [system, start, after, hidden]
    sanitized_like_runner = [m for m in filtered if not m.get("no_api")]
    assert [m["msg_id"] for m in sanitized_like_runner] == [system, start, after]
    assert old not in [m["msg_id"] for m in filtered]


def test_runner_sanitize_keeps_compacted_provider_view(tmp_path):
    db, tid = _new_thread(tmp_path)
    system = ts.append_message(db, tid, "system", "rules")
    old = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary")
    after = ts.append_message(db, tid, "user", "after")
    ts.commit_thread_compaction(db, tid, start, created_by="test")
    snapshot = ts.create_snapshot(db, tid)

    class DummyRunner(ThreadRunner):
        def __init__(self) -> None:
            self.db = db
            self.thread_id = tid
            self.llm = None

        def _get_tool_call_id_normalization_strategy(self, model_key=None):
            return None

    compacted = ts.filter_messages_for_compaction_provider_context(db, tid, snapshot["messages"])
    out = DummyRunner()._sanitize_messages_for_api(compacted)

    assert [m.get("content") for m in out] == ["rules", "summary", "after"]
    assert old not in [m.get("msg_id") for m in compacted]


def test_compaction_allows_assistant_summary_without_tool_calls(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    summary = ts.append_message(db, tid, "assistant", "plain summary with no tool calls")

    result = ts.commit_thread_compaction(db, tid, summary, created_by="test")

    assert result.success is True
    assert result.start_msg_id == summary


def test_compaction_rejects_assistant_tool_call_start_without_complete_results(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    assistant = ts.append_message(
        db,
        tid,
        "assistant",
        "I will call tools",
        extra={
            "tool_calls": [
                {"id": "tc-one", "function": {"name": "one", "arguments": "{}"}},
                {"id": "tc-two", "function": {"name": "two", "arguments": "{}"}},
            ]
        },
    )
    ts.append_message(db, tid, "tool", "one result", extra={"tool_call_id": "tc-one"})

    result = ts.commit_thread_compaction(db, tid, assistant, created_by="test")

    assert result.success is False
    assert "tool_calls" in result.message
    assert _events(db, tid) == []


def test_compaction_rejects_start_inside_tool_result_block(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    assistant = ts.append_message(
        db,
        tid,
        "assistant",
        "I will call tools",
        extra={
            "tool_calls": [
                {"id": "tc-one", "function": {"name": "one", "arguments": "{}"}},
                {"id": "tc-two", "function": {"name": "two", "arguments": "{}"}},
            ]
        },
    )
    tool_one = ts.append_message(db, tid, "tool", "one result", extra={"tool_call_id": "tc-one"})
    ts.append_message(db, tid, "tool", "two result", extra={"tool_call_id": "tc-two"})

    assistant_result = ts.commit_thread_compaction(db, tid, assistant, created_by="test")
    # Simulate a future/advanced selector that might otherwise allow tool-role
    # starts; the protocol hardening helper should identify the real hazard as
    # starting mid assistant/tool result block.
    import eggthreads.api as api

    candidates = api._compaction_candidate_messages(db, tid)
    selected_tool = next(row for row in candidates if row[1] == tool_one)
    protocol_reason = api._compaction_protocol_rejection_reason(selected_tool, candidates)

    assert assistant_result.success is True
    assert protocol_reason == "starts inside an assistant/tool result block"
    assert len(_events(db, tid)) == 1


def test_deleted_compaction_start_marker_is_ignored_for_provider_context(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary")
    after = ts.append_message(db, tid, "user", "after")
    compacted = ts.commit_thread_compaction(db, tid, start, created_by="test")
    assert compacted.success is True

    ts.delete_message(db, tid, start)

    assert ts.latest_thread_compaction(db, tid) is not None
    assert ts.latest_effective_thread_compaction(db, tid) is None
    snapshot = ts.create_snapshot(db, tid)
    filtered = ts.filter_messages_for_compaction_provider_context(db, tid, snapshot["messages"])
    assert [m["msg_id"] for m in filtered] == [old, after]


def test_skipped_compaction_start_marker_is_ignored_for_provider_context(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary")
    after = ts.append_message(db, tid, "user", "after")
    compacted = ts.commit_thread_compaction(db, tid, start, created_by="test")
    assert compacted.success is True

    result = ts.continue_thread(db, tid, old)

    assert result.success is True
    assert start in result.skipped_msg_ids
    assert ts.latest_thread_compaction(db, tid) is not None
    assert ts.latest_effective_thread_compaction(db, tid) is None
    snapshot = ts.create_snapshot(db, tid)
    filtered = ts.filter_messages_for_compaction_provider_context(db, tid, snapshot["messages"])
    assert [m["msg_id"] for m in filtered] == [old]
    assert after in result.skipped_msg_ids


def test_continue_before_compaction_makes_control_event_ineffective(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    summary = ts.append_message(db, tid, "assistant", "summary")
    after = ts.append_message(db, tid, "user", "after")
    first = ts.commit_thread_compaction(db, tid, summary, created_by="test")
    assert first.success is True

    result = ts.continue_thread(db, tid, old)

    assert result.success is True
    assert summary in result.skipped_msg_ids
    assert after in result.skipped_msg_ids
    assert ts.latest_thread_compaction(db, tid) is not None
    assert ts.latest_effective_thread_compaction(db, tid) is None

    snapshot = ts.create_snapshot(db, tid)
    assert [m["msg_id"] for m in snapshot["messages"]] == [old]
    filtered = ts.filter_messages_for_compaction_provider_context(db, tid, snapshot["messages"])
    assert [m["msg_id"] for m in filtered] == [old]

    # Raw/audit history still contains the skipped messages and old marker.
    cur = db.conn.execute(
        "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (tid,),
    )
    assert [row[0] for row in cur.fetchall()] == [old, summary, after]
    assert len(_events(db, tid)) == 1


def test_recompaction_after_continue_uses_effective_start(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    summary = ts.append_message(db, tid, "assistant", "summary")
    ts.commit_thread_compaction(db, tid, summary, created_by="test")

    result = ts.continue_thread(db, tid, old)
    assert result.success is True
    retry_summary = ts.append_message(db, tid, "assistant", "retry summary")

    second = ts.commit_thread_compaction(db, tid, retry_summary, created_by="test")

    assert second.success is True
    assert second.start_msg_id == retry_summary
    assert ts.latest_effective_thread_compaction(db, tid)["start_msg_id"] == retry_summary

    snapshot = ts.create_snapshot(db, tid)
    filtered = ts.filter_messages_for_compaction_provider_context(db, tid, snapshot["messages"])
    assert [m["msg_id"] for m in filtered] == [retry_summary]
    assert old not in [m["msg_id"] for m in filtered]


def test_maybe_auto_compact_direct_mode_triggers_at_threshold_with_last_llm(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    assistant = ts.append_message(db, tid, "assistant", "assistant summary")

    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=False)

    assert result.triggered is True
    assert result.attempted is True
    assert result.compaction is not None
    assert result.compaction.start_msg_id == assistant
    events = _events(db, tid)
    assert len(events) == 1
    payload = events[0][1]
    assert payload["created_by"] == "auto_compaction"
    assert payload["selector"] == "last_llm"
    assert payload["start_msg_id"] == assistant
    assert old


def test_maybe_auto_compact_noops_below_threshold(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    ts.append_message(db, tid, "assistant", "assistant summary")

    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=100, context_tokens=99)

    assert result.triggered is False
    assert result.attempted is False
    assert _events(db, tid) == []


def test_maybe_auto_compact_does_not_emit_again_without_new_llm(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    ts.append_message(db, tid, "assistant", "assistant summary")

    first = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=False)
    second = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=False)

    assert first.triggered is True
    assert second.triggered is False
    assert second.attempted is True
    assert len(_events(db, tid)) == 1


def test_auto_compact_summary_mode_env_defaults_true_and_false_like() -> None:
    assert ts.auto_compact_summary_enabled({}) is True
    assert ts.auto_compact_summary_enabled({"EGG_COMPACT_SUMMARY": ""}) is True
    assert ts.auto_compact_summary_enabled({"EGG_COMPACT_SUMMARY": "yes"}) is True

    for value in ("0", "false", "no", "off", " False "):
        assert ts.auto_compact_summary_enabled({"EGG_COMPACT_SUMMARY": value}) is False


def test_maybe_auto_compact_summary_mode_appends_request_and_marker_once(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old context")
    ts.append_message(db, tid, "assistant", "previous answer")

    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)
    duplicate = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)

    assert result.triggered is True
    assert result.attempted is True
    assert result.compaction is None
    assert duplicate.triggered is False
    assert duplicate.attempted is True
    assert _events(db, tid) == []

    messages = _snapshot_messages(db, tid)
    assert len([m for m in messages if m.get("auto_compaction_request")]) == 1
    request = messages[-1]
    assert request["role"] == "user"
    assert request["created_by"] == "auto_compaction"
    assert "concise continuation summary" in request["content"]
    assert "compact_thread()" in request["content"]
    assert "start_message omitted" in request["content"]

    markers = ts.list_thread_compaction_summary_in_progress_events(db, tid)
    assert len(markers) == 1
    assert markers[0]["created_by"] == "auto_compaction"
    assert markers[0]["request_msg_id"] == request["msg_id"]
    assert ts.latest_effective_thread_compaction_summary_in_progress(db, tid)["event_seq"] == markers[0]["event_seq"]


def test_maybe_auto_compact_defaults_to_summary_mode(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old context")
    ts.append_message(db, tid, "assistant", "previous answer")
    monkeypatch.delenv("EGG_COMPACT_SUMMARY", raising=False)

    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10)

    assert result.triggered is True
    assert _events(db, tid) == []
    assert len(ts.list_thread_compaction_summary_in_progress_events(db, tid)) == 1


def test_maybe_auto_compact_summary_mode_noops_below_threshold(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    ts.append_message(db, tid, "assistant", "assistant summary")

    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=100, context_tokens=99, summary_mode=True)

    assert result.triggered is False
    assert result.attempted is False
    assert _events(db, tid) == []
    assert ts.list_thread_compaction_summary_in_progress_events(db, tid) == []


def test_compaction_summary_marker_uses_effective_continue_view(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    ts.append_message(db, tid, "assistant", "previous answer")
    first = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)
    assert first.triggered is True
    assert ts.has_effective_thread_compaction_summary_in_progress(db, tid) is True

    continued = ts.continue_thread(db, tid, old)
    assert continued.success is True
    assert ts.latest_effective_thread_compaction_summary_in_progress(db, tid) is None

    ts.append_message(db, tid, "assistant", "retry answer")
    retry = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)

    assert retry.triggered is True
    assert len(ts.list_thread_compaction_summary_in_progress_events(db, tid)) == 2


def test_compaction_summary_marker_cleared_by_later_compaction_until_new_context(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    ts.append_message(db, tid, "assistant", "previous answer")
    request_result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)
    assert request_result.triggered is True

    summary_msg = ts.append_message(db, tid, "assistant", "Concise continuation summary")
    compacted = ts.commit_thread_compaction(db, tid, created_by="assistant_tool")
    after_compaction = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)

    assert compacted.success is True
    assert compacted.start_msg_id == summary_msg
    assert ts.latest_effective_thread_compaction_summary_in_progress(db, tid) is None
    assert after_compaction.triggered is False
    assert after_compaction.attempted is True
    assert len(ts.list_thread_compaction_summary_in_progress_events(db, tid)) == 1

    ts.append_message(db, tid, "user", "new useful context")
    new_request = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)
    assert new_request.triggered is True
    assert len(ts.list_thread_compaction_summary_in_progress_events(db, tid)) == 2


def test_compaction_summary_mode_waits_for_post_compaction_user_context(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old")
    summary_msg = ts.append_message(db, tid, "assistant", "summary")
    compacted = ts.commit_thread_compaction(db, tid, created_by="assistant_tool")
    assert compacted.success is True
    assert compacted.start_msg_id == summary_msg

    assistant_only = ts.append_message(db, tid, "assistant", "assistant-only follow-up")
    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10, summary_mode=True)

    assert result.triggered is False
    assert result.attempted is True
    assert ts.list_thread_compaction_summary_in_progress_events(db, tid) == []
    assert assistant_only


def test_auto_compact_threshold_thread_override_takes_precedence_and_continue_can_erase(tmp_path):
    db, tid = _new_thread(tmp_path)
    first = ts.append_message(db, tid, "user", "first")

    event_seq = ts.set_thread_compaction_context_length(db, tid, 123, created_by="test")
    resolved = ts.resolve_auto_compact_threshold(db, tid, explicit_threshold_tokens=456, environ={})

    assert resolved.enabled is True
    assert resolved.threshold_tokens == 123
    assert resolved.source == "thread_event"
    events = ts.list_thread_compaction_context_lengths(db, tid)
    assert events[-1]["event_seq"] == event_seq
    assert events[-1]["threshold_tokens"] == 123
    assert events[-1]["created_by"] == "test"
    assert "created_at" in events[-1]

    ts.set_thread_compaction_context_length(db, tid, 0, created_by="test")
    disabled = ts.resolve_auto_compact_threshold(db, tid, explicit_threshold_tokens=456, environ={})
    assert disabled.enabled is False
    assert disabled.threshold_tokens is None
    assert disabled.source == "thread_event"

    ts.continue_thread(db, tid, first)
    resolved_after_continue = ts.resolve_auto_compact_threshold(db, tid, explicit_threshold_tokens=456, environ={})
    assert resolved_after_continue.enabled is True
    assert resolved_after_continue.threshold_tokens == 456
    assert resolved_after_continue.source == "runner_config"


def test_auto_compact_threshold_precedence_config_model_env_default_and_disable(tmp_path):
    db, tid = _new_thread(tmp_path)
    concrete = {
        "providers": {
            "test": {
                "models": {
                    "ModelA": {
                        "model_name": "model-a",
                        "max_tokens": 1000,
                    }
                }
            }
        }
    }
    ts.set_thread_model(db, tid, "ModelA", concrete_model_info=concrete, reason="test")

    explicit = ts.resolve_auto_compact_threshold(db, tid, explicit_threshold_tokens=222, environ={})
    assert (explicit.enabled, explicit.threshold_tokens, explicit.source) == (True, 222, "runner_config")

    explicit_disabled = ts.resolve_auto_compact_threshold(db, tid, explicit_threshold_tokens=0, environ={})
    assert (explicit_disabled.enabled, explicit_disabled.threshold_tokens, explicit_disabled.source) == (False, None, "runner_config")

    model = ts.resolve_auto_compact_threshold(db, tid, environ={"EGG_AUTO_COMPACT_THRESHOLD_TOKENS": "333"})
    assert (model.enabled, model.threshold_tokens, model.source) == (True, 800, "model_max_tokens")

    env_db, env_tid = _new_thread(tmp_path / "env")
    env = ts.resolve_auto_compact_threshold(env_db, env_tid, environ={"EGG_AUTO_COMPACT_THRESHOLD_TOKENS": "333"})
    assert (env.enabled, env.threshold_tokens, env.source) == (True, 333, "env")

    env_disabled = ts.resolve_auto_compact_threshold(env_db, env_tid, environ={"EGG_AUTO_COMPACT_THRESHOLD_TOKENS": "0"})
    assert (env_disabled.enabled, env_disabled.threshold_tokens, env_disabled.source) == (False, None, "env")

    default = ts.resolve_auto_compact_threshold(env_db, env_tid, environ={})
    assert (default.enabled, default.threshold_tokens, default.source) == (True, 150000, "default")


def test_runner_auto_compacts_summary_mode_at_ra1_boundary_before_llm(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old context")
    ts.append_message(db, tid, "assistant", "previous answer")
    ts.append_message(db, tid, "user", "next question")
    ts.create_snapshot(db, tid)
    seen_messages: list[list[dict]] = []

    class LLM:
        current_model_key = "test-model"

        async def astream_chat(self, messages, **kwargs):
            seen_messages.append(messages)
            yield {"type": "done", "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setattr(
        "eggthreads.token_count.provider_context_token_stats",
        lambda db_arg, tid_arg: {"context_tokens": 100},
    )
    monkeypatch.delenv("EGG_COMPACT_SUMMARY", raising=False)

    runner = ThreadRunner(db, tid, llm=LLM(), config=RunnerConfig(auto_compact_threshold_tokens=100))
    asyncio.run(runner.run_once())

    assert _events(db, tid) == []
    markers = ts.list_thread_compaction_summary_in_progress_events(db, tid)
    assert len(markers) == 1
    messages = _snapshot_messages(db, tid)
    request_messages = [m for m in messages if m.get("auto_compaction_request")]
    assert len(request_messages) == 1
    assert markers[0]["request_msg_id"] == request_messages[0]["msg_id"]
    assert seen_messages
    assert [m["content"] for m in seen_messages[0]] == [
        "old context",
        "previous answer",
        "next question",
        request_messages[0]["content"],
    ]


def test_runner_auto_compacts_direct_mode_when_summary_env_false(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old context")
    assistant = ts.append_message(db, tid, "assistant", "previous answer")
    ts.append_message(db, tid, "user", "next question")
    ts.create_snapshot(db, tid)
    seen_messages: list[list[dict]] = []

    class LLM:
        current_model_key = "test-model"

        async def astream_chat(self, messages, **kwargs):
            seen_messages.append(messages)
            yield {"type": "done", "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setenv("EGG_COMPACT_SUMMARY", "0")
    monkeypatch.setattr(
        "eggthreads.token_count.provider_context_token_stats",
        lambda db_arg, tid_arg: {"context_tokens": 100},
    )

    runner = ThreadRunner(db, tid, llm=LLM(), config=RunnerConfig(auto_compact_threshold_tokens=100))
    asyncio.run(runner.run_once())

    events = _events(db, tid)
    assert len(events) == 1
    payload = events[0][1]
    assert payload["created_by"] == "auto_compaction"
    assert payload["selector"] == "last_llm"
    assert payload["start_msg_id"] == assistant
    assert ts.list_thread_compaction_summary_in_progress_events(db, tid) == []
    assert seen_messages
    assert [m["content"] for m in seen_messages[0]] == ["previous answer", "next question"]
    provider_view = ts.filter_messages_for_compaction_provider_context(db, tid, _snapshot_messages(db, tid))
    assert old not in [m.get("msg_id") for m in provider_view]


def test_runner_auto_compacts_from_model_threshold_without_explicit_config(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old context")
    ts.append_message(db, tid, "assistant", "previous answer")
    ts.append_message(db, tid, "user", "next question")
    ts.set_thread_model(
        db,
        tid,
        "ModelA",
        concrete_model_info={
            "providers": {
                "test": {
                    "models": {
                        "ModelA": {
                            "model_name": "model-a",
                            "max_tokens": 100,
                        }
                    }
                }
            }
        },
        reason="test",
    )

    class LLM:
        current_model_key = "ModelA"

        async def astream_chat(self, messages, **kwargs):
            yield {"type": "done", "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setattr(
        "eggthreads.token_count.provider_context_token_stats",
        lambda db_arg, tid_arg: {"context_tokens": 80},
    )
    monkeypatch.delenv("EGG_COMPACT_SUMMARY", raising=False)

    runner = ThreadRunner(db, tid, llm=LLM(), config=RunnerConfig())
    asyncio.run(runner.run_once())

    assert _events(db, tid) == []
    assert len(ts.list_thread_compaction_summary_in_progress_events(db, tid)) == 1
    assert len([m for m in _snapshot_messages(db, tid) if m.get("auto_compaction_request")]) == 1


def test_runner_thread_override_can_disable_auto_compaction(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "old context")
    ts.append_message(db, tid, "assistant", "previous answer")
    ts.append_message(db, tid, "user", "next question")
    ts.set_thread_compaction_context_length(db, tid, 0, created_by="test")

    calls: list[str] = []
    monkeypatch.setattr(
        "eggthreads.token_count.provider_context_token_stats",
        lambda db_arg, tid_arg: calls.append("token") or {"context_tokens": 100},
    )

    class LLM:
        current_model_key = "test-model"

        async def astream_chat(self, messages, **kwargs):
            yield {"type": "done", "message": {"role": "assistant", "content": "done"}}

    runner = ThreadRunner(db, tid, llm=LLM(), config=RunnerConfig(auto_compact_threshold_tokens=10))
    asyncio.run(runner.run_once())

    assert calls == []
    assert _events(db, tid) == []


def test_runner_does_not_auto_compact_during_tool_turn(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    tool_call_id = "tc-auto-defers"
    db.append_event(
        event_id="assistant-tool-parent",
        thread_id=tid,
        type_="msg.create",
        msg_id="assistant-msg",
        payload={
            "role": "assistant",
            "content": "running",
            "tool_calls": [{"id": tool_call_id, "function": {"name": "compact_thread", "arguments": "{}"}}],
        },
    )
    db.append_event(
        event_id="tool-approved",
        thread_id=tid,
        type_="tool_call.approval",
        payload={"tool_call_id": tool_call_id, "decision": "denied", "reason": "test"},
    )

    calls: list[str] = []
    monkeypatch.setattr(
        "eggthreads.token_count.provider_context_token_stats",
        lambda db_arg, tid_arg: calls.append("token") or {"context_tokens": 100},
    )

    runner = ThreadRunner(db, tid, llm=object(), config=RunnerConfig(auto_compact_threshold_tokens=100))
    asyncio.run(runner.run_once())

    assert calls == []
    assert _events(db, tid) == []


def test_provider_context_token_stats_uses_effective_compaction_not_raw_history(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old " * 200)
    start = ts.append_message(db, tid, "assistant", "summary")
    ts.commit_thread_compaction(db, tid, start, created_by="test")
    ts.create_snapshot(db, tid)

    full = ts.total_token_stats(db, tid)
    provider = ts.provider_context_token_stats(db, tid)

    assert full["context_tokens"] > provider["context_tokens"]
    assert old in full["per_message"]
    assert old not in provider["per_message"]
    assert start in provider["per_message"]


def test_thread_token_stats_reports_provider_context_and_full_history(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old " * 200)
    start = ts.append_message(db, tid, "assistant", "summary")
    ts.commit_thread_compaction(db, tid, start, created_by="test")
    ts.create_snapshot(db, tid)

    stats = ts.thread_token_stats(db, tid)

    assert stats["context_tokens"] < stats["full_thread_tokens"]
    assert start in stats["provider_per_message"]
    assert old not in stats["provider_per_message"]
    assert old in stats["per_message"]


def _ids(messages):
    return [m.get("msg_id") for m in messages]


def test_build_repl_thread_context_splits_visible_old_and_current_messages(tmp_path):
    db, tid = _new_thread(tmp_path)
    system = ts.append_message(db, tid, "system", "rules")
    old = ts.append_message(db, tid, "user", "old visible context")
    start = ts.append_message(db, tid, "assistant", "compact summary")
    current = ts.append_message(db, tid, "user", "current question")
    hidden = ts.append_message(db, tid, "user", "hidden secret", extra={"no_api": True})
    ts.commit_thread_compaction(db, tid, start, created_by="test")

    ctx = ts.build_repl_thread_context(db, tid)

    assert _ids(ctx["all_messages"]) == [system, old, start, current]
    assert hidden not in ctx["messages_by_id"]
    assert _ids(ctx["current_prompt_messages"]) == [system, start, current]
    provider_view = ts.filter_messages_for_compaction_provider_context(db, tid, ts.create_snapshot(db, tid)["messages"])
    provider_view = [m for m in provider_view if not m.get("no_api")]
    assert _ids(ctx["current_prompt_messages"]) == _ids(provider_view)
    assert _ids(ctx["older_messages_not_in_prompt"]) == [old]
    assert ctx["context_files"] == {}
    assert "older_messages_not_in_prompt" in ctx["how_to_use"]
    assert "effective_compaction" not in ctx
    assert "effective_compaction" not in ctx["how_to_use"]


def test_build_repl_thread_context_groups_messages_by_role(tmp_path):
    db, tid = _new_thread(tmp_path)
    system = ts.append_message(db, tid, "system", "rules")
    user = ts.append_message(db, tid, "user", "question")
    assistant = ts.append_message(db, tid, "assistant", "I will call a tool", extra={
        "tool_calls": [{"id": "tc-repl-role", "function": {"name": "bash", "arguments": "{}"}}]
    })
    tool = ts.append_message(db, tid, "tool", "tool output", extra={"tool_call_id": "tc-repl-role"})

    ctx = ts.build_repl_thread_context(db, tid)

    grouped = ctx["messages_by_role"]
    assert _ids(grouped["system"]) == [system]
    assert _ids(grouped["user"]) == [user]
    assert _ids(grouped["assistant"]) == [assistant]
    assert _ids(grouped["tool"]) == [tool]
    assert ctx["messages_by_id"][assistant]["tool_calls"][0]["id"] == "tc-repl-role"
    assert ctx["messages_by_id"][tool]["tool_call_id"] == "tc-repl-role"


def test_build_repl_thread_context_compactions_array_marks_current(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    first_start = ts.append_message(db, tid, "assistant", "first summary")
    first = ts.commit_thread_compaction(db, tid, first_start, created_by="test")
    second_start = ts.append_message(db, tid, "user", "new start")
    second = ts.commit_thread_compaction(db, tid, "last_user", created_by="user_command")

    ctx = ts.build_repl_thread_context(db, tid)

    assert len(ctx["compactions"]) == 2
    assert [c["is_current"] for c in ctx["compactions"]] == [False, True]
    assert ctx["compactions"][0]["marker_event_seq"] == first.compaction_event_seq
    assert ctx["compactions"][0]["current_prompt_starts_at_msg_id"] == first_start
    assert ctx["compactions"][1]["marker_event_seq"] == second.compaction_event_seq
    assert ctx["compactions"][1]["current_prompt_starts_at_msg_id"] == second_start
    assert ctx["compactions"][1]["selector_used"] == "last_user"
    assert ctx["compactions"][1]["created_by"] == "user_command"
    assert old in ctx["messages_by_id"]


def test_build_repl_thread_context_excludes_no_api_and_sanitizes_tool_output(tmp_path):
    db, tid = _new_thread(tmp_path)
    visible_tool_parent = ts.append_message(db, tid, "assistant", "call", extra={
        "tool_calls": [{"id": "tc-visible", "function": {"name": "bash", "arguments": "{}"}}]
    })
    visible_tool = ts.append_message(
        db,
        tid,
        "tool",
        "API_KEY=supersecretvalue\n\x1b[31mred\x1b[0m",
        extra={"tool_call_id": "tc-visible"},
    )
    hidden_user = ts.append_message(db, tid, "user", "hidden", extra={"no_api": True})
    hidden_tool = ts.append_message(db, tid, "tool", "hidden tool", extra={"no_api": True, "tool_call_id": "tc-hidden"})

    ctx = ts.build_repl_thread_context(db, tid)

    assert visible_tool_parent in ctx["messages_by_id"]
    assert visible_tool in ctx["messages_by_id"]
    assert hidden_user not in ctx["messages_by_id"]
    assert hidden_tool not in ctx["messages_by_id"]
    content = ctx["messages_by_id"][visible_tool]["content"]
    assert "\x1b" not in content
    assert "API_KEY=supersecretvalue" in content


def test_build_repl_thread_context_uses_effective_view_after_continue(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    summary = ts.append_message(db, tid, "assistant", "summary")
    after = ts.append_message(db, tid, "user", "after")
    first = ts.commit_thread_compaction(db, tid, summary, created_by="test")
    assert first.success is True

    result = ts.continue_thread(db, tid, old)
    assert result.success is True

    ctx = ts.build_repl_thread_context(db, tid)

    assert _ids(ctx["all_messages"]) == [old]
    assert _ids(ctx["current_prompt_messages"]) == [old]
    assert ctx["older_messages_not_in_prompt"] == []
    assert ctx["compactions"] == []
    assert summary not in ctx["messages_by_id"]
    assert after not in ctx["messages_by_id"]
