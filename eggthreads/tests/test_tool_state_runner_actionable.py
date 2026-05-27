"""Tests for eggthreads.tool_state and basic RunnerActionable logic.

These tests build small synthetic event logs through the public
``eggthreads`` APIs and assert that:

* ``build_tool_call_states`` reconstructs per-tool_call state
  correctly across approval, execution, finish, and output-approval
  events, including the "all-in-turn" decision.
* ``discover_runner_actionable`` chooses the correct RA kind
  (RA3_tools_user, RA2_tools_assistant, RA1_llm) for representative
  scenarios.
* ``thread_state`` reports coarse thread status (running / waiting
  for approval / idle) in line with the tool-call state machine.
"""

from __future__ import annotations

import json
import time
from typing import Dict, Any

import eggthreads


def _make_db(tmp_path):
    db = eggthreads.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _append_event(
    db,
    tid: str,
    type_: str,
    payload: Dict[str, Any],
    *,
    msg_id: str | None = None,
    invoke_id: str | None = None,
) -> None:
    """Append a JSON event with a fresh ULID-like event_id.

    Using the public ``append_event`` helper would also work but is not
    currently re-exported; we call ``ThreadsDB.append_event`` directly
    with a simple counter-based id for tests.
    """

    # Very small, deterministic id for tests – uniqueness is enough.
    eid = f"{type_}-{db.max_event_seq(tid)+1}"
    db.append_event(event_id=eid, thread_id=tid, type_=type_, payload=payload, msg_id=msg_id, invoke_id=invoke_id)


def test_output_approval_without_finished_recovers_as_interrupted(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-output-decision-without-finish"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event(
        event_id="msg-a",
        thread_id=tid,
        type_="msg.create",
        msg_id="m-assistant",
        payload={
            "role": "assistant",
            "tool_calls": [
                {"id": "tcA", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tcA", "decision": "granted"})
    _append_event(
        db,
        tid,
        "tool_call.output_approval",
        {"tool_call_id": "tcA", "decision": "omit", "preview": "Output omitted."},
    )

    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.finished_reason == "interrupted"
    assert tc.state == "TC5"

    ra = eggthreads.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.kind == "RA2_tools_assistant"


def test_tool_interrupt_without_finished_recovers_as_interrupted(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-tool-interrupt-without-finish"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event(
        event_id="msg-a",
        thread_id=tid,
        type_="msg.create",
        msg_id="m-assistant",
        payload={
            "role": "assistant",
            "tool_calls": [
                {"id": "tcA", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tcA", "decision": "granted"})
    invoke_id = "inv-tool"
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tcA"},
        invoke_id=invoke_id,
    )
    _append_event(
        db,
        tid,
        "control.interrupt",
        {"reason": "user", "old_invoke_id": invoke_id, "new_invoke_id": "inv-new", "purpose": "tool"},
    )

    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.finished_reason == "interrupted"
    assert tc.output_decision == "whole"
    assert tc.state == "TC5"

    ra = eggthreads.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.kind == "RA2_tools_assistant"


def test_continue_from_tool_parent_retries_parent_tool_call(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-continue-from-tool-parent"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "do it"}, msg_id="m-user")
    db.append_event(
        event_id="msg-a",
        thread_id=tid,
        type_="msg.create",
        msg_id="m-assistant",
        payload={
            "role": "assistant",
            "tool_calls": [
                {"id": "tcA", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tcA", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tcA"}, invoke_id="inv-tool")
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tcA", "reason": "interrupted", "output": "partial"})
    _append_event(
        db,
        tid,
        "tool_call.output_approval",
        {"tool_call_id": "tcA", "decision": "omit", "preview": "Output omitted."},
    )
    db.append_event(
        event_id="msg-tool",
        thread_id=tid,
        type_="msg.create",
        msg_id="m-tool",
        payload={"role": "tool", "tool_call_id": "tcA", "content": "interrupted"},
    )
    db.append_event(
        event_id="edit-tool",
        thread_id=tid,
        type_="msg.edit",
        msg_id="m-tool",
        payload={"skipped_on_continue": True},
    )
    db.append_event(
        event_id="continue",
        thread_id=tid,
        type_="control.interrupt",
        payload={"purpose": "continue", "continue_from_msg_id": "m-assistant"},
    )

    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.state == "TC1"
    assert tc.execution_started is False
    assert tc.finished_reason is None
    assert tc.output_decision is None


def test_continue_from_before_tool_parent_skips_parent_tool_events(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-continue-before-tool-parent"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "do it"}, msg_id="m-user")
    db.append_event(
        event_id="msg-a",
        thread_id=tid,
        type_="msg.create",
        msg_id="m-assistant",
        payload={
            "role": "assistant",
            "tool_calls": [
                {"id": "tcA", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tcA", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tcA"}, invoke_id="inv-tool")
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tcA", "reason": "interrupted", "output": "partial"})
    _append_event(
        db,
        tid,
        "tool_call.output_approval",
        {"tool_call_id": "tcA", "decision": "omit", "preview": "Output omitted."},
    )
    db.append_event(
        event_id="edit-assistant",
        thread_id=tid,
        type_="msg.edit",
        msg_id="m-assistant",
        payload={"skipped_on_continue": True},
    )
    db.append_event(
        event_id="continue",
        thread_id=tid,
        type_="control.interrupt",
        payload={"purpose": "continue", "continue_from_msg_id": "m-user"},
    )

    assert "tcA" not in eggthreads.build_tool_call_states(db, tid)


def _tool_state_signature(tc):
    return {
        "thread_id": tc.thread_id,
        "tool_call_id": tc.tool_call_id,
        "parent_msg_id": tc.parent_msg_id,
        "parent_event_seq": tc.parent_event_seq,
        "parent_role": tc.parent_role,
        "index": tc.index,
        "name": tc.name,
        "arguments": tc.arguments,
        "approval_decision": tc.approval_decision,
        "execution_started": tc.execution_started,
        "finished_reason": tc.finished_reason,
        "finished_output": tc.finished_output,
        "output_decision": tc.output_decision,
        "summary": tc.summary,
        "published": tc.published,
        "last_output_approval_payload": tc.last_output_approval_payload,
        "state": tc.state,
    }


def _ra_signature(ra):
    if ra is None:
        return None
    return {
        "kind": ra.kind,
        "thread_id": ra.thread_id,
        "triggering_event_seq": ra.triggering_event_seq,
        "msg_id": ra.msg_id,
        "tool_calls": [
            _tool_state_signature(tc) for tc in (ra.tool_calls or [])
        ] if ra.tool_calls is not None else None,
    }


def _reduction_signature(reduced):
    return {
        "thread_id": reduced.thread_id,
        "max_event_seq": reduced.max_event_seq,
        "skipped_msg_ids": set(reduced.skipped_msg_ids),
        "last_llm_boundary_seq": reduced.last_llm_boundary_seq,
        "messages_after_boundary": [int(ev["event_seq"]) for ev in reduced.messages_after_boundary],
        "tool_call_states": {
            key: _tool_state_signature(value)
            for key, value in reduced.tool_call_states.items()
        },
        "next_runner_actionable": _ra_signature(reduced.next_runner_actionable),
        "coarse_thread_state_without_lease": reduced.coarse_thread_state_without_lease,
    }


def _full_rebuild_signature(db, tid: str, reducer=None):
    if reducer is None:
        from eggthreads.tool_state import _reduce_loaded_thread_events as reducer

    events = [dict(row) for row in db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()]
    full = reducer(tid, db.max_event_seq(tid), events)
    return _reduction_signature(full)


def _assert_incremental_matches_full_rebuild(db, tid: str) -> None:
    from eggthreads.tool_state import _REDUCER_CACHE, _reduce_loaded_thread_events, _reduce_thread_events

    incremental = _reduce_thread_events(db, tid)
    events = [dict(row) for row in db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()]
    full = _reduce_loaded_thread_events(tid, db.max_event_seq(tid), events)

    assert _reduction_signature(incremental) == _reduction_signature(full)
    assert (str(db.path), tid, db.max_event_seq(tid)) in _REDUCER_CACHE


def _append_published_user_tool_call(db, tid: str) -> None:
    db.append_event(
        "msg-user-tool",
        tid,
        "msg.create",
        {
            "role": "user",
            "content": "cmd",
            "tool_calls": [
                {"id": "tc_hist", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user-tool",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_hist", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_hist"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_hist", "reason": "success", "output": "ok"})
    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc_hist", "decision": "whole", "preview": "ok"})
    _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": "tc_hist", "content": "ok"}, msg_id="m-tool")


def _assert_reducer_matches_public_state(db, tid: str) -> None:
    from eggthreads.tool_state import (
        _iter_messages_after,
        _last_stream_close_seq,
        _reduce_thread_events,
        build_tool_call_states,
        discover_runner_actionable,
    )

    reduced = _reduce_thread_events(db, tid)

    expected_states = {
        key: _tool_state_signature(value)
        for key, value in build_tool_call_states(db, tid).items()
    }
    actual_states = {
        key: _tool_state_signature(value)
        for key, value in reduced.tool_call_states.items()
    }
    assert actual_states == expected_states

    expected_boundary = _last_stream_close_seq(db, tid)
    assert reduced.last_llm_boundary_seq == expected_boundary

    expected_message_seqs = [
        int(ev["event_seq"]) for ev in _iter_messages_after(db, tid, expected_boundary)
    ]
    actual_message_seqs = [int(ev["event_seq"]) for ev in reduced.messages_after_boundary]
    assert actual_message_seqs == expected_message_seqs

    assert _ra_signature(reduced.next_runner_actionable) == _ra_signature(
        discover_runner_actionable(db, tid)
    )


def test_thread_event_reducer_matches_simple_user_ra1(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-ra1"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")

    _assert_reducer_matches_public_state(db, tid)


def test_thread_event_reducer_matches_assistant_waiting_for_tool_approval(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-wait-tool"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event(
        "msg-asst",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "tc_wait", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst",
    )

    _assert_reducer_matches_public_state(db, tid)


def test_thread_event_reducer_matches_approved_assistant_tool_ra2(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-ra2"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event(
        "msg-asst",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "tc_asst", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_asst", "decision": "granted"})

    _assert_reducer_matches_public_state(db, tid)


def test_thread_event_reducer_matches_user_tool_ra3(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-ra3"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event(
        "msg-user",
        tid,
        "msg.create",
        {
            "role": "user",
            "content": "cmd",
            "tool_calls": [
                {"id": "tc_user", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_user", "decision": "granted"})

    _assert_reducer_matches_public_state(db, tid)


def test_thread_event_reducer_matches_continue_skipped_messages(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-continue"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    db.append_event(
        "msg-asst",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "content": "partial",
            "tool_calls": [
                {"id": "tc_skip", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_skip", "decision": "granted"})
    db.append_event("skip-asst", tid, "msg.edit", {"skipped_on_continue": True}, msg_id="m-asst")
    db.append_event(
        "continue",
        tid,
        "control.interrupt",
        {"reason": "continue", "purpose": "continue", "continue_from_msg_id": "m-user"},
    )

    _assert_reducer_matches_public_state(db, tid)


def test_incremental_reducer_preserves_continue_boundary_after_llm_stream_open(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-continue-stream-open"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-tool", tid, "msg.create", {"role": "tool", "content": "tool result"}, msg_id="m-tool")
    tool_seq = db.max_event_seq(tid)
    db.append_event(
        "old-open",
        tid,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="m-old-open",
        invoke_id="old-invoke",
    )
    db.append_event(
        "old-delta",
        tid,
        "stream.delta",
        {"reason": "LLM/runner error: boom"},
        invoke_id="old-invoke",
        chunk_seq=0,
    )
    db.append_event("msg-error", tid, "msg.create", {"role": "system", "content": "LLM/runner error: boom"}, msg_id="m-error")
    db.append_event("old-close", tid, "stream.close", {}, invoke_id="old-invoke")
    db.append_event("skip-error", tid, "msg.edit", {"skipped_on_continue": True}, msg_id="m-error")
    db.append_event(
        "continue",
        tid,
        "control.interrupt",
        {"reason": "continue", "purpose": "continue", "continue_from_msg_id": "m-tool"},
    )

    from eggthreads.tool_state import _REDUCER_CACHE, _last_stream_close_seq, _reduce_thread_events

    _REDUCER_CACHE.clear()
    before = _reduce_thread_events(db, tid)
    assert before.last_llm_boundary_seq == tool_seq - 1
    assert before.next_runner_actionable is not None
    assert before.next_runner_actionable.kind == "RA1_llm"
    assert before.next_runner_actionable.msg_id == "m-tool"

    db.append_event(
        "new-open",
        tid,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="m-new-open",
        invoke_id="new-invoke",
    )

    after = _reduce_thread_events(db, tid)
    assert after.last_llm_boundary_seq == tool_seq - 1
    assert _last_stream_close_seq(db, tid) == tool_seq - 1
    assert after.next_runner_actionable is not None
    assert after.next_runner_actionable.kind == "RA1_llm"
    assert after.next_runner_actionable.msg_id == "m-tool"

    _assert_incremental_matches_full_rebuild(db, tid)


def test_thread_event_reducer_matches_llm_interrupt_boundary(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-reducer-llm-interrupt"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    db.append_event("interrupt", tid, "control.interrupt", {"reason": "cancel", "purpose": "llm"})

    _assert_reducer_matches_public_state(db, tid)


def test_build_tool_call_states_user_all_in_turn(tmp_path):
    """User-originated tool_calls with all-in-turn approval reach TC2.1.

    We simulate a single user message declaring two tool calls, then a
    ``tool_call.approval`` with decision="all-in-turn".  Both tool
    calls should end in state TC2.1 (approved, not yet executed).
    """

    db = _make_db(tmp_path)
    tid = "thread-all-in-turn"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    # User message with two tool calls
    payload = {
        "role": "user",
        "content": "run tools",
        "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "tc2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    db.append_event(
        event_id="msg-1",
        thread_id=tid,
        type_="msg.create",
        payload=payload,
        msg_id="m-user",
    )

    # Approval for all tool calls in this user turn
    _append_event(db, tid, "tool_call.approval", {"decision": "all-in-turn"})

    states = eggthreads.build_tool_call_states(db, tid)
    assert set(states.keys()) == {"tc1", "tc2"}
    for tc in states.values():
        assert tc.parent_role == "user"
        assert tc.approval_decision == "granted"
        assert tc.state == "TC2.1"  # approved, not executed


def test_build_tool_call_states_assistant_tool_lifecycle(tmp_path):
    """Assistant-originated tool_call flows through approval -> exec -> finish.

    We model a single assistant message with one tool_call and emit a
    normal sequence of tool_call.* events.  The resulting
    ToolCallState should end up in TC4/TC5/TC6 as events progress.
    """

    db = _make_db(tmp_path)
    tid = "thread-assistant-tool"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    # Assistant message that declares a tool call
    payload = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "tcA", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    db.append_event(
        event_id="msg-a",
        thread_id=tid,
        type_="msg.create",
        payload=payload,
        msg_id="m-assistant",
    )

    # Explicit approval for this tool call
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tcA", "decision": "granted"})
    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.approval_decision == "granted"
    assert tc.state == "TC2.1"

    # Execution started
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tcA"})
    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.execution_started is True
    assert tc.state == "TC3"

    # Running summary updates are UI-facing metadata and should not
    # advance the lifecycle beyond TC3.
    _append_event(
        db,
        tid,
        "tool_call.summary",
        {"tool_call_id": "tcA", "name": "bash", "summary": "bash running; timeout in 29s (limit 30s)"},
    )
    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.summary == "bash running; timeout in 29s (limit 30s)"
    assert tc.state == "TC3"

    # Finished with output
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tcA", "reason": "success", "output": "ok"})
    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.finished_reason == "success"
    assert tc.finished_output == "ok"
    assert tc.state == "TC4"

    # Output approval and final tool message mark TC5/TC6
    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tcA", "decision": "whole", "preview": "ok"})
    _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": "tcA", "content": "ok"})
    states = eggthreads.build_tool_call_states(db, tid)
    tc = states["tcA"]
    assert tc.output_decision == "whole"
    assert tc.last_output_approval_payload is not None
    assert tc.published is True
    assert tc.state == "TC6"


def test_discover_runner_actionable_ra3_before_ra2_and_ra1(tmp_path):
    """RA3 (user tools) takes precedence over RA2/RA1.

    We create a thread where:
      - a user message declares an approved tool_call (TC2.1), and
      - an assistant message later declares its own approved tool_call.

    discover_runner_actionable() should return RA3_tools_user for the
    user-originated call first.
    """

    import eggthreads as ts

    db = _make_db(tmp_path)
    tid = "thread-ra3"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    # User message with one tool call
    user_payload = {
        "role": "user",
        "content": "cmd",
        "tool_calls": [
            {"id": "tc_user", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    db.append_event("msg-user", tid, "msg.create", user_payload, msg_id="m-user")
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_user", "decision": "granted"})

    # Assistant message with its own tool call (should be RA2 later)
    asst_payload = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "tc_asst", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    db.append_event("msg-asst", tid, "msg.create", asst_payload, msg_id="m-asst")
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_asst", "decision": "granted"})

    ra = ts.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.kind == "RA3_tools_user"
    assert ra.msg_id == "m-user"
    assert {tc.tool_call_id for tc in (ra.tool_calls or [])} == {"tc_user"}


def test_discover_runner_actionable_ra2_when_only_assistant_tools(tmp_path):
    """When only assistant tool_calls exist, RA2_tools_assistant is chosen."""

    import eggthreads as ts

    db = _make_db(tmp_path)
    tid = "thread-ra2"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    asst_payload = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "tc_asst", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    db.append_event("msg-asst", tid, "msg.create", asst_payload, msg_id="m-asst")
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_asst", "decision": "granted"})

    ra = ts.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.kind == "RA2_tools_assistant"
    assert ra.msg_id == "m-asst"
    assert {tc.tool_call_id for tc in (ra.tool_calls or [])} == {"tc_asst"}


def test_discover_runner_actionable_ra1_after_llm_boundary(tmp_path):
    """RA1_llm triggers on the first eligible message after last stream boundary."""

    import eggthreads as ts

    db = _make_db(tmp_path)
    tid = "thread-ra1"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    # Simulate an earlier LLM turn: assistant content plus LLM-style stream
    db.append_event("msg-asst-prev", tid, "msg.create", {"role": "assistant", "content": "done"}, msg_id="m-asst-prev")
    # stream.open / stream.delta / stream.close with text marks an LLM invoke.
    # For stream.open and stream.delta, the schema requires invoke_id
    # (and chunk_seq for deltas), so we use a dummy value here.
    inv = "inv-llm"
    db.append_event("s-open", tid, "stream.open", {"model_key": "m"}, msg_id="m-asst-prev", invoke_id=inv)
    db.append_event("s-delta", tid, "stream.delta", {"text": "chunk"}, invoke_id=inv, chunk_seq=0)
    db.append_event("s-close", tid, "stream.close", {}, invoke_id=inv)

    # Now a new user message should be picked as RA1 trigger
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "next"}, msg_id="m-user")

    ra = ts.discover_runner_actionable(db, tid)
    assert ra is not None
    assert ra.kind == "RA1_llm"
    assert ra.msg_id == "m-user"


def test_reasoning_summary_stream_delta_counts_as_llm_boundary(tmp_path):
    """Summary-only LLM streams should not re-trigger the same user turn."""

    import eggthreads as ts

    db = _make_db(tmp_path)
    tid = "thread-summary-boundary"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hi"}, msg_id="m-user")
    inv = "inv-summary"
    db.append_event("s-open", tid, "stream.open", {"model_key": "m"}, msg_id="m-asst", invoke_id=inv)
    db.append_event("s-delta", tid, "stream.delta", {"reasoning_summary": "display"}, invoke_id=inv, chunk_seq=0)
    db.append_event("s-close", tid, "stream.close", {}, invoke_id=inv)

    assert ts.discover_runner_actionable(db, tid) is None


def test_thread_state_waiting_and_running(tmp_path):
    """thread_state reflects TC1/TC4 presence and active streams/RA1.

    We do *not* attempt to simulate a full runner turn here.  Instead
    we assert that:

    * with an unapproved tool_call present, the thread waits for tool
      approval,
    * after execution/finish (TC4) it waits for output approval, and
    * once output approval and a final tool message exist, the thread
      is considered "running" again because an RA1 LLM turn is now
      actionable.
    """

    import eggthreads as ts

    db = _make_db(tmp_path)
    tid = "thread-state"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    # Initially idle, waiting for user
    assert ts.thread_state(db, tid) == "waiting_user"

    # Add a user message with a tool_call but no approval -> TC1
    payload = {
        "role": "user",
        "content": "cmd",
        "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }
    db.append_event("msg-user", tid, "msg.create", payload, msg_id="m-user")
    assert ts.thread_state(db, tid) == "waiting_tool_approval"

    # Approve and finish the tool without output_approval -> TC4
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc1", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc1"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc1", "reason": "success", "output": "ok"})
    assert ts.thread_state(db, tid) == "waiting_output_approval"

    # Provide output approval and final tool message -> RA1 becomes
    # actionable, so the coarse state moves back to "running" until the
    # runner performs the assistant turn.
    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc1", "decision": "whole", "preview": "ok"})
    _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": "tc1", "content": "ok"})
    assert ts.thread_state(db, tid) == "running"


def test_tool_call_lifecycle_events_before_parent_message_are_ignored(tmp_path):
    """Tool state should not attach stale reused tool_call_id events.

    Some providers may reuse-looking ids across turns, and event replay should
    only fold lifecycle events that occur after the message declaring that tool
    call.  Older tool_call.* events with the same id must not make a fresh tool
    call look already finished/published.
    """

    db = _make_db(tmp_path)
    tid = "thread-stale-tool-events"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_reused"})
    _append_event(db, tid, "tool_call.summary", {"tool_call_id": "tc_reused", "summary": "old"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_reused", "reason": "success", "output": "old"})
    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc_reused", "decision": "whole", "preview": "old"})
    _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": "tc_reused", "content": "old"})

    db.append_event(
        event_id="msg-new-tool",
        thread_id=tid,
        type_="msg.create",
        payload={
            "role": "assistant",
            "tool_calls": [
                {"id": "tc_reused", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-new-tool",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_reused", "decision": "granted"})

    tc = eggthreads.build_tool_call_states(db, tid)["tc_reused"]
    assert tc.parent_msg_id == "m-new-tool"
    assert tc.approval_decision == "granted"
    assert tc.execution_started is False
    assert tc.finished_reason is None
    assert tc.output_decision is None
    assert tc.published is False
    assert tc.state == "TC2.1"


def test_discover_runner_actionable_cached_reuses_single_reducer_query(tmp_path):
    from eggthreads.tool_state import discover_runner_actionable_cached

    db = _make_db(tmp_path)
    tid = "thread-reducer-query-count"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")

    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        assert discover_runner_actionable_cached(db, tid).kind == "RA1_llm"
        first_count = len([stmt for stmt in statements if " FROM events" in stmt])
        assert first_count == 2

        statements.clear()
        assert discover_runner_actionable_cached(db, tid).kind == "RA1_llm"
        second_count = len([stmt for stmt in statements if " FROM events" in stmt])
        assert second_count == 1
    finally:
        db.conn.set_trace_callback(None)


def test_reducer_cache_keeps_one_current_projection_per_thread(tmp_path):
    from eggthreads.tool_state import _REDUCER_CACHE, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-reducer-cache-one-current"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user-1", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user-1")
    first = _reduce_thread_events(db, tid)

    db.append_event("msg-user-2", tid, "msg.create", {"role": "user", "content": "next"}, msg_id="m-user-2")
    second = _reduce_thread_events(db, tid)

    cache_keys = [key for key in _REDUCER_CACHE if key[0] == str(db.path) and key[1] == tid]
    assert cache_keys == [(str(db.path), tid, second.max_event_seq)]
    assert first.max_event_seq < second.max_event_seq


def test_reducer_cache_incrementally_applies_plain_messages_and_llm_boundaries(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-plain"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user-1", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user-1")
    first = _reduce_thread_events(db, tid)
    assert first.next_runner_actionable is not None
    assert first.next_runner_actionable.msg_id == "m-user-1"

    full_rebuild_calls = 0

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal full_rebuild_calls
        full_rebuild_calls += 1
        return _reduce_loaded_thread_events(thread_id, max_event_seq, events)

    inv = "inv-llm"
    db.append_event("s-open", tid, "stream.open", {"stream_kind": "llm"}, msg_id="m-asst", invoke_id=inv)
    db.append_event("s-close", tid, "stream.close", {}, invoke_id=inv)
    with monkeypatch.context() as m:
        m.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
        after_close = _reduce_thread_events(db, tid)
    assert full_rebuild_calls == 0
    assert after_close.next_runner_actionable is None
    _assert_incremental_matches_full_rebuild(db, tid)

    db.append_event("msg-user-2", tid, "msg.create", {"role": "user", "content": "next"}, msg_id="m-user-2")
    with monkeypatch.context() as m:
        m.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
        after_second_user = _reduce_thread_events(db, tid)
    assert full_rebuild_calls == 0
    assert after_second_user.next_runner_actionable is not None
    assert after_second_user.next_runner_actionable.kind == "RA1_llm"
    assert after_second_user.next_runner_actionable.msg_id == "m-user-2"
    _assert_incremental_matches_full_rebuild(db, tid)


def test_reducer_cache_incrementally_applies_safe_tail_after_historical_tool_states(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-with-tools"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_published_user_tool_call(db, tid)

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_hist"].state == "TC6"
    assert before.next_runner_actionable is not None
    assert before.next_runner_actionable.kind == "RA1_llm"

    full_rebuild_calls = 0

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal full_rebuild_calls
        full_rebuild_calls += 1
        return _reduce_loaded_thread_events(thread_id, max_event_seq, events)

    inv = "inv-after-tools"
    db.append_event("s-open-after-tools", tid, "stream.open", {"stream_kind": "llm"}, msg_id="m-asst", invoke_id=inv)
    db.append_event("s-delta-after-tools", tid, "stream.delta", {"text": "done"}, invoke_id=inv, chunk_seq=0)
    db.append_event("s-close-after-tools", tid, "stream.close", {}, invoke_id=inv)

    with monkeypatch.context() as m:
        m.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
        after_stream = _reduce_thread_events(db, tid)
    assert full_rebuild_calls == 0
    assert after_stream.tool_call_states["tc_hist"].state == "TC6"
    assert after_stream.next_runner_actionable is None
    _assert_incremental_matches_full_rebuild(db, tid)

    db.append_event("msg-user-after-tools", tid, "msg.create", {"role": "user", "content": "next"}, msg_id="m-user-after-tools")
    with monkeypatch.context() as m:
        m.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
        after_user = _reduce_thread_events(db, tid)
    assert full_rebuild_calls == 0
    assert after_user.tool_call_states["tc_hist"].state == "TC6"
    assert after_user.next_runner_actionable is not None
    assert after_user.next_runner_actionable.kind == "RA1_llm"
    assert after_user.next_runner_actionable.msg_id == "m-user-after-tools"
    _assert_incremental_matches_full_rebuild(db, tid)


def test_reducer_cache_incrementally_applies_summary_tail_after_active_tool(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-summary-active-tool"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event(
        "msg-asst",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "tc_active", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_active", "decision": "granted"})
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tc_active"},
        invoke_id="inv-tool-active",
    )

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_active"].state == "TC3"
    assert before.tool_call_states["tc_active"].summary is None
    assert before.next_runner_actionable is None

    full_rebuild_calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal full_rebuild_calls
        full_rebuild_calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)

    _append_event(db, tid, "tool_call.summary", {"tool_call_id": "tc_active", "summary": "running 1"})
    after_first_summary = _reduce_thread_events(db, tid)

    assert full_rebuild_calls == 0
    assert after_first_summary.tool_call_states["tc_active"].state == "TC3"
    assert after_first_summary.tool_call_states["tc_active"].summary == "running 1"
    assert after_first_summary.next_runner_actionable is None
    assert before.tool_call_states["tc_active"].summary is None
    assert after_first_summary.tool_call_states["tc_active"] is not before.tool_call_states["tc_active"]

    _append_event(db, tid, "tool_call.summary", {"tool_call_id": "tc_active", "summary": "running 2"})
    after_second_summary = _reduce_thread_events(db, tid)

    assert full_rebuild_calls == 0
    assert after_second_summary.tool_call_states["tc_active"].state == "TC3"
    assert after_second_summary.tool_call_states["tc_active"].summary == "running 2"
    assert after_second_summary.next_runner_actionable is None
    assert before.tool_call_states["tc_active"].summary is None
    assert after_first_summary.tool_call_states["tc_active"].summary == "running 1"
    assert after_second_summary.tool_call_states["tc_active"] is not after_first_summary.tool_call_states["tc_active"]

    events = [dict(row) for row in db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()]
    full = original(tid, db.max_event_seq(tid), events)
    assert _reduction_signature(after_second_summary) == _reduction_signature(full)

    _append_event(db, tid, "stream.close", {}, invoke_id="inv-tool-active")
    after_tool_stream_close = _reduce_thread_events(db, tid)

    assert full_rebuild_calls == 0
    assert after_tool_stream_close.tool_call_states["tc_active"].state == "TC5"
    assert after_tool_stream_close.tool_call_states["tc_active"].finished_reason == "interrupted"
    assert after_second_summary.tool_call_states["tc_active"].state == "TC3"
    assert after_tool_stream_close.tool_call_states["tc_active"] is not after_second_summary.tool_call_states["tc_active"]
    events = [dict(row) for row in db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()]
    full = original(tid, db.max_event_seq(tid), events)
    assert _reduction_signature(after_tool_stream_close) == _reduction_signature(full)


def _assert_incremental_tail_without_full_rebuild(db, tid: str, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal calls
        calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
    reduced = _reduce_thread_events(db, tid)

    assert calls == 0
    events = [dict(row) for row in db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()]
    full = original(tid, db.max_event_seq(tid), events)
    assert _reduction_signature(reduced) == _reduction_signature(full)
    return reduced


def _assert_next_reduce_uses_full_rebuild(db, tid: str, monkeypatch, *, require_prior_cache: bool = True):
    from eggthreads.tool_state import _REDUCER_CACHE, _reduce_loaded_thread_events, _reduce_thread_events

    if require_prior_cache:
        assert any(
            key[0] == str(db.path) and key[1] == tid and key[2] < db.max_event_seq(tid)
            for key in _REDUCER_CACHE
        )

    calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal calls
        calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
    reduced = _reduce_thread_events(db, tid)

    assert calls == 1
    events = [dict(row) for row in db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()]
    full = original(tid, db.max_event_seq(tid), events)
    assert _reduction_signature(reduced) == _reduction_signature(full)
    return reduced


def _append_assistant_tool_parent(db, tid: str, *, tcid: str = "tc_lifecycle") -> None:
    db.append_event(
        "msg-asst",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "tool_calls": [
                {"id": tcid, "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst",
    )


def test_reducer_cache_incrementally_applies_explicit_approval_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-approval"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC1"
    assert before.coarse_thread_state_without_lease == "waiting_tool_approval"

    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after.tool_call_states["tc_lifecycle"].state == "TC2.1"
    assert after.next_runner_actionable is not None
    assert after.next_runner_actionable.kind == "RA2_tools_assistant"
    assert before.tool_call_states["tc_lifecycle"].state == "TC1"
    assert after.tool_call_states["tc_lifecycle"] is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_explicit_denial_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-denial"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC1"

    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "denied"})
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after.tool_call_states["tc_lifecycle"].state == "TC2.2"
    assert after.next_runner_actionable is not None
    assert after.next_runner_actionable.kind == "RA2_tools_assistant"
    assert before.tool_call_states["tc_lifecycle"].state == "TC1"
    assert after.tool_call_states["tc_lifecycle"] is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_execution_started_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-execution-started"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC2.1"
    assert before.next_runner_actionable is not None

    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tc_lifecycle"},
        invoke_id="inv-tool-lifecycle",
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC3"
    assert tc.owner_invoke_id == "inv-tool-lifecycle"
    assert after.next_runner_actionable is None
    assert before.tool_call_states["tc_lifecycle"].state == "TC2.1"
    assert before.tool_call_states["tc_lifecycle"].owner_invoke_id is None
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_finished_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-finished"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tc_lifecycle"},
        invoke_id="inv-tool-lifecycle",
    )

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC3"

    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_lifecycle", "reason": "success", "output": "ok"})
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC4"
    assert tc.finished_reason == "success"
    assert tc.finished_output == "ok"
    assert after.coarse_thread_state_without_lease == "waiting_output_approval"
    assert before.tool_call_states["tc_lifecycle"].state == "TC3"
    assert before.tool_call_states["tc_lifecycle"].finished_reason is None
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_output_approval_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-output-approval"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_lifecycle"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_lifecycle", "reason": "success", "output": "ok"})

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC4"

    _append_event(
        db,
        tid,
        "tool_call.output_approval",
        {"tool_call_id": "tc_lifecycle", "decision": "whole", "preview": "ok"},
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC5"
    assert tc.output_decision == "whole"
    assert tc.last_output_approval_payload == {"tool_call_id": "tc_lifecycle", "decision": "whole", "preview": "ok"}
    assert after.next_runner_actionable is not None
    assert after.next_runner_actionable.kind == "RA2_tools_assistant"
    assert before.tool_call_states["tc_lifecycle"].state == "TC4"
    assert before.tool_call_states["tc_lifecycle"].output_decision is None
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_output_approval_without_finished_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-output-approval-no-finish"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC2.1"

    _append_event(
        db,
        tid,
        "tool_call.output_approval",
        {"tool_call_id": "tc_lifecycle", "decision": "omit", "preview": "Output omitted."},
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC5"
    assert tc.finished_reason == "interrupted"
    assert tc.finished_output == "Output omitted."
    assert before.tool_call_states["tc_lifecycle"].state == "TC2.1"
    assert before.tool_call_states["tc_lifecycle"].finished_reason is None
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_tool_interrupt_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-tool-interrupt"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tc_lifecycle"},
        invoke_id="inv-tool-lifecycle",
    )

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC3"

    _append_event(
        db,
        tid,
        "control.interrupt",
        {"reason": "user", "old_invoke_id": "inv-tool-lifecycle", "new_invoke_id": "inv-new", "purpose": "tool"},
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC5"
    assert tc.finished_reason == "interrupted"
    assert tc.output_decision == "whole"
    assert "interrupted before the tool reported a result" in (tc.finished_output or "")
    assert before.tool_call_states["tc_lifecycle"].state == "TC3"
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_applies_tool_stream_close_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-tool-close"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "tc_lifecycle"},
        invoke_id="inv-tool-lifecycle",
    )

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC3"

    _append_event(db, tid, "stream.close", {}, invoke_id="inv-tool-lifecycle")
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC5"
    assert tc.finished_reason == "interrupted"
    assert tc.output_decision == "whole"
    assert "stream closed before the tool reported a result" in (tc.finished_output or "")
    assert before.tool_call_states["tc_lifecycle"].state == "TC3"
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_tool_interrupt_without_matching_owner_is_noop(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-tool-interrupt-missing-owner"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC2.1"

    _append_event(
        db,
        tid,
        "control.interrupt",
        {"reason": "user", "old_invoke_id": "inv-missing", "new_invoke_id": "inv-new", "purpose": "tool"},
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after.tool_call_states["tc_lifecycle"].state == "TC2.1"


def test_reducer_cache_incrementally_applies_tool_result_publication_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-tool-result"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_lifecycle"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_lifecycle", "reason": "success", "output": "ok"})
    _append_event(
        db,
        tid,
        "tool_call.output_approval",
        {"tool_call_id": "tc_lifecycle", "decision": "whole", "preview": "ok"},
    )

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC5"
    assert before.next_runner_actionable is not None
    assert before.next_runner_actionable.kind == "RA2_tools_assistant"

    _append_event(
        db,
        tid,
        "msg.create",
        {"role": "tool", "tool_call_id": "tc_lifecycle", "content": "ok"},
        msg_id="m-tool-result",
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_lifecycle"]
    assert tc.state == "TC6"
    assert tc.published is True
    assert after.next_runner_actionable is not None
    assert after.next_runner_actionable.kind == "RA1_llm"
    assert after.next_runner_actionable.msg_id == "m-tool-result"
    assert [int(ev["event_seq"]) for ev in after.messages_after_boundary] == [db.max_event_seq(tid)]
    assert before.tool_call_states["tc_lifecycle"].state == "TC5"
    assert before.tool_call_states["tc_lifecycle"].published is False
    assert tc is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_incrementally_publishes_no_api_tool_result_without_ra1(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-tool-result-no-api"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid)
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_lifecycle", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_lifecycle"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_lifecycle", "reason": "success", "output": "ok"})
    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc_lifecycle", "decision": "whole", "preview": "ok"})

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_lifecycle"].state == "TC5"

    _append_event(
        db,
        tid,
        "msg.create",
        {"role": "tool", "tool_call_id": "tc_lifecycle", "content": "ok", "no_api": True},
        msg_id="m-tool-result-no-api",
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after.tool_call_states["tc_lifecycle"].state == "TC6"
    assert after.next_runner_actionable is None
    assert [int(ev["event_seq"]) for ev in after.messages_after_boundary] == [db.max_event_seq(tid)]
    assert before.tool_call_states["tc_lifecycle"].published is False
    assert after.tool_call_states["tc_lifecycle"] is not before.tool_call_states["tc_lifecycle"]


def test_reducer_cache_lifecycle_tail_falls_back_for_unresolved_tool_call(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-unknown-tool"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    _reduce_thread_events(db, tid)

    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "missing", "reason": "success", "output": "ok"})
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert "missing" not in reduced.tool_call_states


def test_reducer_cache_summary_tail_falls_back_for_malformed_tool_call_id(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-malformed-summary"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    _reduce_thread_events(db, tid)

    _append_event(db, tid, "tool_call.summary", {"summary": "orphaned summary"})
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert reduced.tool_call_states == {}


def test_reducer_cache_tool_result_tail_falls_back_for_unresolved_tool_call(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-unknown-tool-result"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    _reduce_thread_events(db, tid)

    _append_event(
        db,
        tid,
        "msg.create",
        {"role": "tool", "tool_call_id": "missing", "content": "ok"},
        msg_id="m-missing-tool-result",
    )
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert "missing" not in reduced.tool_call_states


def test_reducer_cache_tool_call_declaration_tail_falls_back_for_malformed_tool_call_id(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-malformed-declaration"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    _reduce_thread_events(db, tid)

    db.append_event(
        "msg-bad-tool-id",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "tool_calls": [
                {"id": 123, "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-bad-tool-id",
    )
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert 123 in reduced.tool_call_states
    assert reduced.tool_call_states[123].tool_call_id == "123"


def test_reducer_cache_incrementally_applies_assistant_tool_call_declaration_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-assistant-declaration"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")

    before = _reduce_thread_events(db, tid)
    assert before.next_runner_actionable is not None
    assert before.next_runner_actionable.kind == "RA1_llm"
    assert before.tool_call_states == {}

    db.append_event(
        "msg-asst-tool",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "tc_decl", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst-tool",
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_decl"]
    assert tc.parent_role == "assistant"
    assert tc.parent_msg_id == "m-asst-tool"
    assert tc.state == "TC1"
    assert after.next_runner_actionable is None
    assert after.coarse_thread_state_without_lease == "waiting_tool_approval"
    assert after.messages_after_boundary == []
    assert before.tool_call_states == {}


def test_reducer_cache_incrementally_applies_user_tool_call_declaration_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-user-declaration"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    before = _reduce_thread_events(db, tid)
    assert before.next_runner_actionable is None
    assert before.tool_call_states == {}

    db.append_event(
        "msg-user-tool",
        tid,
        "msg.create",
        {
            "role": "user",
            "content": "cmd",
            "tool_calls": [
                {"id": "tc_user_decl", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user-tool",
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    tc = after.tool_call_states["tc_user_decl"]
    assert tc.parent_role == "user"
    assert tc.parent_msg_id == "m-user-tool"
    assert tc.state == "TC1"
    assert after.next_runner_actionable is None
    assert after.coarse_thread_state_without_lease == "waiting_tool_approval"
    assert [int(ev["event_seq"]) for ev in after.messages_after_boundary] == [db.max_event_seq(tid)]
    assert before.tool_call_states == {}


def test_reducer_cache_tool_call_declaration_tail_falls_back_for_reused_tool_call_id(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-reused-declaration"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event(
        "msg-old-tool",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_reused", "type": "function", "function": {"name": "bash", "arguments": "old"}},
            ],
        },
        msg_id="m-old-tool",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_reused", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_reused"})
    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_reused", "reason": "success", "output": "old"})
    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc_reused", "decision": "whole", "preview": "old"})
    _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": "tc_reused", "content": "old"}, msg_id="m-old-result")

    before = _reduce_thread_events(db, tid)
    before_tc = before.tool_call_states["tc_reused"]
    assert before_tc.state == "TC6"
    assert before_tc.parent_msg_id == "m-old-tool"
    assert before_tc.published is True

    db.append_event(
        "msg-new-tool",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_reused", "type": "function", "function": {"name": "bash", "arguments": "new"}},
            ],
        },
        msg_id="m-new-tool",
    )

    calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal calls
        calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)
    after = _reduce_thread_events(db, tid)

    after_tc = after.tool_call_states["tc_reused"]
    assert calls == 1
    assert after_tc.state == "TC2.1"
    assert after_tc.parent_msg_id == "m-new-tool"
    assert after_tc.arguments == "new"
    assert after_tc.approval_decision == "granted"
    assert after_tc.published is False
    assert before_tc.state == "TC6"
    assert before_tc.parent_msg_id == "m-old-tool"
    assert after_tc is not before_tc


def test_reducer_cache_incrementally_applies_declaration_after_global_approval(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-declaration-after-global"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("global-approval", tid, "tool_call.approval", {"decision": "global_approval"})

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states == {}

    db.append_event(
        "msg-user-tool-global",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_global", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user-tool-global",
    )
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after.tool_call_states["tc_global"].state == "TC2.1"
    assert before.tool_call_states == {}


def test_reducer_cache_incrementally_applies_all_in_turn_approval_tail(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-all-in-turn"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event(
        "msg-user-old-tool",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_old", "type": "function", "function": {"name": "bash", "arguments": "old"}},
            ],
        },
        msg_id="m-user-old-tool",
    )
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_old", "decision": "denied"})
    db.append_event("msg-user-turn", tid, "msg.create", {"role": "user", "content": "new turn"}, msg_id="m-user-turn")
    db.append_event(
        "msg-asst-tool-turn",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "tc_turn_a", "type": "function", "function": {"name": "bash", "arguments": "a"}},
                {"id": "tc_turn_b", "type": "function", "function": {"name": "python", "arguments": "b"}},
            ],
        },
        msg_id="m-asst-tool-turn",
    )

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states["tc_old"].state == "TC2.2"
    assert before.tool_call_states["tc_turn_a"].state == "TC1"
    assert before.tool_call_states["tc_turn_b"].state == "TC1"

    _append_event(db, tid, "tool_call.approval", {"decision": "all-in-turn"})
    after = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after.tool_call_states["tc_old"].state == "TC2.2"
    assert after.tool_call_states["tc_turn_a"].state == "TC2.1"
    assert after.tool_call_states["tc_turn_b"].state == "TC2.1"
    assert after.next_runner_actionable is not None
    assert after.next_runner_actionable.kind == "RA3_tools_user"
    assert before.tool_call_states["tc_turn_a"].state == "TC1"
    assert before.tool_call_states["tc_turn_b"].state == "TC1"
    assert after.tool_call_states["tc_turn_a"] is not before.tool_call_states["tc_turn_a"]
    assert after.tool_call_states["tc_turn_b"] is not before.tool_call_states["tc_turn_b"]


def test_reducer_cache_incrementally_applies_global_approval_and_revoke_tails(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-global-approval"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states == {}

    db.append_event("global-approval", tid, "tool_call.approval", {"decision": "global_approval"})
    after_global = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after_global._current_global_start == db.max_event_seq(tid)
    assert before._current_global_start is None

    db.append_event(
        "msg-user-tool-global-tail",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_global_tail", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user-tool-global-tail",
    )
    after_decl = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after_decl.tool_call_states["tc_global_tail"].state == "TC2.1"

    db.append_event("global-revoke", tid, "tool_call.approval", {"decision": "revoke_global_approval"})
    after_revoke = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after_revoke._current_global_start is None

    db.append_event(
        "msg-user-tool-after-revoke",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_after_revoke", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user-tool-after-revoke",
    )
    after_second_decl = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)

    assert after_second_decl.tool_call_states["tc_after_revoke"].state == "TC1"


def test_reducer_cache_declaration_tail_falls_back_after_all_in_turn_approval(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-declaration-after-all-in-turn"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "turn"}, msg_id="m-user")
    db.append_event("approve-turn", tid, "tool_call.approval", {"decision": "all-in-turn"})

    before = _reduce_thread_events(db, tid)
    assert before.tool_call_states == {}

    calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal calls
        calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)

    db.append_event(
        "msg-user-tool-turn",
        tid,
        "msg.create",
        {
            "role": "user",
            "tool_calls": [
                {"id": "tc_turn", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user-tool-turn",
    )
    after = _reduce_thread_events(db, tid)

    assert calls == 1
    assert after.tool_call_states["tc_turn"].state == "TC1"
    _assert_incremental_matches_full_rebuild(db, tid)


def test_reducer_cache_incrementally_applies_combined_assistant_tool_round_trip(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-combined-round-trip"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    before = _reduce_thread_events(db, tid)
    assert before.next_runner_actionable is not None
    assert before.next_runner_actionable.kind == "RA1_llm"

    db.append_event(
        "msg-asst-tool-combined",
        tid,
        "msg.create",
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "tc_combined", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-asst-tool-combined",
    )
    after_decl = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)
    assert after_decl.tool_call_states["tc_combined"].state == "TC1"

    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_combined", "decision": "granted"})
    after_approval = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)
    assert after_approval.tool_call_states["tc_combined"].state == "TC2.1"

    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_combined"}, invoke_id="inv-combined")
    after_start = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)
    assert after_start.tool_call_states["tc_combined"].state == "TC3"

    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_combined", "reason": "success", "output": "ok"})
    after_finish = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)
    assert after_finish.tool_call_states["tc_combined"].state == "TC4"

    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc_combined", "decision": "whole", "preview": "ok"})
    after_output_approval = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)
    assert after_output_approval.tool_call_states["tc_combined"].state == "TC5"

    _append_event(
        db,
        tid,
        "msg.create",
        {"role": "tool", "tool_call_id": "tc_combined", "content": "ok"},
        msg_id="m-tool-combined",
    )
    after_publish = _assert_incremental_tail_without_full_rebuild(db, tid, monkeypatch)
    assert after_publish.tool_call_states["tc_combined"].state == "TC6"
    assert after_publish.next_runner_actionable is not None
    assert after_publish.next_runner_actionable.kind == "RA1_llm"
    assert after_publish.next_runner_actionable.msg_id == "m-tool-combined"


def test_reducer_cache_synthetic_long_thread_normal_tails_do_not_rebuild(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-synthetic-long"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    for idx in range(120):
        tcid = f"tc_hist_{idx}"
        msg_id = f"m-hist-user-{idx}"
        db.append_event(
            f"hist-msg-{idx}",
            tid,
            "msg.create",
            {
                "role": "user",
                "content": f"cmd {idx}",
                "tool_calls": [
                    {"id": tcid, "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ],
            },
            msg_id=msg_id,
        )
        _append_event(db, tid, "tool_call.approval", {"tool_call_id": tcid, "decision": "granted"})
        _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": tcid}, invoke_id=f"inv-hist-tool-{idx}")
        _append_event(db, tid, "tool_call.summary", {"tool_call_id": tcid, "summary": f"running {idx}"})
        _append_event(db, tid, "tool_call.finished", {"tool_call_id": tcid, "reason": "success", "output": f"ok {idx}"})
        _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": tcid, "decision": "whole", "preview": f"ok {idx}"})
        _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": tcid, "content": f"ok {idx}"}, msg_id=f"m-hist-tool-{idx}")
        inv = f"inv-hist-llm-{idx}"
        db.append_event(f"hist-llm-open-{idx}", tid, "stream.open", {"stream_kind": "llm"}, msg_id=f"m-hist-asst-{idx}", invoke_id=inv)
        db.append_event(f"hist-llm-delta-{idx}", tid, "stream.delta", {"text": f"done {idx}"}, invoke_id=inv, chunk_seq=idx)
        db.append_event(f"hist-asst-{idx}", tid, "msg.create", {"role": "assistant", "content": f"done {idx}"}, msg_id=f"m-hist-asst-{idx}")
        db.append_event(f"hist-llm-close-{idx}", tid, "stream.close", {}, invoke_id=inv)

    db.append_event("tail-active-tool-parent", tid, "msg.create", {
        "role": "assistant",
        "tool_calls": [
            {"id": "tc_tail_active", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }, msg_id="m-tail-active-tool-parent")
    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_tail_active", "decision": "granted"})
    _append_event(db, tid, "tool_call.execution_started", {"tool_call_id": "tc_tail_active"}, invoke_id="inv-tail-active-tool")

    warm = _reduce_thread_events(db, tid)
    assert warm.tool_call_states["tc_tail_active"].state == "TC3"

    calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal calls
        calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)

    reductions = []
    start = time.perf_counter()

    def reduce_tail(label: str):
        reduced = _reduce_thread_events(db, tid)
        reductions.append((label, reduced.max_event_seq))
        assert calls == 0
        assert _reduction_signature(reduced) == _full_rebuild_signature(db, tid, original)
        return reduced

    _append_event(db, tid, "tool_call.summary", {"tool_call_id": "tc_tail_active", "summary": "still running"})
    after_summary = reduce_tail("summary-only")
    assert after_summary.tool_call_states["tc_tail_active"].summary == "still running"

    inv = "inv-tail-llm"
    db.append_event("tail-stream-open", tid, "stream.open", {"stream_kind": "llm"}, msg_id="m-tail-stream", invoke_id=inv)
    reduce_tail("stream-open")
    db.append_event("tail-stream-delta", tid, "stream.delta", {"text": "partial"}, invoke_id=inv, chunk_seq=10_000)
    reduce_tail("stream-delta")
    db.append_event("tail-stream-close", tid, "stream.close", {}, invoke_id=inv)
    after_stream_close = reduce_tail("stream-close")
    assert after_stream_close.next_runner_actionable is None

    _append_event(db, tid, "tool_call.finished", {"tool_call_id": "tc_tail_active", "reason": "success", "output": "ok"})
    after_finish = reduce_tail("tool-finished")
    assert after_finish.tool_call_states["tc_tail_active"].state == "TC4"

    _append_event(db, tid, "tool_call.output_approval", {"tool_call_id": "tc_tail_active", "decision": "whole", "preview": "ok"})
    after_output_approval = reduce_tail("tool-output-approval")
    assert after_output_approval.tool_call_states["tc_tail_active"].state == "TC5"

    _append_event(db, tid, "msg.create", {"role": "tool", "tool_call_id": "tc_tail_active", "content": "ok"}, msg_id="m-tail-tool-result")
    after_tool_msg = reduce_tail("tool-result-message")
    assert after_tool_msg.tool_call_states["tc_tail_active"].state == "TC6"
    assert after_tool_msg.next_runner_actionable is not None
    assert after_tool_msg.next_runner_actionable.kind == "RA1_llm"
    assert after_tool_msg.next_runner_actionable.msg_id == "m-tail-tool-result"

    db.append_event("tail-user-tool-parent", tid, "msg.create", {
        "role": "user",
        "content": "run user tool",
        "tool_calls": [
            {"id": "tc_tail_user", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ],
    }, msg_id="m-tail-user-tool-parent")
    after_user_tool = reduce_tail("next-user-tool-trigger")
    assert after_user_tool.tool_call_states["tc_tail_user"].state == "TC1"
    assert after_user_tool.next_runner_actionable is not None
    assert after_user_tool.next_runner_actionable.kind == "RA1_llm"
    assert after_user_tool.coarse_thread_state_without_lease == "running"

    elapsed = time.perf_counter() - start
    assert calls == 0
    assert len(reductions) == 8
    assert elapsed >= 0.0


def test_last_stream_close_seq_reuses_reducer_cache(tmp_path):
    from eggthreads.tool_state import _last_stream_close_seq, _last_stream_close_seq_uncached, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-boundary-cache"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    inv = "inv-boundary"
    db.append_event("s-open", tid, "stream.open", {"stream_kind": "llm"}, msg_id="m-asst", invoke_id=inv)
    db.append_event("s-close", tid, "stream.close", {}, invoke_id=inv)

    assert _reduce_thread_events(db, tid).last_llm_boundary_seq == _last_stream_close_seq_uncached(db, tid)

    expected = _last_stream_close_seq_uncached(db, tid)

    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        assert _last_stream_close_seq(db, tid) == expected
    finally:
        db.conn.set_trace_callback(None)

    event_queries = [stmt for stmt in statements if " FROM events" in stmt]
    assert event_queries == [
        f"SELECT MAX(event_seq) FROM events WHERE thread_id='{tid}'",
    ]


def test_reducer_cache_msg_edit_falls_back_to_full_rebuild(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-hard-event"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)

    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    assert _reduce_thread_events(db, tid).next_runner_actionable is not None

    db.append_event("msg-edit", tid, "msg.edit", {"skipped_on_continue": True}, msg_id="m-user")

    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert reduced.next_runner_actionable is None


def test_reducer_cache_msg_delete_falls_back_to_full_rebuild(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-msg-delete"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    assert _reduce_thread_events(db, tid).next_runner_actionable is not None

    db.append_event("msg-delete", tid, "msg.delete", {}, msg_id="m-user")
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert reduced.next_runner_actionable is not None
    assert reduced.next_runner_actionable.msg_id == "m-user"


def test_reducer_cache_continue_interrupt_falls_back_to_full_rebuild(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-continue-interrupt"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    assert _reduce_thread_events(db, tid).next_runner_actionable is not None

    db.append_event(
        "continue",
        tid,
        "control.interrupt",
        {"reason": "continue", "purpose": "continue", "continue_from_msg_id": "m-user"},
    )
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert reduced.next_runner_actionable is not None
    assert reduced.next_runner_actionable.msg_id == "m-user"


def test_reducer_cache_falls_back_for_impossible_cached_tool_event_ordering(tmp_path, monkeypatch):
    from dataclasses import replace

    from eggthreads.tool_state import _REDUCER_CACHE, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-impossible-ordering"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    _append_assistant_tool_parent(db, tid, tcid="tc_impossible")
    baseline = _reduce_thread_events(db, tid)
    tc = baseline.tool_call_states["tc_impossible"]

    impossible = replace(
        baseline,
        tool_call_states={
            "tc_impossible": replace(tc, parent_event_seq=db.max_event_seq(tid) + 100),
        },
    )
    _REDUCER_CACHE.clear()
    _REDUCER_CACHE[(str(db.path), tid, impossible.max_event_seq)] = impossible

    _append_event(db, tid, "tool_call.approval", {"tool_call_id": "tc_impossible", "decision": "granted"})
    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch)

    assert reduced.tool_call_states["tc_impossible"].state == "TC2.1"


def test_reducer_cache_ignores_future_cached_watermark_and_full_rebuilds(tmp_path, monkeypatch):
    from eggthreads.tool_state import _REDUCER_CACHE, _reduce_thread_events

    db = _make_db(tmp_path)
    tid = "thread-incremental-impossible-watermark"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")
    first = _reduce_thread_events(db, tid)
    assert first.next_runner_actionable is not None

    _REDUCER_CACHE.clear()
    _REDUCER_CACHE[(str(db.path), tid, db.max_event_seq(tid) + 100)] = first
    db.append_event("msg-user-2", tid, "msg.create", {"role": "user", "content": "next"}, msg_id="m-user-2")

    reduced = _assert_next_reduce_uses_full_rebuild(db, tid, monkeypatch, require_prior_cache=False)

    assert reduced.max_event_seq == db.max_event_seq(tid)


def test_thread_state_reuses_reducer_after_actionable_cache(tmp_path):
    from eggthreads.tool_state import discover_runner_actionable_cached, thread_state

    db = _make_db(tmp_path)
    tid = "thread-state-query-count"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user")

    assert discover_runner_actionable_cached(db, tid).kind == "RA1_llm"

    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        assert thread_state(db, tid) == "running"
    finally:
        db.conn.set_trace_callback(None)

    event_queries = [stmt for stmt in statements if " FROM events" in stmt]
    assert event_queries == [
        f"SELECT MAX(event_seq) FROM events WHERE thread_id='{tid}'"
    ]


def test_thread_state_uses_incremental_reducer_tail_after_warmup(tmp_path, monkeypatch):
    from eggthreads.tool_state import _reduce_loaded_thread_events, _reduce_thread_events, thread_state

    db = _make_db(tmp_path)
    tid = "thread-state-incremental-tail"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event("msg-user-1", tid, "msg.create", {"role": "user", "content": "hello"}, msg_id="m-user-1")

    assert _reduce_thread_events(db, tid).coarse_thread_state_without_lease == "running"

    calls = 0
    original = _reduce_loaded_thread_events

    def counting_full_rebuild(thread_id, max_event_seq, events):
        nonlocal calls
        calls += 1
        return original(thread_id, max_event_seq, events)

    monkeypatch.setattr("eggthreads.tool_state._reduce_loaded_thread_events", counting_full_rebuild)

    db.append_event("msg-user-2", tid, "msg.create", {"role": "user", "content": "next"}, msg_id="m-user-2")

    assert thread_state(db, tid) == "running"
    assert calls == 0


def test_build_tool_call_states_returns_cache_safe_copies(tmp_path):
    db = _make_db(tmp_path)
    tid = "thread-state-copy"
    db.create_thread(thread_id=tid, name="t", parent_id=None, depth=0)
    db.append_event(
        "msg-user",
        tid,
        "msg.create",
        {
            "role": "user",
            "content": "cmd",
            "tool_calls": [
                {"id": "tc_copy", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        msg_id="m-user",
    )

    first = eggthreads.build_tool_call_states(db, tid)
    first["tc_copy"].approval_decision = "granted"

    second = eggthreads.build_tool_call_states(db, tid)

    assert second["tc_copy"].approval_decision is None
    assert second["tc_copy"].state == "TC1"
