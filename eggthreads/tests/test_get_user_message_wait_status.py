from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import eggthreads as ts
from eggthreads.runner import ThreadRunner


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



def test_older_live_lease_does_not_reclaim_authority_from_newer_wait(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _start_get_user_tool_waiting(db, tid, note="Older")
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={"tool_calls": [{
            "id": "call-get-user-newer",
            "type": "function",
            "function": {"name": GET_USER_TOOL_NAME, "arguments": json.dumps({"assistant_note": "Newer"})},
        }]},
    )
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "call-get-user-newer"},
        invoke_id="invoke-newer-without-lease",
    )
    ts.append_message(
        db,
        tid,
        "assistant",
        "Newer",
        extra={
            "answer_user_preserve_turn": True,
            "source_tool_name": GET_USER_TOOL_NAME,
            "tool_call_id": "call-get-user-newer",
            "awaiting_user_message_tool_call_id": "call-get-user-newer",
        },
    )

    assert ts.get_active_get_user_message_waiting_note(db, tid) is None

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


def test_wait_observes_get_user_boundary_after_transient_heartbeat_error(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.enqueue_user_tool_call(db, parent, "wait", {"thread_ids": [child]}, hidden=True)
    child_tool_call_id = ts.enqueue_user_tool_call(
        db,
        child,
        GET_USER_TOOL_NAME,
        {"assistant_note": "HANDOFF_READY"},
        hidden=True,
    )

    parent_runner = ThreadRunner(
        db,
        parent,
        llm=object(),
        config=ts.RunnerConfig(lease_ttl_sec=2, heartbeat_sec=0.01),
    )
    child_runner = ThreadRunner(
        db,
        child,
        llm=object(),
        config=ts.RunnerConfig(lease_ttl_sec=5, heartbeat_sec=0.01),
    )
    parent_observed = asyncio.Event()
    observed = {}

    real_heartbeat = db.heartbeat
    parent_heartbeat_attempts = 0

    def flaky_heartbeat(thread_id, invoke_id, lease_until):
        nonlocal parent_heartbeat_attempts
        if thread_id == parent:
            parent_heartbeat_attempts += 1
            if parent_heartbeat_attempts == 1:
                raise sqlite3.OperationalError("database is locked")
        return real_heartbeat(thread_id, invoke_id, lease_until)

    monkeypatch.setattr(db, "heartbeat", flaky_heartbeat)

    async def parent_wait(_invoke_id, _model, _ra):
        def wait_on_worker_connection():
            worker_db = ts.ThreadsDB(db.path)
            try:
                return ts.wait_for_threads(
                    worker_db,
                    [child],
                    timeout_sec=3,
                    poll_interval=0.01,
                )
            finally:
                worker_db.conn.close()

        result = await asyncio.to_thread(wait_on_worker_connection)
        observed["child"] = result[child]
        parent_observed.set()

    async def child_handoff(invoke_id, _model, _ra):
        # Cross the original two-second parent lease. Before the repair, the
        # first transient heartbeat exception killed the heartbeat task, so the
        # parent's otherwise successful wait result could never be published.
        await asyncio.sleep(2.1)
        child_runner._owned_append(
            invoke_id,
            type_="tool_call.execution_started",
            payload={"tool_call_id": child_tool_call_id},
        )
        ts.start_get_user_message_wait(
            db,
            child,
            invoke_id=invoke_id,
            tool_call_id=child_tool_call_id,
            assistant_note="HANDOFF_READY",
        )
        await asyncio.wait_for(parent_observed.wait(), timeout=1)

    monkeypatch.setattr(parent_runner, "_run_ra_tools", parent_wait)
    monkeypatch.setattr(child_runner, "_run_ra_tools", child_handoff)

    async def run():
        return await asyncio.gather(
            parent_runner.run_once(),
            child_runner.run_once(),
            return_exceptions=True,
        )

    results = asyncio.run(run())

    assert results == [True, True]
    assert parent_heartbeat_attempts >= 2
    assert observed["child"].finished is True
    assert observed["child"].state == "waiting_user"
    assert observed["child"].last_assistant_message == "HANDOFF_READY"


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


def test_atomic_user_reply_claim_is_running_not_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _start_get_user_tool_waiting(db, child, note="What title should I use?")
    ts.append_normal_user_message(db, child, "The title")
    ts.create_snapshot(db, child)

    wait_result = ts.wait_for_threads(db, [child], timeout_sec=0)[child]
    wait_out = ts.create_default_tools().execute("wait", {"thread_ids": [child], "timeout_sec": 0.01})
    status_out = ts.create_default_tools().execute("get_child_status", {"child_thread_ids": [child]}, thread_id=parent)
    child_status = json.loads(status_out)["children"][0]

    assert wait_result.finished is False
    assert wait_result.state == "running"
    assert f"Thread {child[-8:]} not finished (state=running)." in wait_out
    assert child_status["state"] == "running"
