from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from .db import ThreadsDB


@dataclass
class ToolCallState:
    """Represents the state of a single tool call within a thread.

    States (TC = Tool Call):
      - TC1: needs approval
      - TC2.1: approved
      - TC2.2: denied
      - TC3: executing
      - TC4: finished (tool completed, waiting for output approval)
      - TC5: output decision made (waiting for publish)
      - TC6: publishing done (final tool message exists)
    """

    thread_id: str
    tool_call_id: str
    parent_msg_id: str
    parent_event_seq: int
    index: int  # index within tool_calls list of parent message
    name: str
    arguments: Any

    # Derived state
    approval_decision: Optional[str] = None  # "granted" | "denied"
    execution_started: bool = False
    finished_reason: Optional[str] = None  # "success" | "interrupted" | ...
    output_decision: Optional[str] = None  # "whole" | "partial" | "omit"
    published: bool = False  # final tool message written

    @property
    def state(self) -> str:
        if self.published:
            return "TC6"
        if self.output_decision is not None:
            return "TC5"
        if self.finished_reason is not None:
            return "TC4"
        if self.execution_started:
            return "TC3"
        if self.approval_decision == "granted":
            return "TC2.1"
        if self.approval_decision == "denied":
            return "TC2.2"
        return "TC1"


@dataclass
class RunnerActionable:
    """Describes a unit of work the ThreadRunner can perform.

    kind:
      - "RA1_llm"            -> call LLM (assistant turn)
      - "RA2_tools_assistant" -> process assistant-originated tool calls
      - "RA3_tools_user"      -> process user-originated tool calls (user commands)
    """

    kind: str
    thread_id: str
    triggering_event_seq: int
    msg_id: Optional[str] = None
    tool_calls: Optional[List[ToolCallState]] = None


def _iter_events(db: ThreadsDB, thread_id: str) -> Iterable[Dict[str, Any]]:
    cur = db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (thread_id,),
    )
    for row in cur.fetchall():
        yield dict(row)


def build_tool_call_states(db: ThreadsDB, thread_id: str) -> Dict[str, ToolCallState]:
    """Scan events for a thread and reconstruct ToolCallState per tool_call_id.

    This is intentionally stateless and computed on demand; threads are
    typically small enough that this is acceptable, and it avoids schema
    changes.
    """
    states: Dict[str, ToolCallState] = {}

    # First pass: find messages that declare tool_calls
    for ev in _iter_events(db, thread_id):
        if ev.get("type") != "msg.create":
            continue
        ev_seq = int(ev["event_seq"])
        msg_id = ev.get("msg_id") or ""
        try:
            payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
        except Exception:
            payload = {}
        tcs = payload.get("tool_calls") or []
        if not isinstance(tcs, list) or not tcs:
            continue
        for idx, tc in enumerate(tcs):
            if not isinstance(tc, dict):
                continue
            tcid = (tc.get("id") or f"{msg_id}:{idx}")
            fn = (tc.get("function") or {})
            name = fn.get("name") or tc.get("name") or ""
            args = fn.get("arguments") if "function" in tc else tc.get("arguments")
            states[tcid] = ToolCallState(
                thread_id=thread_id,
                tool_call_id=str(tcid),
                parent_msg_id=msg_id,
                parent_event_seq=ev_seq,
                index=idx,
                name=str(name),
                arguments=args,
            )

    if not states:
        return states

    # Second pass: fold tool_call.* events and tool messages into states
    for ev in _iter_events(db, thread_id):
        ev_type = ev.get("type")
        try:
            payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
        except Exception:
            payload = {}

        if ev_type == "tool_call.approval":
            tcid = payload.get("tool_call_id")
            if tcid in states:
                decision = payload.get("decision")
                if isinstance(decision, str):
                    states[tcid].approval_decision = decision
        elif ev_type == "tool_call.execution_started":
            tcid = payload.get("tool_call_id")
            if tcid in states:
                states[tcid].execution_started = True
        elif ev_type == "tool_call.finished":
            tcid = payload.get("tool_call_id")
            if tcid in states:
                reason = payload.get("reason")
                if isinstance(reason, str):
                    states[tcid].finished_reason = reason
        elif ev_type == "tool_call.output_approval":
            tcid = payload.get("tool_call_id")
            if tcid in states:
                decision = payload.get("decision")
                if isinstance(decision, str):
                    states[tcid].output_decision = decision
        elif ev_type == "msg.create":
            # Final published tool result
            try:
                role = payload.get("role")
            except Exception:
                role = None
            if role == "tool":
                tcid = payload.get("tool_call_id")
                if tcid in states:
                    states[tcid].published = True

    return states


def list_tool_calls_for_message(db: ThreadsDB, thread_id: str, msg_id: str) -> List[ToolCallState]:
    """Return ToolCallState objects for tool calls declared in a given message."""
    all_states = build_tool_call_states(db, thread_id)
    out = [tc for tc in all_states.values() if tc.parent_msg_id == msg_id]
    out.sort(key=lambda tc: tc.index)
    return out


def list_tool_calls_for_thread(db: ThreadsDB, thread_id: str) -> List[ToolCallState]:
    """Return ToolCallState objects for all tool calls in this thread."""
    all_states = build_tool_call_states(db, thread_id)
    return sorted(all_states.values(), key=lambda tc: (tc.parent_event_seq, tc.index))


def _last_stream_close_seq(db: ThreadsDB, thread_id: str) -> int:
    row = db.conn.execute(
        "SELECT MAX(event_seq) FROM events WHERE thread_id=? AND type='stream.close'",
        (thread_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else -1


def _iter_messages_after(db: ThreadsDB, thread_id: str, after_seq: int) -> Iterable[Dict[str, Any]]:
    cur = db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? AND type='msg.create' AND event_seq>? ORDER BY event_seq ASC",
        (thread_id, after_seq),
    )
    for row in cur.fetchall():
        yield dict(row)


def discover_runner_actionable(db: ThreadsDB, thread_id: str) -> Optional[RunnerActionable]:
    """Determine the next actionable work item for a thread.

    This function encapsulates the Runner Actionables (RA1/RA2/RA3) logic
    based solely on the event log and tool call states.
    """
    last_close = _last_stream_close_seq(db, thread_id)
    all_states = build_tool_call_states(db, thread_id)

    for ev in _iter_messages_after(db, thread_id, last_close):
        ev_seq = int(ev["event_seq"])
        msg_id = ev.get("msg_id") or ""
        try:
            payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
        except Exception:
            payload = {}
        role = payload.get("role")
        keep_user_turn = bool(payload.get("keep_user_turn"))
        no_api = bool(payload.get("no_api"))
        tool_calls = payload.get("tool_calls") or []

        # Map tool_call_ids in this message to their states
        tcs_for_msg: List[ToolCallState] = []
        if isinstance(tool_calls, list) and tool_calls:
            for idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                tcid = tc.get("id") or f"{msg_id}:{idx}"
                st = all_states.get(str(tcid))
                if st:
                    tcs_for_msg.append(st)
            tcs_for_msg.sort(key=lambda tc: tc.index)

        # RA3: user-originated tool calls (user commands / user tools)
        if role == "user" and tcs_for_msg:
            # Runnable tool calls: those not yet published and whose state the runner can advance
            runnable = [tc for tc in tcs_for_msg if tc.state in ("TC2.1", "TC2.2", "TC5")]
            if runnable:
                return RunnerActionable(
                    kind="RA3_tools_user",
                    thread_id=thread_id,
                    triggering_event_seq=ev_seq,
                    msg_id=msg_id,
                    tool_calls=runnable,
                )

        # RA2: assistant-originated tool calls
        if role == "assistant" and tcs_for_msg:
            runnable = [tc for tc in tcs_for_msg if tc.state in ("TC2.1", "TC2.2", "TC5")]
            if runnable:
                return RunnerActionable(
                    kind="RA2_tools_assistant",
                    thread_id=thread_id,
                    triggering_event_seq=ev_seq,
                    msg_id=msg_id,
                    tool_calls=runnable,
                )

        # RA1: LLM call
        # - user messages without tool_calls and without keep_user_turn
        # - tool messages that are not no_api and not keep_user_turn
        if role == "user" and not tcs_for_msg and not keep_user_turn:
            return RunnerActionable(
                kind="RA1_llm",
                thread_id=thread_id,
                triggering_event_seq=ev_seq,
                msg_id=msg_id,
                tool_calls=None,
            )
        if role == "tool" and not no_api and not keep_user_turn:
            return RunnerActionable(
                kind="RA1_llm",
                thread_id=thread_id,
                triggering_event_seq=ev_seq,
                msg_id=msg_id,
                tool_calls=None,
            )

    return None


def thread_state(db: ThreadsDB, thread_id: str) -> str:
    """Coarse thread state used by tools and UIs.

    Returns one of:
      - "running"                 (streaming or runnable RA present)
      - "waiting_tool_approval"   (TC1 exists, no RA)
      - "waiting_output_approval" (TC4 exists, no RA)
      - "waiting_user"            (idle, waiting for user input)
      - "paused"                  (thread.status == 'paused')
    """
    th = db.get_thread(thread_id)
    if th and th.status == "paused":
        return "paused"

    # Active stream -> running
    try:
        row = db.current_open(thread_id)
    except Exception:
        row = None
    if row is not None:
        return "running"

    # Any actionable RA -> running
    if discover_runner_actionable(db, thread_id) is not None:
        return "running"

    # Otherwise, inspect tool call states
    all_states = build_tool_call_states(db, thread_id)
    any_tc1 = any(tc.state == "TC1" for tc in all_states.values())
    any_tc4 = any(tc.state == "TC4" for tc in all_states.values())

    if any_tc1:
        return "waiting_tool_approval"
    if any_tc4:
        return "waiting_output_approval"
    return "waiting_user"
