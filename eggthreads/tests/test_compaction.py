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


def test_show_compaction_start_tool_reports_effective_marker(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary start")
    result = ts.commit_thread_compaction(db, tid, start, created_by="test")
    assert result.success is True

    registry = create_tool_registry()
    out = registry.execute("show_compaction_start", {}, thread_id=tid, db=db)
    payload = json.loads(out)

    assert payload["thread_id"] == tid
    assert payload["raw_compaction_count"] == 1
    assert payload["effective"]["compaction_event_seq"] == result.compaction_event_seq
    assert payload["effective"]["start_msg_id"] == start
    assert payload["effective"]["start_event_seq"] == result.start_event_seq
    assert payload["effective"]["start_message"]["role"] == "assistant"
    assert payload["effective"]["start_message"]["content_preview"] == "summary start"
    assert old


def test_show_compaction_start_ignores_continued_away_marker(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    start = ts.append_message(db, tid, "assistant", "summary")
    ts.commit_thread_compaction(db, tid, start, created_by="test")
    ts.continue_thread(db, tid, old)

    status = ts.show_compaction_start(db, tid)

    assert status["raw_compaction_count"] == 1
    assert status["latest_raw_compaction_event_seq"] is not None
    assert status["effective"] is None


def test_search_compaction_sources_skips_hidden_and_bounds_output(tmp_path):
    db, tid = _new_thread(tmp_path)
    visible = ts.append_message(db, tid, "user", "needle visible detail " * 20)
    hidden_tcid = ts.execute_bash_command_hidden(db, tid, "echo needle hidden dollar-dollar")
    hidden = ts.append_message(db, tid, "tool", "needle hidden detail", extra={"no_api": True, "tool_call_id": hidden_tcid})
    start = ts.append_message(db, tid, "assistant", "summary start")
    after = ts.append_message(db, tid, "user", "needle after start should not appear")
    ts.commit_thread_compaction(db, tid, start, created_by="test")

    result = ts.search_compaction_sources(db, tid, "needle", max_results=5, max_chars=40)

    assert result["ok"] is True
    assert result["effective_start"]["start_msg_id"] == start
    assert result["matching_message_count"] == 1
    assert result["returned_chars"] <= 40
    assert [item["msg_id"] for item in result["results"]] == [visible]
    preview = result["results"][0]["content_preview"]
    assert "needle" in preview
    assert "hidden" not in json.dumps(result)
    assert hidden not in json.dumps(result)
    assert after not in json.dumps(result)


def test_fetch_compaction_source_returns_visible_pre_start_message_only(tmp_path):
    db, tid = _new_thread(tmp_path)
    secret_text = "API_KEY=sk-" + ("a" * 24)
    visible = ts.append_message(db, tid, "tool", f"old output\n{secret_text}")
    hidden = ts.append_message(db, tid, "user", "hidden old", extra={"no_api": True})
    start = ts.append_message(db, tid, "assistant", "summary start")
    ts.commit_thread_compaction(db, tid, start, created_by="test")

    fetched = ts.fetch_compaction_source(db, tid, visible, max_chars=200)
    hidden_fetch = ts.fetch_compaction_source(db, tid, hidden, max_chars=200)

    assert fetched["ok"] is True
    assert fetched["found"] is True
    assert fetched["message"]["msg_id"] == visible
    assert "old output" in fetched["content"]
    assert "sk-" + ("a" * 24) not in fetched["content"]
    assert "***" in fetched["content"] or "MASKED SECRET" in fetched["content"]
    assert hidden_fetch["ok"] is True
    assert hidden_fetch["found"] is False
    assert "not found" in hidden_fetch["error"].lower()


def test_compaction_source_tools_are_registered_and_bound_to_context(tmp_path):
    db, tid = _new_thread(tmp_path)
    visible = ts.append_message(db, tid, "user", "needle visible detail")
    start = ts.append_message(db, tid, "assistant", "summary start")
    ts.commit_thread_compaction(db, tid, start, created_by="test")

    registry = create_tool_registry()
    search_payload = json.loads(registry.execute("search_compaction_sources", {"query": "needle"}, thread_id=tid, db=db))
    fetch_payload = json.loads(registry.execute("fetch_compaction_source", {"source_id": visible}, thread_id=tid, db=db))

    assert "search_compaction_sources" in registry._tools
    assert "fetch_compaction_source" in registry._tools
    assert search_payload["results"][0]["msg_id"] == visible
    assert fetch_payload["message"]["msg_id"] == visible
    assert fetch_payload["content"] == "needle visible detail"


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
    assert "Compaction committed" in (result.message or "")
    assert seen and "Compaction committed" in seen[-1]
    assert _events(db, tid)[0][1]["start_msg_id"] == user


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


def test_maybe_auto_compact_triggers_at_threshold_with_last_llm(tmp_path):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old")
    assistant = ts.append_message(db, tid, "assistant", "assistant summary")

    result = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10)

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

    first = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10)
    second = ts.maybe_auto_compact_thread(db, tid, threshold_tokens=10, context_tokens=10)

    assert first.triggered is True
    assert second.triggered is False
    assert second.attempted is True
    assert len(_events(db, tid)) == 1


def test_runner_auto_compacts_at_ra1_boundary_before_llm(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    old = ts.append_message(db, tid, "user", "old context")
    assistant = ts.append_message(db, tid, "assistant", "previous answer")
    next_user = ts.append_message(db, tid, "user", "next question")
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

    runner = ThreadRunner(db, tid, llm=LLM(), config=RunnerConfig(auto_compact_threshold_tokens=100))
    asyncio.run(runner.run_once())

    events = _events(db, tid)
    assert len(events) == 1
    payload = events[0][1]
    assert payload["created_by"] == "auto_compaction"
    assert payload["start_msg_id"] == assistant
    assert seen_messages
    assert [m["content"] for m in seen_messages[0]] == ["previous answer", "next question"]
    provider_view = ts.filter_messages_for_compaction_provider_context(db, tid, ts.create_snapshot(db, tid)["messages"])
    assert old not in [m.get("msg_id") for m in provider_view]


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
