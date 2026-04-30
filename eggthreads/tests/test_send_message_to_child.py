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
