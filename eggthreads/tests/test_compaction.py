from __future__ import annotations

import json

import eggthreads as ts
from eggthreads.command_catalog import CommandContext, create_default_command_registry
from eggthreads.runner import ThreadRunner
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
