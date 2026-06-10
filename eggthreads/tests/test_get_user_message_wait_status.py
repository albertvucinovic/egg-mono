from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _append_event(db: ts.ThreadsDB, tid: str, type_: str, payload: dict, *, msg_id: str | None = None, invoke_id: str | None = None) -> None:
    db.append_event(
        event_id=f"{type_}-{db.max_event_seq(tid) + 1}",
        thread_id=tid,
        type_=type_,
        payload=payload,
        msg_id=msg_id,
        invoke_id=invoke_id,
    )


def _start_get_user_tool_waiting(db: ts.ThreadsDB, tid: str, *, note: str = "Please reply") -> None:
    invoke_id = "invoke-get-user"
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-get-user",
                    "type": "function",
                    "function": {
                        "name": GET_USER_TOOL_NAME,
                        "arguments": json.dumps({"assistant_note": note}),
                    },
                }
            ]
        },
    )
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "call-get-user"},
        invoke_id=invoke_id,
    )
    ts.append_message(
        db,
        tid,
        "assistant",
        note,
        extra={
            "answer_user_preserve_turn": True,
            "source_tool_name": GET_USER_TOOL_NAME,
            "tool_call_id": "call-get-user",
            "awaiting_user_message_tool_call_id": "call-get-user",
        },
    )
    ts.create_snapshot(db, tid)


def _start_normal_tool_running(db: ts.ThreadsDB, tid: str) -> None:
    invoke_id = "invoke-bash"
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-bash",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"script": "sleep 10"}),
                    },
                }
            ]
        },
    )
    _append_event(
        db,
        tid,
        "tool_call.approval",
        {"tool_call_id": "call-bash", "decision": "granted"},
    )
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "call-bash"},
        invoke_id=invoke_id,
    )
    ts.create_snapshot(db, tid)


def test_wait_for_threads_finishes_with_note_for_active_get_user_wait(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _start_get_user_tool_waiting(db, tid, note="What title should I use?")

    result = ts.wait_for_threads(db, [tid], timeout_sec=0)[tid]

    assert result.finished is True
    assert result.state == "waiting_user"
    assert result.last_assistant_message == "What title should I use?"


def test_wait_tool_inherits_active_get_user_waiting_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    _start_get_user_tool_waiting(db, tid, note="What title should I use?")

    out = ts.create_default_tools().execute("wait", {"thread_ids": [tid], "timeout_sec": 0})

    assert f"Thread {tid[-8:]} finished" in out
    assert "What title should I use?" in out


def test_get_child_status_reports_waiting_user_and_note_for_active_get_user_wait(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _start_get_user_tool_waiting(db, child, note="What title should I use?")

    out = ts.create_default_tools().execute("get_child_status", {"child_thread_ids": [child]}, thread_id=parent)
    child_status = json.loads(out)["children"][0]

    assert child_status["state"] == "waiting_user"
    assert child_status["open_invoke_id"] == "invoke-get-user"
    assert child_status["assistant_notes"][0]["content"] == "What title should I use?"
    assert child_status["assistant_notes"][0]["tool_call_id"] == "call-get-user"


def test_normal_active_tool_stream_stays_running_and_unfinished(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _start_normal_tool_running(db, child)

    wait_result = ts.wait_for_threads(db, [child], timeout_sec=0)[child]
    status = ts.get_child_thread_status(db, parent, child)

    assert wait_result.finished is False
    assert wait_result.state == "running"
    assert status.state == "running"


def test_user_reply_before_consumption_is_running_not_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _start_get_user_tool_waiting(db, child, note="What title should I use?")
    ts.append_message(db, child, "user", "The title")
    ts.create_snapshot(db, child)

    wait_result = ts.wait_for_threads(db, [child], timeout_sec=0)[child]
    wait_out = ts.create_default_tools().execute("wait", {"thread_ids": [child], "timeout_sec": 0.01})
    status_out = ts.create_default_tools().execute("get_child_status", {"child_thread_ids": [child]}, thread_id=parent)
    child_status = json.loads(status_out)["children"][0]

    assert wait_result.finished is False
    assert wait_result.state == "running"
    assert f"Thread {child[-8:]} not finished (state=running)." in wait_out
    assert child_status["state"] == "running"
