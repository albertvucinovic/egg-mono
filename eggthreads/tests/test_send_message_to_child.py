from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts
from eggthreads.tools import create_default_tools


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_send_message_to_child_api_appends_user_message_to_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "system", "system")
    ts.append_message(db, child, "user", "initial")
    ts.append_message(db, child, "assistant", "done")
    ts.create_snapshot(db, child)

    msg_id = ts.send_message_to_child_thread(db, parent, child, "please refine")

    assert msg_id
    row = db.get_thread(child)
    assert row and row.snapshot_json
    messages = json.loads(row.snapshot_json)["messages"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "please refine"
    assert messages[-1]["origin"] == "manager_message"
    assert messages[-1]["from_thread_id"] == parent


def test_send_message_to_child_api_rejects_non_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    other = ts.create_root_thread(db, name="other")

    try:
        ts.send_message_to_child_thread(db, parent, other, "nope")
    except ValueError as e:
        assert "descendant" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_send_message_to_child_tool_uses_current_thread_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "system", "system")
    ts.append_message(db, child, "user", "initial")
    ts.append_message(db, child, "assistant", "done")
    ts.create_snapshot(db, child)

    out = create_default_tools().execute(
        "send_message_to_child",
        {"child_thread_id": child, "message": "next step"},
        thread_id=parent,
    )

    assert "Sent message" in out
    messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
    assert messages[-1]["content"] == "next step"


def test_send_message_to_child_registered_and_exposed():
    tools = create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "send_message_to_child" in names


def test_continue_child_thread_api_continues_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "initial")
    assistant_msg = ts.append_message(db, child, "assistant", "bad partial")
    ts.append_message(db, child, "system", "LLM/runner error: provider exploded")
    ts.create_snapshot(db, child)

    result = ts.continue_child_thread(db, parent, child, msg_id=assistant_msg)

    assert result.success is True
    assert result.continue_from_msg_id == assistant_msg
    messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
    assert [m["content"] for m in messages] == ["initial", "bad partial"]


def test_continue_child_thread_api_rejects_non_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    other = ts.create_root_thread(db, name="other")

    result = ts.continue_child_thread(db, parent, other)

    assert result.success is False
    assert "descendant" in result.message


def test_continue_subthread_tool_uses_current_thread_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "initial")
    assistant_msg = ts.append_message(db, child, "assistant", "bad partial")
    ts.append_message(db, child, "system", "LLM/runner error: provider exploded")
    ts.create_snapshot(db, child)

    out = create_default_tools().execute(
        "continue_subthread",
        {"child_thread_id": child, "msg_id": assistant_msg},
        thread_id=parent,
    )
    payload = json.loads(out)

    assert payload["success"] is True
    assert payload["continue_from_msg_id"] == assistant_msg


def test_continue_subthread_registered_and_exposed():
    tools = create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "continue_subthread" in names
