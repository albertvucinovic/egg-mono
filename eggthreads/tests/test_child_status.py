from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts
from eggthreads.tools import create_default_tools


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_get_child_thread_status_reports_context_and_errors(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "system", "You are helpful")
    ts.append_message(db, child, "user", "hello world")
    ts.append_message(db, child, "assistant", "partial answer")
    ts.append_message(db, child, "system", "LLM/runner error: provider exploded")
    ts.create_snapshot(db, child)
    ts.set_context_limit(db, child, 1000, reason="test")

    st = ts.get_child_thread_status(db, parent, child)

    assert st.thread_id == child
    assert st.name == "child"
    assert st.state == "waiting_user"
    assert st.context_tokens > 0
    assert st.full_thread_tokens >= st.context_tokens
    assert st.compaction == {"compacted": False, "raw_marker_count": 0}
    assert st.context_limit == 1000
    assert st.context_limit_percent is not None
    assert st.error_count == 1
    assert st.recent_errors
    assert "provider exploded" in st.recent_errors[0]["message"]


def test_get_child_thread_status_reports_compacted_context_and_full_tokens(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    old = ts.append_message(db, child, "user", "old " * 200)
    start = ts.append_message(db, child, "assistant", "summary")
    ts.commit_thread_compaction(db, child, start, created_by="test")
    ts.create_snapshot(db, child)

    st = ts.get_child_thread_status(db, parent, child)
    payload = st.to_dict()

    assert st.context_tokens < st.full_thread_tokens
    assert payload["context_tokens"] == st.context_tokens
    assert payload["full_thread_tokens"] == st.full_thread_tokens
    assert payload["compaction"] == {
        "compacted": True,
        "current_prompt_start_msg_id": start,
        "current_prompt_start_event_seq": ts.latest_effective_thread_compaction(db, child)["start_event_seq"],
        "marker_event_seq": ts.latest_effective_thread_compaction(db, child)["event_seq"],
        "raw_marker_count": 1,
    }
    assert old


def test_get_child_thread_status_rejects_non_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    other = ts.create_root_thread(db, name="other")

    try:
        ts.get_child_thread_status(db, parent, other)
    except ValueError as e:
        assert "descendant" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_get_child_status_tool_uses_current_thread_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "hello")
    ts.append_message(db, child, "assistant", "done")
    ts.create_snapshot(db, child)

    out = create_default_tools().execute(
        "get_child_status",
        {"child_thread_ids": [child]},
        thread_id=parent,
    )
    payload = json.loads(out)

    assert payload["children"][0]["thread_id"] == child
    assert payload["children"][0]["state"] == "waiting_user"
    assert payload["children"][0]["context_tokens"] > 0
    assert payload["children"][0]["full_thread_tokens"] >= payload["children"][0]["context_tokens"]
    assert payload["children"][0]["compaction"] == {"compacted": False, "raw_marker_count": 0}


def test_get_child_status_reports_active_assistant_notes(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    note = ts.append_message(
        db,
        child,
        "assistant",
        "still working on it",
        extra={"answer_user_preserve_turn": True, "model_key": "test-model", "tool_call_id": "call-note"},
    )
    ts.create_snapshot(db, child)

    payload = ts.get_child_thread_status(db, parent, child).to_dict()

    assert payload["assistant_notes"] == [
        {
            "event_seq": payload["assistant_notes"][0]["event_seq"],
            "ts": payload["assistant_notes"][0]["ts"],
            "msg_id": note,
            "content": "still working on it",
            "model_key": "test-model",
            "tool_call_id": "call-note",
        }
    ]


def test_get_child_status_clears_assistant_notes_after_final_assistant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "assistant", "still working", extra={"answer_user_preserve_turn": True})
    ts.append_message(db, child, "assistant", "final answer")
    ts.create_snapshot(db, child)

    payload = ts.get_child_thread_status(db, parent, child).to_dict()

    assert payload["assistant_notes"] == []


def test_get_child_status_tool_defaults_to_direct_children(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")
    ts.append_message(db, child, "user", "child message")
    ts.append_message(db, grandchild, "user", "grandchild message")
    ts.create_snapshot(db, child)
    ts.create_snapshot(db, grandchild)

    out = create_default_tools().execute("get_child_status", {}, thread_id=parent)
    ids = [item["thread_id"] for item in json.loads(out)["children"]]

    assert ids == [child]


def test_get_child_status_registered_and_exposed():
    tools = create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "get_child_status" in names
