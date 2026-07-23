from __future__ import annotations

import asyncio
import json
from pathlib import Path
import pytest

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


def test_synthetic_user_tool_call_is_full_approved_and_no_api(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tool_call_id = "01KYSYNTHETICTOOLCALL000001"

    tcid = ts.enqueue_synthetic_user_tool_call(
        db,
        tid,
        "python_exec",
        {"script": "import json\nprint(json.dumps({'valid': True}))", "timeout": 5},
        origin="test-suite",
        tool_call_id=tool_call_id,
    )

    assert tcid == tool_call_id
    payload = json.loads(
        db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create'",
            (tid,),
        ).fetchone()[0]
    )
    assert payload["no_api"] is True
    assert payload["keep_user_turn"] is True
    assert payload["synthetic_user_tool_request"] is True
    assert payload["synthetic_user_tool_request_version"] == "1"
    assert payload["origin"] == "test-suite"
    assert payload["content"] == (
        "Generated auto-approved user tool request as a synthetic message:\n\n"
        "- Tool: `python_exec`\n"
        f"- Tool-call ID: `{tool_call_id}`\n"
        "- Origin: `test-suite`\n"
        "- Approval: `auto-approved`\n"
        "- Provider visibility: `no_api`\n"
        "- Keep user turn: `true`\n\n"
        "Arguments:\n\n"
        "```json\n{\n  \"timeout\": 5\n}\n```\n\n"
        "`script`:\n\n```python\n"
        "import json\nprint(json.dumps({'valid': True}))\n```"
    )
    assert ts.build_tool_call_states(db, tid)[tcid].approval_decision == "granted"


def test_record_synthetic_user_tool_call_is_complete_and_not_runnable(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    tcid = ts.record_synthetic_user_tool_call(
        db,
        tid,
        "python_repl",
        {"code": "print('once')", "repl_name": "default"},
        "once\n",
        origin="repl_eval",
        extra={"runtime": True},
    )

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.state == "TC6"
    assert state.finished_output == "once\n"
    assert state.output_decision == "whole"
    assert ts.discover_runner_actionable(db, tid) is None
    assert ts.get_user_command_result(db, tid, tcid) == "once\n"

    messages = [
        json.loads(row[0])
        for row in db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq",
            (tid,),
        )
    ]
    assert [message["role"] for message in messages] == ["user", "tool"]
    assert messages[0]["synthetic_user_tool_request"] is True
    assert messages[0]["runtime"] is True
    assert messages[1]["tool_call_id"] == tcid
    assert messages[1]["content"] == "once\n"


def test_synthetic_user_tool_call_renders_generic_arguments_as_pretty_json(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    ts.enqueue_synthetic_user_tool_call(
        db,
        tid,
        "wait",
        {"thread_ids": ["child"], "timeout": 30},
        origin="test-suite",
    )

    payload = json.loads(
        db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create'",
            (tid,),
        ).fetchone()[0]
    )
    assert (
        "```json\n{\n  \"thread_ids\": [\n    \"child\"\n  ],\n"
        "  \"timeout\": 30\n}\n```" in payload["content"]
    )


def test_synthetic_request_is_not_duplicated_into_tool_result(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    registry = ts.ToolRegistry()
    registry.register(
        "echo",
        "Echo",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda arguments: arguments["value"],
    )
    tcid = ts.enqueue_synthetic_user_tool_call(
        db, tid, "echo", {"value": "result only"}, origin="test-suite"
    )
    runner = ts.ThreadRunner(
        db,
        tid,
        llm=object(),
        config=ts.RunnerConfig(no_api_calls=True),
        tools=registry,
    )
    while ts.build_tool_call_states(db, tid)[tcid].state != "TC6":
        assert asyncio.run(runner.run_once()) is True

    result = ts.wait_for_tool_call_result(db, tid, tcid, timeout_sec=0)
    assert result.content == "result only"
    payload = json.loads(
        db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' "
            "AND json_extract(payload_json, '$.tool_call_id')=?",
            (tid, tcid),
        ).fetchone()[0]
    )
    assert payload["content"] == "result only"
    assert payload["no_api"] is True


def test_visible_user_tool_request_keeps_existing_command_plus_result(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    registry = ts.ToolRegistry()
    registry.register(
        "echo",
        "Echo",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda arguments: arguments["value"],
    )
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "echo",
        {"value": "result"},
        content="$ echo visible",
        hidden=False,
        auto_approve=True,
    )
    runner = ts.ThreadRunner(db, tid, llm=object(), tools=registry)
    while ts.build_tool_call_states(db, tid)[tcid].state != "TC6":
        assert asyncio.run(runner.run_once()) is True

    result = ts.wait_for_tool_call_result(db, tid, tcid, timeout_sec=0)
    assert result.content == "$ echo visible\n\nresult"


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


def test_wait_for_tool_call_result_does_not_emit_countdown_summary_events(tmp_path, monkeypatch):
    import eggthreads.api as api

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "bash", {"script": "echo hi"}, auto_approve=False)

    now = [1000.0]

    def fake_time():
        return now[0]

    def fake_sleep(seconds):
        now[0] += float(seconds)

    monkeypatch.setattr(api.time, "time", fake_time)
    monkeypatch.setattr(api.time, "sleep", fake_sleep)

    result = ts.wait_for_tool_call_result(db, tid, tcid, timeout_sec=6, poll_interval=1)

    assert result.timed_out is True
    rows = db.conn.execute(
        "SELECT 1 FROM events WHERE thread_id=? AND type='tool_call.summary'",
        (tid,),
    ).fetchall()
    assert rows == []


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


def test_wait_for_threads_normalizes_tool_output_wrapped_thread_id(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(db, tid, "assistant", "answer")
    ts.create_snapshot(db, tid)

    wrapped = f"eggtools.spawn_agent_auto(...)\n\n{tid}"
    results = ts.wait_for_threads(db, [wrapped], timeout_sec=0)

    assert set(results) == {tid}
    assert results[tid].finished is True
    assert results[tid].last_assistant_message == "answer"


def test_wait_tool_normalizes_tool_output_wrapped_thread_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(db, tid, "assistant", "answer")
    ts.create_snapshot(db, tid)

    out = ts.create_default_tools().execute(
        "wait",
        {"thread_ids": [f"eggtools.spawn_agent_auto(...)\n\n{tid}"], "timeout_sec": 0},
    )

    assert f"Thread {tid[-8:]} finished" in out
    assert "answer" in out


def test_wait_for_threads_does_not_block_for_missing_thread(tmp_path):
    db = _make_db(tmp_path)
    missing = "01K000000000000000MISSING"

    results = ts.wait_for_threads(db, [missing], timeout_sec=None)

    assert results[missing].finished is False
    assert results[missing].state == "not_found"


def test_wait_tool_reports_missing_thread_without_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    missing = "01K000000000000000MISSING"

    out = ts.create_default_tools().execute("wait", {"thread_ids": [missing]}, timeout_sec=30)

    assert "not found; not waiting" in out


def test_cancelled_wait_tool_call_records_interrupted_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    ts.append_message(db, child, "user", "still working")
    ts.create_snapshot(db, child)

    tcid = "tc-wait-cancelled"
    ts.append_message(
        db,
        root,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tcid,
                    "type": "function",
                    "function": {
                        "name": "wait",
                        "arguments": json.dumps({"thread_ids": [child], "timeout_sec": 60}),
                    },
                }
            ]
        },
    )
    db.append_event("approve", root, "tool_call.approval", {"tool_call_id": tcid, "decision": "granted"})

    async def run_and_cancel():
        runner = ts.ThreadRunner(db, root, llm=object(), config=ts.RunnerConfig())
        task = asyncio.create_task(runner.run_once())
        await asyncio.sleep(0.05)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)

    asyncio.run(run_and_cancel())

    states = ts.build_tool_call_states(db, root)
    assert states[tcid].state == "TC5"
    assert states[tcid].finished_reason == "interrupted"
    assert states[tcid].output_decision == "whole"

    asyncio.run(ts.ThreadRunner(db, root, llm=object(), config=ts.RunnerConfig()).run_once())

    states = ts.build_tool_call_states(db, root)
    assert states[tcid].state == "TC6"


def test_closed_tool_stream_without_finished_result_is_recoverable(tmp_path):
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    tcid = "tc-closed-without-finished"
    invoke_id = "invoke-closed"

    ts.append_message(
        db,
        root,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tcid,
                    "type": "function",
                    "function": {"name": "wait", "arguments": json.dumps({"thread_ids": [root], "timeout_sec": 60})},
                }
            ]
        },
    )
    db.append_event("approve", root, "tool_call.approval", {"tool_call_id": tcid, "decision": "granted"})
    db.append_event("stream-open", root, "stream.open", {"stream_kind": "tool"}, msg_id="stream-msg", invoke_id=invoke_id)
    db.append_event("started", root, "tool_call.execution_started", {"tool_call_id": tcid}, invoke_id=invoke_id)
    db.append_event("stream-close", root, "stream.close", {}, invoke_id=invoke_id)

    states = ts.build_tool_call_states(db, root)
    assert states[tcid].state == "TC4"
    assert states[tcid].finished_reason == "interrupted"

    result = ts.finalize_tool_output(
        db,
        root,
        tcid,
        decision="whole",
        source="automatic_synthetic",
        reason="Recovered closed tool stream",
        expected_event_seq=states[tcid].state_event_seq,
    )
    assert result.committed is True
    asyncio.run(ts.ThreadRunner(db, root, llm=object(), config=ts.RunnerConfig()).run_once())

    states = ts.build_tool_call_states(db, root)
    assert states[tcid].state == "TC6"


def test_wait_for_threads_does_not_finish_when_llm_turn_is_actionable(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "work to do")
    ts.create_snapshot(db, child)

    results = ts.wait_for_threads(db, [child], timeout_sec=0)
    result = results[child]

    assert result.finished is False
    assert result.last_assistant_message == ""


def test_wait_for_threads_waits_for_new_llm_turn_when_old_answer_exists(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(db, tid, "assistant", "answer")
    ts.create_snapshot(db, tid)
    ts.append_message(db, tid, "user", "follow up")

    results = ts.wait_for_threads(db, [tid], timeout_sec=0)
    result = results[tid]

    assert result.finished is False
    assert result.last_assistant_message == "answer"


def test_wait_for_threads_treats_llm_error_after_tool_message_as_completion(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = "tc-after-error"
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tcid,
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"script": "echo hi"})},
                }
            ]
        },
    )
    db.append_event("approve", tid, "tool_call.approval", {"tool_call_id": tcid, "decision": "granted"})
    db.append_event("finish", tid, "tool_call.finished", {"tool_call_id": tcid, "reason": "success", "output": "hi"})
    db.append_event("output-approval", tid, "tool_call.output_approval", {"tool_call_id": tcid, "decision": "whole", "preview": "hi"})
    ts.append_message(db, tid, "tool", "hi", extra={"tool_call_id": tcid})
    invoke_id = "llm-error-invoke"
    db.append_event("llm-open", tid, "stream.open", {"stream_kind": "llm"}, msg_id="llm-stream", invoke_id=invoke_id)
    db.append_event("llm-delta", tid, "stream.delta", {"reason": "LLM/runner error: disconnected"}, invoke_id=invoke_id, chunk_seq=0)
    ts.append_message(db, tid, "system", "LLM/runner error: disconnected")
    db.append_event("llm-close", tid, "stream.close", {}, invoke_id=invoke_id)
    ts.create_snapshot(db, tid)

    results = ts.wait_for_threads(db, [tid], timeout_sec=0)
    result = results[tid]

    assert result.finished is True
    assert result.state == "waiting_user"


def test_wait_for_threads_releases_expired_open_stream_before_completion_check(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(db, tid, "assistant", "answer")
    db.try_open_stream(tid, "stale-invoke", "2000-01-01 00:00:00", owner="stale", purpose="llm")

    results = ts.wait_for_threads(db, [tid], timeout_sec=0)
    result = results[tid]

    assert result.finished is True
    assert result.last_assistant_message == "answer"
    assert db.current_open(tid) is None


def test_thread_state_releases_expired_open_stream(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(db, tid, "assistant", "answer")
    db.try_open_stream(tid, "stale-invoke", "2000-01-01 00:00:00", owner="stale", purpose="llm")

    assert ts.thread_state(db, tid) == "waiting_user"
    assert db.current_open(tid) is None


def test_wait_for_threads_reuses_unchanged_unfinished_poll_result(tmp_path, monkeypatch):
    import eggthreads.api as api
    import eggthreads.tool_state as tool_state

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "work to do")

    reducer_calls = 0
    original_reduce_thread_events = tool_state._reduce_thread_events

    def counting_reduce_thread_events(db_arg, thread_id):
        nonlocal reducer_calls
        assert thread_id == tid
        reducer_calls += 1
        return original_reduce_thread_events(db_arg, thread_id)

    now = [1000.0]
    sleep_calls = 0

    def fake_time():
        return now[0]

    def fake_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        now[0] += float(seconds)

    monkeypatch.setattr(tool_state, "_reduce_thread_events", counting_reduce_thread_events)
    monkeypatch.setattr(api.time, "time", fake_time)
    monkeypatch.setattr(api.time, "sleep", fake_sleep)

    results = ts.wait_for_threads(db, [tid], timeout_sec=0.003, poll_interval=0.001)

    assert sleep_calls >= 2
    assert reducer_calls == 1
    assert results[tid].finished is False
    assert results[tid].state == "running"


def test_wait_for_threads_active_open_stream_avoids_reducer_polling(tmp_path, monkeypatch):
    import eggthreads.api as api
    import eggthreads.tool_state as tool_state

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    assert db.try_open_stream(tid, "active-invoke", "2999-01-01 00:00:00", owner="test", purpose="llm")

    reducer_calls = 0
    original_reduce_thread_events = tool_state._reduce_thread_events

    def counting_reduce_thread_events(db_arg, thread_id):
        nonlocal reducer_calls
        assert thread_id == tid
        reducer_calls += 1
        return original_reduce_thread_events(db_arg, thread_id)

    now = [1000.0]
    sleep_calls = 0

    def fake_time():
        return now[0]

    def fake_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        now[0] += float(seconds)

    monkeypatch.setattr(tool_state, "_reduce_thread_events", counting_reduce_thread_events)
    monkeypatch.setattr(api.time, "time", fake_time)
    monkeypatch.setattr(api.time, "sleep", fake_sleep)

    results = ts.wait_for_threads(db, [tid], timeout_sec=0.003, poll_interval=0.001)

    assert sleep_calls >= 2
    assert reducer_calls == 0
    assert results[tid].finished is False
    assert results[tid].state == "running"


def test_wait_for_threads_uses_monotonic_deadline_and_bounded_sleep(tmp_path, monkeypatch):
    import eggthreads.api as api

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="active")
    ts.append_message(db, tid, "user", "still working")

    monotonic_now = [1000.0]
    wall_now = [2000.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return monotonic_now[0]

    def fake_time() -> float:
        # Wall time moving backwards must not extend the wait deadline.
        wall_now[0] -= 100.0
        return wall_now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        monotonic_now[0] += float(seconds)

    monkeypatch.setattr(api.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(api.time, "time", fake_time)
    monkeypatch.setattr(api.time, "sleep", fake_sleep)

    results = ts.wait_for_threads(db, [tid], timeout_sec=60, poll_interval=100)

    assert sleeps == [60.0]
    assert monotonic_now[0] == pytest.approx(1060.0)
    assert results[tid].finished is False
    assert results[tid].state == "running"


def test_wait_for_threads_checks_cancellation_before_polling(tmp_path, monkeypatch):
    import eggthreads.api as api

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="active")
    polled = False

    def fail_poll(*args, **kwargs):
        nonlocal polled
        polled = True
        raise AssertionError("cancelled wait must not poll")

    monkeypatch.setattr(api, "_thread_wait_poll_once", fail_poll)

    results = ts.wait_for_threads(
        db,
        [tid],
        timeout_sec=None,
        cancel_check=lambda: True,
    )

    assert polled is False
    assert results[tid].finished is False
    assert results[tid].state == "cancelled"


def test_wait_for_threads_reports_typed_timeout_without_changing_child_state(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="active")
    ts.append_message(db, tid, "user", "still working")

    result = ts.wait_for_threads(db, [tid], timeout_sec=0)[tid]

    assert result.finished is False
    assert result.state == "running"
    assert result.timed_out is True
    assert result.cancelled is False


def test_wait_tool_returns_typed_timeout_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    tid = ts.create_root_thread(db, name="active")
    ts.append_message(db, tid, "user", "still working")

    result = ts.create_default_tools().execute(
        "wait",
        {"thread_ids": [tid], "timeout": 0.05},
        preserve_tool_result=True,
    )

    assert isinstance(result, ts.ToolExecutionResult)
    assert result.reason == "timeout"
    assert "TIMEOUT" in result.output
    assert "INTERRUPTED" not in result.output


def test_wait_for_threads_contains_cancel_check_exceptions(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="active")
    ts.append_message(db, tid, "user", "still working")
    calls = 0

    def broken_cancel_check() -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("status source unavailable")

    result = ts.wait_for_threads(
        db,
        [tid],
        timeout_sec=0,
        cancel_check=broken_cancel_check,
    )[tid]

    assert calls >= 1
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.state == "running"


@pytest.mark.parametrize("poll_interval", [-1, float("nan"), float("inf"), "bad", None])
def test_wait_for_threads_normalizes_invalid_poll_interval(
    tmp_path,
    monkeypatch,
    poll_interval,
):
    import eggthreads.api as api

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="active")
    ts.append_message(db, tid, "user", "still working")
    monotonic_now = [1000.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monotonic_now[0] += seconds

    monkeypatch.setattr(api.time, "monotonic", lambda: monotonic_now[0])
    monkeypatch.setattr(api.time, "sleep", fake_sleep)

    result = ts.wait_for_threads(
        db,
        [tid],
        timeout_sec=0.4,
        poll_interval=poll_interval,
    )[tid]

    assert len(sleeps) == 2
    assert sleeps == pytest.approx([0.2, 0.2])
    assert result.timed_out is True


def test_wait_for_threads_caps_poll_interval_for_responsive_cancellation(tmp_path, monkeypatch):
    import eggthreads.api as api

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="active")
    ts.append_message(db, tid, "user", "still working")
    sleeps: list[float] = []
    cancelled = False

    def fake_sleep(seconds: float) -> None:
        nonlocal cancelled
        sleeps.append(seconds)
        cancelled = True

    monkeypatch.setattr(api.time, "sleep", fake_sleep)

    result = ts.wait_for_threads(
        db,
        [tid],
        timeout_sec=None,
        poll_interval=3600,
        cancel_check=lambda: cancelled,
    )[tid]

    assert sleeps == [0.2]
    assert result.cancelled is True
