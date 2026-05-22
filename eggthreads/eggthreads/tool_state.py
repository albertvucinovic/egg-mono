from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .db import ThreadsDB


AUTO_APPROVED_TOOL_NAMES = {"compact_thread", "answer_user_while_preserving_llm_turn"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
    summary: Optional[str] = None  # latest one-line running status from tool_call.summary
    published: bool = False  # final tool message written
    # Last output_approval payload (if any) for this tool call; allows UI to
    # encode preview/truncation/paths that the runner can later use when
    # publishing the final tool message.
    last_output_approval_payload: Optional[Dict[str, Any]] = None
    owner_invoke_id: Optional[str] = None

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


@dataclass
class _ThreadEventReduction:
    thread_id: str
    max_event_seq: int
    skipped_msg_ids: set[str]
    last_llm_boundary_seq: int
    messages_after_boundary: List[Dict[str, Any]]
    tool_call_states: Dict[str, ToolCallState]
    next_runner_actionable: Optional[RunnerActionable]
    coarse_thread_state_without_lease: str
    _messages_after_records: List[Tuple[Dict[str, Any], Dict[str, Any], int]] = field(default_factory=list)
    _llm_invokes: set[str] = field(default_factory=set)
    _last_llm_stream_boundary_seq: int = -1
    _last_assistant_seq: int = -1


def _payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    try:
        pj = ev.get("payload_json")
        payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_seq_value(ev: Dict[str, Any]) -> int:
    try:
        return int(ev.get("event_seq"))
    except Exception:
        return -1


_REDUCER_CACHE: Dict[Tuple[str, str, int], _ThreadEventReduction] = {}


def _store_reducer_cache(db_path: str, thread_id: str, reduction: _ThreadEventReduction) -> None:
    cache_key = (db_path, thread_id, reduction.max_event_seq)
    _REDUCER_CACHE[cache_key] = reduction

    for key in list(_REDUCER_CACHE.keys()):
        if key[0] == db_path and key[1] == thread_id and key != cache_key:
            del _REDUCER_CACHE[key]


def _latest_cached_reduction_before(
    db_path: str,
    thread_id: str,
    max_seq: int,
) -> Optional[_ThreadEventReduction]:
    latest: Optional[_ThreadEventReduction] = None
    for key, reduction in _REDUCER_CACHE.items():
        if key[0] != db_path or key[1] != thread_id or key[2] >= max_seq:
            continue
        if latest is None or reduction.max_event_seq > latest.max_event_seq:
            latest = reduction
    return latest


def _is_incremental_no_tool_event(ev: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    ev_type = ev.get("type")
    if ev_type == "msg.create":
        # The initial slice deliberately avoids incremental tool-state
        # maintenance. A plain tool result still participates in RA1
        # detection, but a tool message with tool_call_id mutates tool
        # publication state and belongs on the full-rebuild path.
        if payload.get("tool_call_id") is not None:
            return False
        tool_calls = payload.get("tool_calls") or []
        return not (isinstance(tool_calls, list) and tool_calls)
    if ev_type in {"stream.open", "stream.delta", "stream.close"}:
        return True
    if ev_type == "control.interrupt":
        # Continue rewrites the effective LLM boundary relative to an older
        # message and can skip previous messages/tool events.  Keep that on
        # the full-rebuild path for now.
        return payload.get("purpose") != "continue"
    return False


def _has_incremental_safe_tail(records: List[Tuple[Dict[str, Any], Dict[str, Any], int]]) -> bool:
    """Return True when all events can be tail-applied without tool-state mutation."""

    return all(_is_incremental_no_tool_event(ev, payload) for ev, payload, _ev_seq in records)


def _tail_preserves_tool_states(
    records: List[Tuple[Dict[str, Any], Dict[str, Any], int]],
    tool_call_states: Dict[str, ToolCallState],
) -> bool:
    active_tool_invokes = {
        tc.owner_invoke_id for tc in tool_call_states.values()
        if tc.execution_started and tc.finished_reason is None and tc.owner_invoke_id
    }
    if not active_tool_invokes:
        return True
    for ev, payload, _ev_seq in records:
        if ev.get("type") == "stream.close" and ev.get("invoke_id") in active_tool_invokes:
            return False
        if ev.get("type") == "control.interrupt" and payload.get("old_invoke_id") in active_tool_invokes:
            return False
    return True


def _try_reduce_thread_events_incrementally(
    db: ThreadsDB,
    thread_id: str,
    previous: _ThreadEventReduction,
    max_seq: int,
) -> Optional[_ThreadEventReduction]:
    """Apply a small safe incremental reducer slice for safe tail events.

    This handles the hot RA1/LLM bookkeeping path (plain messages and stream
    boundaries) without replaying/parsing the full event log. Existing tool
    states can be preserved unchanged; new tool-state mutations, msg edits,
    continue interrupts, or other hard events stay on the full reducer until
    their incremental semantics are explicit.
    """

    cur = db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? AND event_seq>? AND event_seq<=? ORDER BY event_seq ASC",
        (thread_id, previous.max_event_seq, max_seq),
    )
    events = [dict(row) for row in cur.fetchall()]
    if not events:
        return previous

    records = [(ev, _payload(ev), _event_seq_value(ev)) for ev in events]
    if not _has_incremental_safe_tail(records) or not _tail_preserves_tool_states(records, previous.tool_call_states):
        return None

    skipped_msg_ids = set(previous.skipped_msg_ids)
    llm_invokes = set(previous._llm_invokes)
    last_llm_stream_boundary_seq = previous._last_llm_stream_boundary_seq
    last_assistant_seq = previous._last_assistant_seq
    messages_after_records = list(previous._messages_after_records)
    new_message_records: List[Tuple[Dict[str, Any], Dict[str, Any], int]] = []

    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")
        inv = ev.get("invoke_id")
        if ev_type == "stream.open":
            if payload.get("stream_kind") == "llm" and isinstance(inv, str) and inv:
                llm_invokes.add(inv)
        elif ev_type == "stream.delta":
            if (
                "text" in payload
                or "reason" in payload
                or "reasoning_summary" in payload
                or "tool_call" in payload
            ):
                if isinstance(inv, str) and inv:
                    llm_invokes.add(inv)
        elif ev_type == "stream.close":
            if isinstance(inv, str) and inv in llm_invokes:
                last_llm_stream_boundary_seq = ev_seq
        elif ev_type == "control.interrupt":
            old_inv = payload.get("old_invoke_id")
            purpose = payload.get("purpose")
            if purpose == "llm" or (isinstance(old_inv, str) and old_inv in llm_invokes):
                last_llm_stream_boundary_seq = ev_seq
        elif ev_type == "msg.create":
            msg_id = ev.get("msg_id")
            skipped = msg_id and str(msg_id) in skipped_msg_ids
            if skipped:
                continue
            if payload.get("role") == "assistant" and not bool(payload.get("no_api")):
                last_assistant_seq = ev_seq
            new_message_records.append((ev, payload, ev_seq))

    last_llm_boundary_seq = (
        last_llm_stream_boundary_seq
        if last_llm_stream_boundary_seq != -1
        else last_assistant_seq
    )
    messages_after_records = [
        record for record in messages_after_records
        if record[2] > last_llm_boundary_seq
    ]
    messages_after_records.extend(
        record for record in new_message_records
        if record[2] > last_llm_boundary_seq
    )
    messages_after_boundary = [ev for ev, _payload_obj, _ev_seq in messages_after_records]
    tool_call_states = dict(previous.tool_call_states)
    next_runner_actionable = _next_runner_actionable_from_reduction(
        thread_id,
        tool_call_states,
        messages_after_records,
    )
    if next_runner_actionable is not None:
        coarse_state = "running"
    elif any(tc.state == "TC1" for tc in tool_call_states.values()):
        coarse_state = "waiting_tool_approval"
    elif any(tc.state == "TC4" for tc in tool_call_states.values()):
        coarse_state = "waiting_output_approval"
    else:
        coarse_state = "waiting_user"

    return _ThreadEventReduction(
        thread_id=thread_id,
        max_event_seq=max_seq,
        skipped_msg_ids=skipped_msg_ids,
        last_llm_boundary_seq=last_llm_boundary_seq,
        messages_after_boundary=messages_after_boundary,
        tool_call_states=tool_call_states,
        next_runner_actionable=next_runner_actionable,
        coarse_thread_state_without_lease=coarse_state,
        _messages_after_records=messages_after_records,
        _llm_invokes=llm_invokes,
        _last_llm_stream_boundary_seq=last_llm_stream_boundary_seq,
        _last_assistant_seq=last_assistant_seq,
    )


def _reduce_thread_events(db: ThreadsDB, thread_id: str) -> _ThreadEventReduction:
    """Reduce a thread's event log once into the hot state views.

    This is private and rebuildable: SQLite events remain the source of truth.
    The cache is keyed by database path, thread id, and max event sequence, so
    any appended event naturally invalidates the previous reduction.
    """

    try:
        max_seq = db.max_event_seq(thread_id)
    except Exception:
        max_seq = -1

    db_path = str(db.path)
    cache_key = (db_path, thread_id, max_seq)
    cached = _REDUCER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    previous = _latest_cached_reduction_before(db_path, thread_id, max_seq)
    if previous is not None:
        reduction = _try_reduce_thread_events_incrementally(db, thread_id, previous, max_seq)
        if reduction is not None:
            _store_reducer_cache(db_path, thread_id, reduction)
            return reduction

    events: List[Dict[str, Any]] = []
    if max_seq >= 0:
        cur = db.conn.execute(
            "SELECT * FROM events WHERE thread_id=? AND event_seq<=? ORDER BY event_seq ASC",
            (thread_id, max_seq),
        )
        events = [dict(row) for row in cur.fetchall()]

    reduction = _reduce_loaded_thread_events(thread_id, max_seq, events)
    _store_reducer_cache(db_path, thread_id, reduction)

    return reduction


def _reduce_loaded_thread_events(
    thread_id: str,
    max_event_seq: int,
    events: List[Dict[str, Any]],
) -> _ThreadEventReduction:
    records = [(ev, _payload(ev), _event_seq_value(ev)) for ev in events]

    skipped_msg_ids: set[str] = set()
    msg_seq_by_id: Dict[str, int] = {}
    user_seqs: list[int] = []
    latest_interrupt_seq: Optional[int] = None
    latest_interrupt_payload: Dict[str, Any] = {}

    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")
        if ev_type == "msg.edit":
            msg_id = ev.get("msg_id")
            if msg_id and payload.get("skipped_on_continue"):
                skipped_msg_ids.add(str(msg_id))
            continue
        if ev_type == "control.interrupt":
            latest_interrupt_seq = ev_seq
            latest_interrupt_payload = payload
        elif ev_type == "msg.create":
            msg_id = ev.get("msg_id")
            if msg_id:
                msg_seq_by_id.setdefault(str(msg_id), ev_seq)
            if payload.get("role") == "user":
                user_seqs.append(ev_seq)

    continue_boundary_seq: Optional[int] = None
    continue_interrupt_seq: Optional[int] = None
    if latest_interrupt_seq is not None and latest_interrupt_payload.get("purpose") == "continue":
        continue_interrupt_seq = latest_interrupt_seq
        continue_from_msg_id = latest_interrupt_payload.get("continue_from_msg_id")
        if continue_from_msg_id:
            continue_boundary_seq = msg_seq_by_id.get(str(continue_from_msg_id))

    states: Dict[str, ToolCallState] = {}

    # First fold: messages that declare tool calls. Later messages with the
    # same tool_call id intentionally replace earlier ones, matching the old
    # replay behavior for reused-looking provider ids.
    for ev, payload, ev_seq in records:
        if ev.get("type") != "msg.create":
            continue
        msg_id = ev.get("msg_id") or ""
        if msg_id and str(msg_id) in skipped_msg_ids:
            continue
        role = payload.get("role")
        tcs = payload.get("tool_calls") or []
        if not isinstance(tcs, list) or not tcs:
            continue
        for idx, tc in enumerate(tcs):
            if not isinstance(tc, dict):
                continue
            tcid = tc.get("id") or f"{msg_id}:{idx}"
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
            args = fn.get("arguments") if "function" in tc else tc.get("arguments")
            states[tcid] = ToolCallState(
                thread_id=thread_id,
                tool_call_id=str(tcid),
                parent_msg_id=str(msg_id),
                parent_event_seq=ev_seq,
                parent_role=str(role) if isinstance(role, str) else None,
                index=idx,
                name=str(name),
                arguments=args,
            )

    global_auto_approval = False
    global_intervals: list[tuple[int, Optional[int]]] = []
    current_global_start: Optional[int] = None

    def _should_skip_tc_event(ev_seq: int, tcid: Optional[str]) -> bool:
        if continue_boundary_seq is None or continue_interrupt_seq is None:
            return False
        if ev_seq < continue_boundary_seq or ev_seq > continue_interrupt_seq:
            return False
        if tcid and tcid in states:
            tc = states[tcid]
            if tc.parent_event_seq > continue_boundary_seq:
                return True
        return False

    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")

        if ev_type == "tool_call.approval":
            decision = payload.get("decision")
            if decision == "global_approval":
                global_auto_approval = True
                current_global_start = ev_seq
                continue
            if decision == "revoke_global_approval":
                if global_auto_approval and current_global_start is not None:
                    global_intervals.append((current_global_start, ev_seq))
                global_auto_approval = False
                current_global_start = None
                continue

            if decision == "all-in-turn":
                prev_user_seq = -1
                next_user_seq = None
                for user_seq in user_seqs:
                    if user_seq <= ev_seq:
                        prev_user_seq = user_seq
                    elif next_user_seq is None:
                        next_user_seq = user_seq
                        break
                for tc in states.values():
                    if tc.approval_decision is not None:
                        continue
                    if tc.parent_event_seq < prev_user_seq:
                        continue
                    if next_user_seq is not None and tc.parent_event_seq >= next_user_seq:
                        continue
                    tc.approval_decision = "granted"
            else:
                tcid = payload.get("tool_call_id")
                if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                    if isinstance(decision, str):
                        states[tcid].approval_decision = decision
        elif ev_type == "tool_call.execution_started":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    tc.execution_started = True
                    inv = ev.get("invoke_id")
                    if isinstance(inv, str) and inv:
                        tc.owner_invoke_id = inv
        elif ev_type == "tool_call.summary":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    summary = payload.get("summary")
                    if isinstance(summary, str):
                        tc.summary = summary
        elif ev_type == "tool_call.finished":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    reason = payload.get("reason")
                    if isinstance(reason, str):
                        tc.finished_reason = reason
                    out = payload.get("output")
                    if out is not None:
                        tc.finished_output = str(out)
        elif ev_type == "tool_call.output_approval":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    decision = payload.get("decision")
                    if isinstance(decision, str):
                        tc.output_decision = decision
                    tc.last_output_approval_payload = payload
        elif ev_type == "msg.create":
            if payload.get("role") != "tool":
                continue
            tcid = payload.get("tool_call_id")
            msg_id = ev.get("msg_id")
            if msg_id and str(msg_id) in skipped_msg_ids:
                continue
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    tc.published = True

    if global_auto_approval and current_global_start is not None:
        global_intervals.append((current_global_start, None))

    closed_invokes: set[str] = set()
    interrupted_invokes: set[str] = set()
    for ev, _payload_obj, _ev_seq in records:
        if ev.get("type") != "stream.close":
            continue
        inv = ev.get("invoke_id")
        if isinstance(inv, str) and inv:
            closed_invokes.add(inv)
    for ev, payload, _ev_seq in records:
        if ev.get("type") != "control.interrupt":
            continue
        old_inv = payload.get("old_invoke_id")
        if isinstance(old_inv, str) and old_inv:
            interrupted_invokes.add(old_inv)

    def _has_global_approval(ev_seq: int) -> bool:
        for start, end in global_intervals:
            if ev_seq < start:
                continue
            if end is not None and ev_seq > end:
                continue
            return True
        return False

    for tc in states.values():
        if tc.finished_reason is None and tc.output_decision is not None:
            tc.finished_reason = "interrupted"
            tc.finished_output = str(
                (tc.last_output_approval_payload or {}).get("preview")
                or "--- INTERRUPTED ---\nTool output was decided before the tool reported a result."
            )
        if tc.execution_started and tc.finished_reason is None and tc.owner_invoke_id in interrupted_invokes:
            tc.finished_reason = "interrupted"
            tc.finished_output = (
                "--- INTERRUPTED ---\n"
                "Tool execution was interrupted before the tool reported a result."
            )
            tc.output_decision = "whole"
            tc.last_output_approval_payload = {
                "tool_call_id": tc.tool_call_id,
                "decision": "whole",
                "reason": "Tool execution was interrupted before the tool reported a result.",
                "preview": tc.finished_output,
            }
        if tc.execution_started and tc.finished_reason is None and tc.owner_invoke_id in closed_invokes:
            tc.finished_reason = "interrupted"
            tc.finished_output = (
                "--- INTERRUPTED ---\n"
                "Tool execution stream closed before the tool reported a result."
            )
            tc.output_decision = "whole"
            tc.last_output_approval_payload = {
                "tool_call_id": tc.tool_call_id,
                "decision": "whole",
                "reason": "Tool execution stream closed before the tool reported a result.",
                "preview": tc.finished_output,
            }
        if tc.approval_decision is None and _has_global_approval(tc.parent_event_seq):
            tc.approval_decision = "granted"
        if tc.approval_decision is None and tc.name in AUTO_APPROVED_TOOL_NAMES:
            tc.approval_decision = "granted"

    (
        last_llm_boundary_seq,
        llm_invokes,
        last_llm_stream_boundary_seq,
        last_assistant_seq,
    ) = _llm_boundary_details_from_records(records, skipped_msg_ids, msg_seq_by_id)
    messages_after_records = [
        (ev, payload, ev_seq)
        for ev, payload, ev_seq in records
        if ev.get("type") == "msg.create"
        and ev_seq > last_llm_boundary_seq
        and not (ev.get("msg_id") and str(ev.get("msg_id")) in skipped_msg_ids)
    ]
    messages_after_boundary = [ev for ev, _payload_obj, _ev_seq in messages_after_records]
    next_runner_actionable = _next_runner_actionable_from_reduction(
        thread_id,
        states,
        messages_after_records,
    )
    if next_runner_actionable is not None:
        coarse_state = "running"
    elif any(tc.state == "TC1" for tc in states.values()):
        coarse_state = "waiting_tool_approval"
    elif any(tc.state == "TC4" for tc in states.values()):
        coarse_state = "waiting_output_approval"
    else:
        coarse_state = "waiting_user"

    return _ThreadEventReduction(
        thread_id=thread_id,
        max_event_seq=max_event_seq,
        skipped_msg_ids=skipped_msg_ids,
        last_llm_boundary_seq=last_llm_boundary_seq,
        messages_after_boundary=messages_after_boundary,
        tool_call_states=states,
        next_runner_actionable=next_runner_actionable,
        coarse_thread_state_without_lease=coarse_state,
        _messages_after_records=messages_after_records,
        _llm_invokes=llm_invokes,
        _last_llm_stream_boundary_seq=last_llm_stream_boundary_seq,
        _last_assistant_seq=last_assistant_seq,
    )


def _llm_boundary_details_from_records(
    records: List[Tuple[Dict[str, Any], Dict[str, Any], int]],
    skipped_msg_ids: set[str],
    msg_seq_by_id: Dict[str, int],
) -> Tuple[int, set[str], int, int]:
    last_close = -1
    llm_invokes: set[str] = set()
    last_assistant_seq = -1
    last_llm_stream_boundary_seq = -1

    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")
        inv = ev.get("invoke_id")
        if ev_type == "stream.open":
            if payload.get("stream_kind") == "llm" and isinstance(inv, str) and inv:
                llm_invokes.add(inv)
        elif ev_type == "stream.delta":
            if (
                "text" in payload
                or "reason" in payload
                or "reasoning_summary" in payload
                or "tool_call" in payload
            ):
                if isinstance(inv, str) and inv:
                    llm_invokes.add(inv)
        elif ev_type == "stream.close" and isinstance(inv, str) and inv in llm_invokes:
            last_close = ev_seq
            last_llm_stream_boundary_seq = ev_seq
        elif ev_type == "control.interrupt":
            old_inv = payload.get("old_invoke_id")
            purpose = payload.get("purpose")
            if purpose == "llm":
                last_close = ev_seq
                last_llm_stream_boundary_seq = ev_seq
                continue
            if purpose == "continue":
                continue_from_msg_id = payload.get("continue_from_msg_id")
                if continue_from_msg_id:
                    msg_seq = msg_seq_by_id.get(str(continue_from_msg_id))
                    if msg_seq is not None:
                        last_close = msg_seq - 1
                        continue
                last_close = ev_seq
                continue
            if isinstance(old_inv, str) and old_inv in llm_invokes:
                last_close = ev_seq
                last_llm_stream_boundary_seq = ev_seq
        elif ev_type == "msg.create":
            msg_id = ev.get("msg_id")
            skipped = msg_id and str(msg_id) in skipped_msg_ids
            if payload.get("role") == "assistant" and not bool(payload.get("no_api")) and not skipped:
                last_assistant_seq = ev_seq

    if last_close == -1 and last_assistant_seq != -1:
        last_close = last_assistant_seq
    return last_close, llm_invokes, last_llm_stream_boundary_seq, last_assistant_seq


def _last_llm_boundary_from_records(
    records: List[Tuple[Dict[str, Any], Dict[str, Any], int]],
    skipped_msg_ids: set[str],
    msg_seq_by_id: Dict[str, int],
) -> int:
    last_close, _llm_invokes, _last_llm_stream_boundary_seq, _last_assistant_seq = _llm_boundary_details_from_records(
        records,
        skipped_msg_ids,
        msg_seq_by_id,
    )
    return last_close


def _next_runner_actionable_from_reduction(
    thread_id: str,
    states: Dict[str, ToolCallState],
    messages_after_records: List[Tuple[Dict[str, Any], Dict[str, Any], int]],
) -> Optional[RunnerActionable]:
    user_tcs = [
        tc for tc in states.values()
        if tc.parent_role == "user" and tc.state in ("TC2.1", "TC2.2", "TC5")
    ]
    if user_tcs:
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

    assistant_tcs = [
        tc for tc in states.values()
        if tc.parent_role == "assistant" and tc.state in ("TC2.1", "TC2.2", "TC5")
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

    has_unpublished_assistant_tool = any(
        tc.parent_role == "assistant" and not tc.published
        for tc in states.values()
    )
    if has_unpublished_assistant_tool:
        return None

    for ev, payload, ev_seq in messages_after_records:
        msg_id = ev.get("msg_id") or ""
        role = payload.get("role")
        keep_user_turn = bool(payload.get("keep_user_turn"))
        no_api = bool(payload.get("no_api"))
        tool_calls = payload.get("tool_calls") or []

        if role == "user" and not tool_calls and not keep_user_turn and not no_api:
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


def _iter_events(db: ThreadsDB, thread_id: str) -> Iterable[Dict[str, Any]]:
    cur = db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (thread_id,),
    )
    for row in cur.fetchall():
        yield dict(row)


def build_tool_call_states(db: ThreadsDB, thread_id: str) -> Dict[str, ToolCallState]:
    """Return reconstructed ToolCallState objects for a thread.

    The cached private reducer owns the event-log fold.  Public callers get
    copies so accidental mutation of returned ``ToolCallState`` objects cannot
    contaminate the process-local reducer cache.
    """

    states = _reduce_thread_events(db, thread_id).tool_call_states
    return {tcid: deepcopy(tc) for tcid, tc in states.items()}

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
      - a stream.close whose invoke_id saw LLM-style deltas (``text``,
        ``reason``, or display-only ``reasoning_summary`` fields), or
      - a control.interrupt whose payload.old_invoke_id matches such an
        invoke_id.

    We intentionally ignore stream.close events that belong purely to
    tool-execution streams (RA2/RA3). Those streams only emit deltas
    with a ``tool`` field (and no top-level ``text``/``reason`` /
    ``reasoning_summary`` / ``tool_call``), whereas LLM streams (RA1)
    emit text, durable reasoning, display-only summaries, and/or tool-call
    argument deltas.

    This distinction matters because RA1 (LLM turns) should be driven by
    user/tool messages that appear *after* the last LLM turn finishes or
    is explicitly interrupted by the user, but we do not want
    tool-execution streams to reset that boundary.

    Note: This function also respects the skipped_on_continue flag when
    finding the last assistant message as a fallback boundary.
    """
    return _reduce_thread_events(db, thread_id).last_llm_boundary_seq


def _last_stream_close_seq_uncached(db: ThreadsDB, thread_id: str) -> int:
    """Original full-scan implementation kept for focused equivalence tests."""

    last_close = -1
    llm_invokes: set[str] = set()
    # Fallback for threads that have assistant messages but no
    # associated LLM streams (e.g. imported transcripts, sync-only
    # completions, or duplicated threads that copied msg.create events
    # but not stream.*). In that case, we treat the last assistant
    # message as the effective RA1 boundary so that RA1 does not
    # re-trigger on already-answered user messages.
    last_assistant_seq = -1

    # First, collect msg_ids that have been marked as skipped
    skipped_msg_ids: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit' ORDER BY event_seq ASC",
        (thread_id,),
    )
    for row in cur_edit.fetchall():
        msg_id = row[0]
        try:
            payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            payload = {}
        if payload.get('skipped_on_continue'):
            skipped_msg_ids.add(msg_id)

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
        if t == "stream.open":
            # Newer runners tag stream.open with stream_kind so we can
            # identify an LLM invoke even if it was interrupted before
            # the first delta.
            try:
                payload = json.loads(ev.get("payload_json")) if isinstance(ev.get("payload_json"), str) else (ev.get("payload_json") or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload.get('stream_kind') == 'llm':
                if isinstance(inv, str) and inv:
                    llm_invokes.add(inv)

        elif t == "stream.delta":
            try:
                payload = json.loads(ev.get("payload_json")) if isinstance(ev.get("payload_json"), str) else (ev.get("payload_json") or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and (
                "text" in payload
                or "reason" in payload
                or "reasoning_summary" in payload
                or "tool_call" in payload
            ):
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
            #
            # Note: Ctrl+C may happen *before* a runner acquires a lease
            # and emits any stream.open/delta events ("pending RA1"). In
            # that case, old_invoke_id may be None. If the payload
            # explicitly marks purpose=='llm', we still advance the
            # boundary so the same triggering user message does not
            # re-trigger RA1 immediately.
            try:
                payload = json.loads(ev.get("payload_json")) if isinstance(ev.get("payload_json"), str) else (ev.get("payload_json") or {})
            except Exception:
                payload = {}
            old_inv = payload.get("old_invoke_id")
            purpose = payload.get('purpose')

            # If the interrupt is explicitly for an LLM step, treat it as a boundary.
            if purpose == 'llm':
                try:
                    last_close = int(ev.get("event_seq"))
                except Exception:
                    continue
                continue

            # For continue interrupts, set boundary to BEFORE the continue point
            # so the continue_from message becomes visible to RA1 detection.
            if purpose == 'continue':
                continue_from_msg_id = payload.get('continue_from_msg_id')
                if continue_from_msg_id:
                    # Look up the event_seq for the continue_from message
                    msg_cur = db.conn.execute(
                        "SELECT event_seq FROM events WHERE thread_id=? AND type='msg.create' AND msg_id=?",
                        (thread_id, continue_from_msg_id),
                    )
                    msg_row = msg_cur.fetchone()
                    if msg_row:
                        # Set boundary to one BEFORE the continue point
                        last_close = int(msg_row[0]) - 1
                        continue
                # Fallback: use interrupt seq if msg not found
                try:
                    last_close = int(ev.get("event_seq"))
                except Exception:
                    continue
                continue

            # Otherwise, only treat it as a boundary when it refers to a
            # known LLM invoke_id.
            if isinstance(old_inv, str) and old_inv in llm_invokes:
                try:
                    last_close = int(ev.get("event_seq"))
                except Exception:
                    continue
        elif t == "msg.create":
            # Track the last assistant message as a potential fallback
            # RA1 boundary when no LLM streams are present.
            try:
                pj = ev.get("payload_json")
                payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
            except Exception:
                payload = {}
            role = payload.get("role")
            no_api = bool(payload.get("no_api"))
            msg_id = ev.get("msg_id")
            # Skip messages that have been marked as skipped_on_continue
            skipped = msg_id and msg_id in skipped_msg_ids
            if role == "assistant" and not no_api and not skipped:
                try:
                    last_assistant_seq = int(ev.get("event_seq"))
                except Exception:
                    continue
    # If we never observed an LLM stream boundary but we did see an
    # assistant message, treat the last assistant msg.create as the
    # effective RA1 boundary. This prevents RA1 from re-triggering on
    # historical user/tool messages in threads that were populated
    # without streaming metadata (e.g. duplicates, imports).
    if last_close == -1 and last_assistant_seq != -1:
        last_close = last_assistant_seq
    return last_close


def _iter_messages_after(db: ThreadsDB, thread_id: str, after_seq: int) -> Iterable[Dict[str, Any]]:
    """Iterate over msg.create events after a given event_seq.

    This function respects the skipped_on_continue flag: messages that have
    been marked as skipped (via a msg.edit event) are not yielded. This allows
    continue_thread to effectively "reset" the conversation to an earlier point.
    """
    # First, collect msg_ids that have been marked as skipped
    skipped_msg_ids: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit' ORDER BY event_seq ASC",
        (thread_id,),
    )
    for row in cur_edit.fetchall():
        msg_id = row[0]
        try:
            payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            payload = {}
        if payload.get('skipped_on_continue'):
            skipped_msg_ids.add(msg_id)

    cur = db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? AND type='msg.create' AND event_seq>? ORDER BY event_seq ASC",
        (thread_id, after_seq),
    )
    for row in cur.fetchall():
        ev = dict(row)
        msg_id = ev.get("msg_id")
        # Skip messages that have been marked as skipped_on_continue
        if msg_id and msg_id in skipped_msg_ids:
            continue
        yield ev


def discover_runner_actionable_cached(db: ThreadsDB, thread_id: str) -> Optional[RunnerActionable]:
    """Return the next actionable item using the cached thread reducer."""

    return _reduce_thread_events(db, thread_id).next_runner_actionable


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
        # Consider assistant tool calls that have already been approved,                                                                         │ │ │
        # denied, or are ready to be published. RA2 operates purely on tool                                                                      │ │ │
        # call state; we do not need to gate by last LLM boundary here                                                                           │ │ │
        # because the per-thread lease (open_streams) ensures that RA2 does                                                                      │ │ │
        # not run concurrently with an active RA1 stream.
        #and tc.parent_event_seq <= last_close
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

    # Before we consider a new LLM turn (RA1), ensure there are no
    # assistant-originated tool calls that are still unresolved from the
    # provider's point of view. The OpenAI tools protocol requires that
    # every assistant tool_call has a corresponding tool message before
    # the next assistant call.
    has_unpublished_assistant_tool = any(
        tc.parent_role == 'assistant' and not tc.published
        for tc in all_states.values()
    )
    if has_unpublished_assistant_tool:
        return None

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
        #   and not marked no_api (no_api user messages are metadata and
        #   must not trigger an LLM turn)
        # - tool messages that are not no_api and not keep_user_turn
        if role == "user" and not tool_calls and not keep_user_turn and not no_api:
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
    if th is None:
        return "unknown"
    if th and th.status == "paused":
        return "paused"

    # Active stream -> running
    try:
        row = db.current_open(thread_id)
    except Exception:
        row = None
    if row is not None:
        try:
            if str(row["lease_until"] or "") <= _utcnow_iso():
                db.release(thread_id, str(row["invoke_id"]))
            else:
                return "running"
        except Exception:
            return "running"

    return _reduce_thread_events(db, thread_id).coarse_thread_state_without_lease
