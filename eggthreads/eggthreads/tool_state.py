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
    parent_role: Optional[str]
    index: int  # index within tool_calls list of parent message
    name: str
    arguments: Any

    # Derived state
    approval_decision: Optional[str] = None  # "granted" | "denied"
    execution_started: bool = False
    finished_reason: Optional[str] = None  # "success" | "interrupted" | ...
    finished_output: Optional[str] = None  # full tool output from tool_call.finished, if any
    output_decision: Optional[str] = None  # "whole" | "partial" | "omit"
    published: bool = False  # final tool message written
    # Last output_approval payload (if any) for this tool call; allows UI to
    # encode preview/truncation/paths that the runner can later use when
    # publishing the final tool message.
    last_output_approval_payload: Optional[Dict[str, Any]] = None

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

    # Track global tool auto-approval intervals for this thread. These
    # are derived purely from tool_call.approval events with
    # decision="global_approval" / "revoke_global_approval".
    global_auto_approval = False
    global_intervals: list[tuple[int, Optional[int]]] = []
    current_global_start: Optional[int] = None

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
        role = payload.get("role")
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
                parent_role=str(role) if isinstance(role, str) else None,
                index=idx,
                name=str(name),
                arguments=args,
            )

    if not states:
        return states

    # Precompute user message event sequences so that "all-in-turn"
    # approvals can be scoped from the approval event until the next
    # user message.
    user_seqs: list[int] = []
    for ev in _iter_events(db, thread_id):
        if ev.get("type") != "msg.create":
            continue
        try:
            payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
        except Exception:
            payload = {}
        if payload.get("role") == "user":
            try:
                user_seqs.append(int(ev["event_seq"]))
            except Exception:
                continue

    # Second pass: fold tool_call.* events and tool messages into states,
    # and record global auto-approval intervals.
    for ev in _iter_events(db, thread_id):
        ev_type = ev.get("type")
        try:
            payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
        except Exception:
            payload = {}

        if ev_type == "tool_call.approval":
            decision = payload.get("decision")
            # global_approval / revoke_global_approval mark intervals
            # (by event_seq) during which automatic approval is active
            # for all tool calls in this thread.
            if decision == "global_approval":
                global_auto_approval = True
                try:
                    current_global_start = int(ev.get("event_seq"))
                except Exception:
                    current_global_start = None
                continue
            if decision == "revoke_global_approval":
                if global_auto_approval and current_global_start is not None:
                    try:
                        end_seq = int(ev.get("event_seq"))
                    except Exception:
                        end_seq = None
                    global_intervals.append((current_global_start, end_seq))
                global_auto_approval = False
                current_global_start = None
                continue

            # Special case: "all-in-turn" means: mark all tool calls in the
            # current USER turn as granted, even if some tool calls are
            # declared after this event. A USER turn is the span between
            # one user msg.create and the next.
            if decision == "all-in-turn":
                try:
                    cur_seq = int(ev.get("event_seq"))
                except Exception:
                    cur_seq = None
                if cur_seq is None:
                    continue
                prev_user_seq = -1
                next_user_seq = None
                if user_seqs:
                    for us in user_seqs:
                        if us <= cur_seq:
                            prev_user_seq = us
                        elif us > cur_seq and next_user_seq is None:
                            next_user_seq = us
                            break
                for tc in states.values():
                    if tc.approval_decision is not None:
                        continue
                    # Parent must exist within the approved user turn
                    if tc.parent_event_seq < prev_user_seq:
                        continue
                    if next_user_seq is not None and tc.parent_event_seq >= next_user_seq:
                        continue
                    tc.approval_decision = "granted"
            else:
                tcid = payload.get("tool_call_id")
                if tcid in states:
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
                out = payload.get('output')
                if out is not None:
                    states[tcid].finished_output = str(out)
        elif ev_type == "tool_call.output_approval":
            tcid = payload.get("tool_call_id")
            if tcid in states:
                decision = payload.get("decision")
                if isinstance(decision, str):
                    states[tcid].output_decision = decision
                # Preserve full payload for later use when publishing
                states[tcid].last_output_approval_payload = payload
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

    # Finalize global intervals: if auto-approval was still active at the
    # end of the log, close the interval with an open-ended end (None).
    if global_auto_approval and current_global_start is not None:
        global_intervals.append((current_global_start, None))

    # Helper: is a given event_seq covered by any global auto-approval
    # interval? This is purely derived from events.
    def _has_global_approval(ev_seq: int) -> bool:
        for start, end in global_intervals:
            if start is None:
                continue
            if ev_seq < start:
                continue
            if end is not None and ev_seq > end:
                continue
            return True
        return False

    # Apply global auto-approval to any tool calls that are still in TC1
    # and whose parent message falls within an active interval (i.e.,
    # that were created after the last global_approval and before a
    # revoke_global_approval).
    for tc in states.values():
        if tc.approval_decision is None and _has_global_approval(tc.parent_event_seq):
            tc.approval_decision = "granted"

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
    """Return the event_seq of the last *LLM* stream boundary for a thread.

    Boundaries are either:
      - a stream.close whose invoke_id saw LLM-style deltas (``text`` or
        ``reason`` fields), or
      - a control.interrupt whose payload.old_invoke_id matches such an
        invoke_id.

    We intentionally ignore stream.close events that belong purely to
    tool-execution streams (RA2/RA3). Those streams only emit deltas
    with a ``tool`` field (and no top-level ``text``/``reason``),
    whereas LLM streams (RA1) emit deltas with ``text`` and/or
    ``reason`` keys.

    This distinction matters because RA1 (LLM turns) should be driven by
    user/tool messages that appear *after* the last LLM turn finishes or
    is explicitly interrupted by the user, but we do not want
    tool-execution streams to reset that boundary.
    """
    last_close = -1
    llm_invokes: set[str] = set()

    # Single pass over events in order: mark invoke_ids that have LLM
    # deltas, then record the last stream.close/control.interrupt for any
    # such invoke_id.
    cur = db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (thread_id,),
    )
    for row in cur.fetchall():
        ev = dict(row)
        t = ev.get("type")
        inv = ev.get("invoke_id")
        if t == "stream.delta":
            try:
                payload = json.loads(ev.get("payload_json")) if isinstance(ev.get("payload_json"), str) else (ev.get("payload_json") or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and ("text" in payload or "reason" in payload):
                if isinstance(inv, str) and inv:
                    llm_invokes.add(inv)
        elif t == "stream.close" and isinstance(inv, str) and inv in llm_invokes:
            try:
                last_close = int(ev.get("event_seq"))
            except Exception:
                continue
        elif t == "control.interrupt":
            # Treat an explicit interrupt of an LLM invoke as a boundary
            # equivalent to a stream.close for RA1 purposes.
            try:
                payload = json.loads(ev.get("payload_json")) if isinstance(ev.get("payload_json"), str) else (ev.get("payload_json") or {})
            except Exception:
                payload = {}
            old_inv = payload.get("old_invoke_id")
            if isinstance(old_inv, str) and old_inv in llm_invokes:
                try:
                    last_close = int(ev.get("event_seq"))
                except Exception:
                    continue
    return last_close


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
    based on the event log and tool call states.

    RA1 (LLM) uses messages *after* the last stream.close, while RA2/RA3
    operate directly on tool call states so that they can act on tool
    calls whose parent message may have been emitted before the last
    stream.close (e.g. assistant tool calls created by a prior LLM turn
    or user commands that have already finished execution).
    """
    last_close = _last_stream_close_seq(db, thread_id)
    all_states = build_tool_call_states(db, thread_id)

    # -------- RA3: user-originated tool calls (user commands) --------
    user_tcs = [
        tc for tc in all_states.values()
        if tc.parent_role == 'user' and tc.state in ("TC2.1", "TC2.2", "TC5")
    ]
    if user_tcs:
        # Pick earliest parent message and group its runnable tool calls
        user_tcs.sort(key=lambda tc: tc.parent_event_seq)
        msg_id = user_tcs[0].parent_msg_id
        parent_seq = user_tcs[0].parent_event_seq
        tcs_for_msg = [tc for tc in user_tcs if tc.parent_msg_id == msg_id]
        return RunnerActionable(
            kind="RA3_tools_user",
            thread_id=thread_id,
            triggering_event_seq=parent_seq,
            msg_id=msg_id,
            tool_calls=tcs_for_msg,
        )

    # -------- RA2: assistant-originated tool calls --------
    # Consider tool calls whose parent assistant message occurred at or
    # before the last stream.close (i.e. produced by a prior LLM turn).
    assistant_tcs = [
        tc for tc in all_states.values()
        if tc.parent_role == 'assistant'
        and tc.state in ("TC2.1", "TC2.2", "TC5")
        and tc.parent_event_seq <= last_close
    ]
    if assistant_tcs:
        assistant_tcs.sort(key=lambda tc: tc.parent_event_seq)
        msg_id = assistant_tcs[0].parent_msg_id
        parent_seq = assistant_tcs[0].parent_event_seq
        tcs_for_msg = [tc for tc in assistant_tcs if tc.parent_msg_id == msg_id]
        return RunnerActionable(
            kind="RA2_tools_assistant",
            thread_id=thread_id,
            triggering_event_seq=parent_seq,
            msg_id=msg_id,
            tool_calls=tcs_for_msg,
        )

    # -------- RA1: LLM call --------
    # Scan messages after the last stream.close to find a user or tool
    # message that should trigger an LLM turn.
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

        # RA1: LLM call
        # - user messages without tool_calls and without keep_user_turn
        # - tool messages that are not no_api and not keep_user_turn
        if role == "user" and not tool_calls and not keep_user_turn:
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
