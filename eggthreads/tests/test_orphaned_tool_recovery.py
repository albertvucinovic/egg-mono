from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import eggthreads as ts
from eggthreads.runner import ThreadRunner


def _make_db(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _declare_executing_tool(
    db,
    thread_id: str,
    *,
    tool_call_id: str = "tc-orphan",
    owner_invoke_id: str = "invoke-old",
    with_lease: str | None = None,
):
    db.append_event(
        f"assistant-tool-{thread_id}",
        thread_id,
        "msg.create",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "side_effect_tool",
                        "arguments": json.dumps({"value": 1}),
                    },
                }
            ],
        },
        msg_id=f"assistant-tool-message-{thread_id}",
    )
    db.append_event(
        f"approve-tool-{thread_id}",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        f"start-tool-{thread_id}",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id, "timeout": 60},
        invoke_id=owner_invoke_id,
    )
    if with_lease is not None:
        assert db.try_open_stream(
            thread_id,
            owner_invoke_id,
            with_lease,
            owner="old-runner",
            purpose="tool",
        )


def _payloads(db, thread_id: str, event_type: str):
    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq",
        (thread_id, event_type),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_missing_lease_tc3_is_runner_actionable(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _declare_executing_tool(db, tid)

    ra = ts.discover_runner_actionable(db, tid)

    assert ra is not None
    assert ra.kind == "RA2_tools_assistant"
    assert ra.recovery_mode == "orphaned_tc3"
    assert [tc.tool_call_id for tc in ra.tool_calls or []] == ["tc-orphan"]
    assert ts.thread_state(db, tid) == "running"


def test_expired_lease_tc3_is_runner_actionable_but_live_lease_is_not(tmp_path):
    db = _make_db(tmp_path)
    expired_tid = ts.create_root_thread(db, name="expired")
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    _declare_executing_tool(db, expired_tid, with_lease=expired)

    expired_ra = ts.discover_runner_actionable(db, expired_tid)
    assert expired_ra is not None
    assert expired_ra.recovery_mode == "orphaned_tc3"

    live_tid = ts.create_root_thread(db, name="live")
    live = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    _declare_executing_tool(
        db,
        live_tid,
        owner_invoke_id="invoke-live",
        with_lease=live,
    )

    assert ts.discover_runner_actionable(db, live_tid) is None


def test_paused_thread_does_not_run_orphan_recovery_until_resumed(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="paused")
    _declare_executing_tool(db, tid)
    db.conn.execute("UPDATE threads SET status='paused' WHERE thread_id=?", (tid,))

    assert ts.thread_state(db, tid) == "paused"
    assert asyncio.run(ThreadRunner(db, tid, llm=object()).run_once()) is False
    assert ts.build_tool_call_states(db, tid)["tc-orphan"].state == "TC3"


def test_expired_lease_only_recovers_tool_owned_by_that_invocation(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _declare_executing_tool(db, tid, tool_call_id="tc-owned", owner_invoke_id="invoke-owned")
    db.append_event(
        "assistant-second-tool",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "tc-unrelated",
                "type": "function",
                "function": {"name": "side_effect_tool", "arguments": "{}"},
            }],
        },
        msg_id="assistant-second-tool-message",
    )
    db.append_event(
        "approve-second-tool",
        tid,
        "tool_call.approval",
        {"tool_call_id": "tc-unrelated", "decision": "granted"},
    )
    db.append_event(
        "start-second-tool",
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tc-unrelated"},
        invoke_id="invoke-unrelated",
    )
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert db.try_open_stream(
        tid,
        "invoke-owned",
        expired,
        owner="old-runner",
        purpose="tool",
    )

    ra = ts.discover_runner_actionable(db, tid)
    assert ra is not None
    assert [tc.tool_call_id for tc in ra.tool_calls or []] == ["tc-owned"]


def test_runner_recovers_orphan_without_rerunning_and_permits_next_turn(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _declare_executing_tool(db, tid)

    executions = 0

    def side_effect(_args):
        nonlocal executions
        executions += 1
        return "must not run"

    tools = ts.ToolRegistry()
    tools.register(
        "side_effect_tool",
        "test side effect tool",
        {"type": "object", "properties": {}},
        side_effect,
    )
    runner = ThreadRunner(db, tid, llm=object(), tools=tools)

    assert asyncio.run(runner.run_once()) is True

    assert executions == 0
    assert db.current_open(tid) is None
    state = ts.build_tool_call_states(db, tid)["tc-orphan"]
    assert state.state == "TC6"
    assert state.finished_reason == "interrupted"
    assert "not run again" in (state.finished_output or "")

    interrupts = _payloads(db, tid, "control.interrupt")
    assert any(
        item.get("reason") == "orphaned_tool_execution_recovery"
        and item.get("old_invoke_id") == "invoke-old"
        and item.get("new_invoke_id")
        for item in interrupts
    )
    assert len(_payloads(db, tid, "tool_call.finished")) == 1
    assert len(_payloads(db, tid, "tool_call.output_approval")) == 1

    tool_results = [
        payload
        for payload in _payloads(db, tid, "msg.create")
        if payload.get("role") == "tool" and payload.get("tool_call_id") == "tc-orphan"
    ]
    assert len(tool_results) == 1
    assert "INTERRUPTED" in tool_results[0]["content"]

    next_ra = ts.discover_runner_actionable(db, tid)
    assert next_ra is not None
    assert next_ra.kind == "RA1_llm"
    assert next_ra.msg_id is not None


def test_runner_recovers_expired_lease_takeover_to_tc6_without_rerun(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    _declare_executing_tool(db, tid, with_lease=expired)

    executions = 0

    def side_effect(_args):
        nonlocal executions
        executions += 1
        return "must not run"

    tools = ts.ToolRegistry()
    tools.register(
        "side_effect_tool",
        "test side effect tool",
        {"type": "object", "properties": {}},
        side_effect,
    )

    assert asyncio.run(ThreadRunner(db, tid, llm=object(), tools=tools).run_once()) is True

    assert executions == 0
    assert db.current_open(tid) is None
    state = ts.build_tool_call_states(db, tid)["tc-orphan"]
    assert state.state == "TC6"
    assert state.finished_reason == "interrupted"

    interrupts = _payloads(db, tid, "control.interrupt")
    assert [item["reason"] for item in interrupts] == [
        "expired_lease_takeover",
        "orphaned_tool_execution_recovery",
    ]
    assert len(_payloads(db, tid, "tool_call.finished")) == 1
    assert len(_payloads(db, tid, "tool_call.output_approval")) == 1


def test_recovery_contention_allows_only_one_fresh_lease_owner(tmp_path):
    db1 = _make_db(tmp_path)
    tid = ts.create_root_thread(db1, name="root")
    _declare_executing_tool(db1, tid)
    db2 = ts.ThreadsDB(db1.path)

    ra1 = ts.discover_runner_actionable(db1, tid)
    ra2 = ts.discover_runner_actionable(db2, tid)
    assert ra1 is not None and ra2 is not None
    assert ra1.recovery_mode == ra2.recovery_mode == "orphaned_tc3"

    lease_until = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert db1.try_open_stream(tid, "recovery-winner", lease_until, purpose="tool") is True
    assert db2.try_open_stream(tid, "recovery-loser", lease_until, purpose="tool") is False
    assert ts.discover_runner_actionable(db2, tid) is None


def _declare_wait(db, parent: str, child: str, *, deadline, timeout=60.0):
    tcid = "tc-wait-orphan"
    old_invoke = "invoke-wait-old"
    db.append_event(
        "assistant-wait-parent",
        parent,
        "msg.create",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tcid,
                "type": "function",
                "function": {
                    "name": "wait",
                    "arguments": json.dumps({"thread_ids": [child], "timeout": timeout}),
                },
            }],
        },
        msg_id="assistant-wait-message",
    )
    db.append_event(
        "approve-wait-parent",
        parent,
        "tool_call.approval",
        {"tool_call_id": tcid, "decision": "granted"},
    )
    db.append_event(
        "start-wait-parent",
        parent,
        "tool_call.execution_started",
        {
            "tool_call_id": tcid,
            "name": "wait",
            "timeout": timeout,
            "timeout_deadline": deadline,
            "resumes_after_lease_loss": True,
        },
        invoke_id=old_invoke,
    )
    return tcid


def test_expired_resumable_wait_uses_original_deadline_without_invoking_tool(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline)

    calls = 0
    tools = ts.create_default_tools()
    original = tools._tools["wait"]["impl"]

    def should_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    tools._tools["wait"]["impl"] = should_not_run
    assert asyncio.run(ThreadRunner(db, parent, llm=object(), tools=tools).run_once()) is True

    state = ts.build_tool_call_states(db, parent)[tcid]
    assert calls == 0
    assert state.state == "TC6"
    assert state.finished_reason == "timeout"
    assert "TIMEOUT" in (state.finished_output or "")


def test_only_explicit_wait_tools_advertise_lease_loss_resumption():
    tools = ts.create_default_tools()

    assert tools.capabilities("wait").resumes_after_lease_loss is True
    assert (
        tools.capabilities(
            "get_user_message_while_preserving_llm_turn"
        ).resumes_after_lease_loss
        is True
    )
    assert tools.capabilities("bash").resumes_after_lease_loss is False


def test_current_registry_capability_cannot_resume_legacy_unmarked_execution(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline)
    db.conn.execute(
        "UPDATE events SET payload_json=json_remove(payload_json, '$.resumes_after_lease_loss') "
        "WHERE thread_id=? AND type='tool_call.execution_started'",
        (parent,),
    )
    calls = 0
    tools = ts.create_default_tools()

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "must not run"

    tools._tools["wait"]["impl"] = should_not_run
    assert asyncio.run(ThreadRunner(db, parent, llm=object(), tools=tools).run_once()) is True
    state = ts.build_tool_call_states(db, parent)[tcid]
    assert calls == 0
    assert state.state == "TC6"
    assert state.finished_reason == "interrupted"


def test_normal_wait_execution_persists_resumption_policy(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "assistant", "done")
    ts.append_message(
        db,
        parent,
        "assistant",
        "",
        extra={
            "tool_calls": [{
                "id": "tc-normal-wait",
                "type": "function",
                "function": {
                    "name": "wait",
                    "arguments": json.dumps({"thread_ids": [child], "timeout": 1}),
                },
            }],
        },
    )
    db.append_event(
        "approve-normal-wait",
        parent,
        "tool_call.approval",
        {"tool_call_id": "tc-normal-wait", "decision": "granted"},
    )

    assert asyncio.run(ThreadRunner(db, parent, llm=object()).run_once()) is True
    starts = _payloads(db, parent, "tool_call.execution_started")
    assert starts[-1]["resumes_after_lease_loss"] is True


def test_resumable_wait_repolls_with_only_original_remaining_budget(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "still working")
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline, timeout=60.0)

    seen_timeouts = []
    tools = ts.create_default_tools()
    original = tools._tools["wait"]["impl"]

    def capture_timeout(args, ctx):
        seen_timeouts.append(float(args["timeout"]))
        return original(args, ctx)

    tools._tools["wait"]["impl"] = capture_timeout
    assert asyncio.run(ThreadRunner(db, parent, llm=object(), tools=tools).run_once()) is True

    state = ts.build_tool_call_states(db, parent)[tcid]
    assert state.state == "TC6"
    assert seen_timeouts and 0 < seen_timeouts[0] <= 0.3
    assert state.finished_reason == "timeout"
    starts = _payloads(db, parent, "tool_call.execution_started")
    assert starts[-1]["resume_after_lease_loss"] is True
    assert starts[-1]["timeout_deadline"] == deadline


def test_get_user_recovery_reuses_existing_note_and_consumes_reply(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = "tc-get-user-orphan"
    old_invoke = "invoke-get-user-old"
    note = "Please provide the missing value."
    db.append_event(
        "assistant-get-user",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tcid,
                "type": "function",
                "function": {
                    "name": "get_user_message_while_preserving_llm_turn",
                    "arguments": json.dumps({"assistant_note": note}),
                },
            }],
        },
        msg_id="assistant-get-user-message",
    )
    db.append_event(
        "start-get-user",
        tid,
        "tool_call.execution_started",
        {"tool_call_id": tcid, "resumes_after_lease_loss": True},
        invoke_id=old_invoke,
    )
    db.append_event(
        "note-get-user",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "content": note,
            "answer_user_preserve_turn": True,
            "source_tool_name": "get_user_message_while_preserving_llm_turn",
            "tool_call_id": tcid,
            "awaiting_user_message_tool_call_id": tcid,
        },
        msg_id="note-get-user-message",
        invoke_id=old_invoke,
    )
    def append_reply():
        __import__("time").sleep(0.05)
        reply_db = ts.ThreadsDB(db.path)
        try:
            ts.append_normal_user_message(reply_db, tid, "Recovered answer")
        finally:
            reply_db.conn.close()

    thread = threading.Thread(target=append_reply, daemon=True)
    thread.start()

    assert asyncio.run(ThreadRunner(db, tid, llm=object()).run_once()) is True
    thread.join(timeout=1)

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.state == "TC6"
    assert state.finished_reason == "success"
    assert state.finished_output == "Recovered answer"
    notes = [
        payload
        for payload in _payloads(db, tid, "msg.create")
        if payload.get("awaiting_user_message_tool_call_id") == tcid
    ]
    assert len(notes) == 1


def test_recovery_actionable_after_takeover_interrupt_crash_window(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _declare_executing_tool(db, tid)
    db.append_event(
        "expired-takeover-before-recovery-crash",
        tid,
        "control.interrupt",
        {
            "reason": "expired_lease_takeover",
            "old_invoke_id": "invoke-old",
            "new_invoke_id": "crashed-recovery",
            "purpose": "tool",
        },
    )

    ra = ts.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.recovery_mode == "orphaned_tc3"
    assert (ra.tool_calls or [])[0].state == "TC4"

    assert asyncio.run(ThreadRunner(db, tid, llm=object()).run_once()) is True
    assert ts.build_tool_call_states(db, tid)["tc-orphan"].state == "TC6"


def test_real_interrupted_finish_after_takeover_is_not_recovered_again(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _declare_executing_tool(db, tid)
    db.append_event(
        "expired-takeover",
        tid,
        "control.interrupt",
        {
            "reason": "expired_lease_takeover",
            "old_invoke_id": "invoke-old",
            "new_invoke_id": "takeover",
            "purpose": "tool",
        },
    )
    db.append_event(
        "durable-real-finish",
        tid,
        "tool_call.finished",
        {
            "tool_call_id": "tc-orphan",
            "reason": "interrupted",
            "output": "real interrupted output",
        },
        invoke_id="takeover",
    )

    state = ts.build_tool_call_states(db, tid)["tc-orphan"]
    assert state.state == "TC4"
    assert state.finished_event_seq is not None
    assert ts.discover_runner_actionable(db, tid) is None


def test_no_api_mode_terminalizes_resumable_wait_without_invoking_it(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline)
    calls = 0
    tools = ts.create_default_tools()

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "must not run"

    tools._tools["wait"]["impl"] = should_not_run
    runner = ThreadRunner(
        db,
        parent,
        llm=object(),
        tools=tools,
        config=ts.RunnerConfig(no_api_calls=True),
    )

    assert asyncio.run(runner.run_once()) is True
    state = ts.build_tool_call_states(db, parent)[tcid]
    assert calls == 0
    assert state.state == "TC6"
    assert state.finished_reason == "interrupted"


def test_recovery_actionable_after_takeover_runner_lease_expires_before_owner_transfer(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _declare_executing_tool(db, tid, with_lease=expired)

    # A first recovery runner takes over the old owner, fencing it durably, but
    # crashes before it can append a resumed execution start or synthetic finish.
    live = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert db.try_open_stream(
        tid,
        "crashed-recovery",
        live,
        purpose="tool",
    ) is True
    db.conn.execute(
        "UPDATE open_streams SET lease_until=? WHERE thread_id=?",
        (expired, tid),
    )

    state = ts.build_tool_call_states(db, tid)["tc-orphan"]
    assert state.state == "TC4"
    assert state.owner_invoke_id == "invoke-old"
    assert db.current_open(tid)["invoke_id"] == "crashed-recovery"

    ra = ts.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.recovery_mode == "orphaned_tc3"

    assert asyncio.run(ThreadRunner(db, tid, llm=object()).run_once()) is True
    assert ts.build_tool_call_states(db, tid)["tc-orphan"].state == "TC6"


def _declare_get_user_wait(db, thread_id: str, *, invoke_id: str, tool_call_id: str):
    note = "Please provide the missing value."
    db.append_event(
        f"assistant-{tool_call_id}",
        thread_id,
        "msg.create",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "get_user_message_while_preserving_llm_turn",
                    "arguments": json.dumps({"assistant_note": note}),
                },
            }],
        },
        msg_id=f"assistant-message-{tool_call_id}",
    )
    db.append_event(
        f"start-{tool_call_id}",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=invoke_id,
    )
    db.append_event(
        f"note-{tool_call_id}",
        thread_id,
        "msg.create",
        {
            "role": "assistant",
            "content": note,
            "answer_user_preserve_turn": True,
            "source_tool_name": "get_user_message_while_preserving_llm_turn",
            "tool_call_id": tool_call_id,
            "awaiting_user_message_tool_call_id": tool_call_id,
        },
        msg_id=f"note-message-{tool_call_id}",
        invoke_id=invoke_id,
    )


def test_get_user_recovery_publishes_reply_claimed_before_owner_crash(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    invoke_id = "invoke-get-user-claimed"
    tool_call_id = "tc-get-user-claimed"
    live = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert db.try_open_stream(tid, invoke_id, live, purpose="tool")
    _declare_get_user_wait(
        db,
        tid,
        invoke_id=invoke_id,
        tool_call_id=tool_call_id,
    )
    reply_msg_id = ts.append_normal_user_message(db, tid, "Already claimed answer")
    assert ts.build_tool_call_states(db, tid)[tool_call_id].claimed_user_msg_id == reply_msg_id
    db.conn.execute("DELETE FROM open_streams WHERE thread_id=?", (tid,))

    assert asyncio.run(ThreadRunner(db, tid, llm=object()).run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tool_call_id]
    assert state.state == "TC6"
    assert state.finished_reason == "success"
    assert state.finished_output == "Already claimed answer"
    messages = [
        payload
        for payload in _payloads(db, tid, "msg.create")
        if payload.get("role") == "tool" and payload.get("tool_call_id") == tool_call_id
    ]
    assert [message["content"] for message in messages] == ["Already claimed answer"]


def test_deadline_without_timeout_terminalizes_when_expired(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline, timeout=60.0)
    db.conn.execute(
        "UPDATE events SET payload_json=json_remove(payload_json, '$.timeout') "
        "WHERE thread_id=? AND type='tool_call.execution_started'",
        (parent,),
    )

    calls = 0
    tools = ts.create_default_tools()

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "must not run"

    tools._tools["wait"]["impl"] = should_not_run
    assert asyncio.run(ThreadRunner(db, parent, llm=object(), tools=tools).run_once()) is True

    state = ts.build_tool_call_states(db, parent)[tcid]
    assert calls == 0
    assert state.state == "TC6"
    assert state.finished_reason == "timeout"


def test_resumable_wait_rechecks_absolute_deadline_before_invocation(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=0.1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline)
    calls = 0
    tools = ts.create_default_tools()

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "must not run"

    tools._tools["wait"]["impl"] = should_not_run
    original_build = __import__("eggthreads.runner", fromlist=["build_tool_call_states"]).build_tool_call_states
    builds = 0

    def delayed_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        if builds == 3:
            time.sleep(0.15)
        return original_build(*args, **kwargs)

    monkeypatch.setattr("eggthreads.runner.build_tool_call_states", delayed_build)

    assert asyncio.run(ThreadRunner(db, parent, llm=object(), tools=tools).run_once()) is True
    state = ts.build_tool_call_states(db, parent)[tcid]
    assert calls == 0
    assert state.state == "TC6"
    assert state.finished_reason == "timeout"


def test_resumable_marker_without_owner_terminalizes_instead_of_wedging(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    tcid = _declare_wait(db, parent, child, deadline=deadline)
    db.conn.execute(
        "UPDATE events SET invoke_id=NULL WHERE thread_id=? "
        "AND type='tool_call.execution_started'",
        (parent,),
    )
    calls = 0
    tools = ts.create_default_tools()

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "must not run"

    tools._tools["wait"]["impl"] = should_not_run
    assert asyncio.run(ThreadRunner(db, parent, llm=object(), tools=tools).run_once()) is True
    state = ts.build_tool_call_states(db, parent)[tcid]
    assert calls == 0
    assert state.state == "TC6"
    assert state.finished_reason == "interrupted"


def test_takeover_lease_suppresses_other_recovery_until_it_expires(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _declare_executing_tool(db, tid, with_lease=expired)
    live = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert db.try_open_stream(tid, "recovery-owner", live, purpose="tool")

    assert ts.discover_runner_actionable(db, tid) is None
