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
from typing import Dict, Any

import eggthreads


def _make_db(tmp_path):
    db = eggthreads.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _append_event(db, tid: str, type_: str, payload: Dict[str, Any], *, msg_id: str | None = None) -> None:
    """Append a JSON event with a fresh ULID-like event_id.

    Using the public ``append_event`` helper would also work but is not
    currently re-exported; we call ``ThreadsDB.append_event`` directly
    with a simple counter-based id for tests.
    """

    # Very small, deterministic id for tests – uniqueness is enough.
    eid = f"{type_}-{db.max_event_seq(tid)+1}"
    db.append_event(event_id=eid, thread_id=tid, type_=type_, payload=payload, msg_id=msg_id)


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
