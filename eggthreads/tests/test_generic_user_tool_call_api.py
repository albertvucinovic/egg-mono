from __future__ import annotations

import asyncio
from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_enqueue_user_tool_call_creates_hidden_approved_ra3(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "wait",
        {"thread_ids": [tid]},
        content="eggtools.wait(...)" ,
        hidden=True,
        origin="repl",
        auto_approve=True,
    )

    states = ts.build_tool_call_states(db, tid)
    assert tcid in states
    tc = states[tcid]
    assert tc.parent_role == "user"
    assert tc.approval_decision == "granted"
    assert tc.state == "TC2.1"

    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    )
    payload = __import__("json").loads(cur.fetchone()[0])
    assert payload["no_api"] is True
    assert payload["keep_user_turn"] is True
    assert payload["origin"] == "repl"


def test_wait_for_tool_call_result_returns_details_and_timeout(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "bash", {"script": "echo hi"}, auto_approve=False)

    timed = ts.wait_for_tool_call_result(db, tid, tcid, timeout_sec=0, poll_interval=0.001)
    assert timed.timed_out is True
    assert timed.state == "TC1"
    assert timed.content is None

    db.append_event("finish", tid, "tool_call.finished", {"tool_call_id": tcid, "reason": "success", "output": "hi"})
    db.append_event("approve-output", tid, "tool_call.output_approval", {"tool_call_id": tcid, "decision": "whole", "preview": "hi"})
    db.append_event("tool-msg", tid, "msg.create", {"role": "tool", "tool_call_id": tcid, "content": "hi"})

    result = ts.wait_for_tool_call_result(db, tid, tcid, timeout_sec=0.1)
    assert result.timed_out is False
    assert result.state == "TC6"
    assert result.content == "hi"
    assert result.finished_reason == "success"
    assert result.output_decision == "whole"


def test_wait_for_tool_call_result_async(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "bash", {"script": "echo hi"}, auto_approve=False)
    db.append_event("finish", tid, "tool_call.finished", {"tool_call_id": tcid, "reason": "success", "output": "hi"})
    db.append_event("approve-output", tid, "tool_call.output_approval", {"tool_call_id": tcid, "decision": "whole", "preview": "hi"})
    db.append_event("tool-msg", tid, "msg.create", {"role": "tool", "tool_call_id": tcid, "content": "hi"})

    result = asyncio.run(ts.wait_for_tool_call_result_async(db, tid, tcid, timeout_sec=0.1))
    assert result.content == "hi"
    assert result.state == "TC6"


def test_wait_for_threads_returns_structured_results(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(db, tid, "assistant", "answer")
    ts.create_snapshot(db, tid)

    results = ts.wait_for_threads(db, [tid], timeout_sec=0)
    assert set(results) == {tid}
    result = results[tid]
    assert result.finished is True
    assert result.state == "waiting_user"
    assert result.last_assistant_message == "answer"
