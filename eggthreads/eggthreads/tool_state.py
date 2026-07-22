from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .db import ThreadsDB


GET_USER_MESSAGE_TOOL_NAME = "get_user_message_while_preserving_llm_turn"

AUTO_APPROVED_TOOL_NAMES = {
    "compact_thread",
    "answer_user_while_preserving_llm_turn",
    GET_USER_MESSAGE_TOOL_NAME,
}


def _is_consumed_get_user_message_edit(payload: Dict[str, Any]) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("consumed_by_tool_name") == GET_USER_MESSAGE_TOOL_NAME
        and payload.get("consumed_by_tool_call_id")
        and payload.get("no_api")
        and payload.get("keep_user_turn")
    )


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
    # Storage facts remain durable across the TC4 publication recovery window.
    # Historical finish events default to their stored output size/uncapped.
    finished_original_char_count: Optional[int] = None
    finished_output_capped: bool = False
    # Normalized presentation stays separate from canonical finished output.
    publication_presentation: Dict[str, Any] = field(default_factory=dict)
    force_provider_output_masking: bool = False
    transcript_content_tool_name: Optional[str] = None
    output_decision: Optional[str] = None  # "whole" | "partial" | "omit"
    summary: Optional[str] = None  # latest one-line running status from tool_call.summary
    published: bool = False  # final tool message written
    # Last output_approval payload (if any) for this tool call; allows UI to
    # encode preview/truncation/paths that the runner can later use when
    # publishing the final tool message.
    last_output_approval_payload: Optional[Dict[str, Any]] = None
    owner_invoke_id: Optional[str] = None
    # Preserve-turn wait metadata is folded into the canonical lifecycle
    # reduction. Hot reply/start boundaries can therefore inspect only the
    # unresolved candidates instead of rescanning every historical message.
    parent_skipped_on_continue: bool = False
    waiting_note_msg_id: Optional[str] = None
    waiting_note_event_seq: Optional[int] = None
    waiting_note_ts: Optional[str] = None
    waiting_note_content: Optional[str] = None
    waiting_note_skipped_on_continue: bool = False
    claimed_user_msg_id: Optional[str] = None
    claimed_user_event_seq: Optional[int] = None
    claimed_user_content: Optional[str] = None
    result_msg_id: Optional[str] = None
    result_skipped_on_continue: bool = False
    execution_started_ts: Optional[str] = None
    execution_timeout_sec: Optional[float] = None
    execution_timeout_deadline: Optional[str] = None
    execution_resumes_after_lease_loss: bool = False
    finished_event_seq: Optional[int] = None
    # Event watermark of the latest lifecycle event applied to this call, and
    # the event that currently owns the authoritative output decision.
    state_event_seq: int = -1
    output_decision_event_seq: Optional[int] = None

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
      - "RA1_llm"             -> call LLM (assistant turn)
      - "RA2_tools_assistant" -> process assistant-originated tool calls
      - "RA3_tools_user"      -> process user-originated tool calls (user commands)

    ``recovery_mode`` describes why otherwise non-runnable tool-call states are
    actionable. It is deliberately separate from ``kind`` so recovery keeps the
    assistant/user publication semantics while allowing either per-tool
    execution recovery or publication of already-durable successful output.
    """

    kind: str
    thread_id: str
    triggering_event_seq: int
    msg_id: Optional[str] = None
    tool_calls: Optional[List[ToolCallState]] = None
    recovery_mode: Optional[str] = None


@dataclass
class _ThreadEventReduction:
    thread_id: str
    max_event_seq: int
    skipped_msg_ids: set[str]
    consumed_user_msg_ids: set[str]
    last_llm_boundary_seq: int
    messages_after_boundary: List[Dict[str, Any]]
    tool_call_states: Dict[str, ToolCallState]
    next_runner_actionable: Optional[RunnerActionable]
    coarse_thread_state_without_lease: str
    get_user_wait_tool_call_ids: Tuple[str, ...] = ()
    _messages_after_records: List[Tuple[Dict[str, Any], Dict[str, Any], int]] = field(default_factory=list)
    _llm_invokes: set[str] = field(default_factory=set)
    _last_llm_stream_boundary_seq: int = -1
    _last_assistant_seq: int = -1
    _user_seqs: List[int] = field(default_factory=list)
    _current_global_start: Optional[int] = None
    _open_all_in_turn_user_seq: Optional[int] = None
    _open_all_in_turn_approval_seq: Optional[int] = None


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


def _prune_reducer_cache_for_threads(db_path: str, thread_ids: Iterable[str]) -> None:
    thread_id_set = {str(tid) for tid in thread_ids}
    if not thread_id_set:
        return
    for key in list(_REDUCER_CACHE.keys()):
        if key[0] == db_path and key[1] in thread_id_set:
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


def _is_incremental_safe_tail_event(ev: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    ev_type = ev.get("type")
    if ev_type == "msg.edit":
        return _is_consumed_get_user_message_edit(payload)
    if ev_type == "msg.delete":
        return False
    if ev_type == "msg.create":
        raw_tool_calls = payload.get("tool_calls")
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            if payload.get("tool_call_id") is not None:
                return False
            return payload.get("role") in {"assistant", "user"}
        if raw_tool_calls:
            return False
        if payload.get("tool_call_id") is not None:
            return bool(
                payload.get("role") == "tool"
                or (
                    payload.get("role") == "assistant"
                    and payload.get("answer_user_preserve_turn")
                    and payload.get("source_tool_name") == GET_USER_MESSAGE_TOOL_NAME
                    and payload.get("awaiting_user_message_tool_call_id")
                )
            )
        return True
    if ev_type in {"stream.open", "stream.delta", "stream.close", "provider_request.started"}:
        return True
    if ev_type == "control.interrupt":
        # Continue rewrites the effective LLM boundary relative to an older
        # message and can skip previous messages/tool events.  Keep that on
        # the full-rebuild path for now.
        return payload.get("purpose") != "continue"
    if ev_type == "tool_call.summary":
        return isinstance(payload.get("tool_call_id"), str)
    if ev_type in {
        "tool_call.approval",
        "tool_call.execution_started",
        "tool_call.finished",
        "tool_call.output_approval",
    }:
        decision = payload.get("decision") if ev_type == "tool_call.approval" else None
        if decision in {"all-in-turn", "global_approval", "revoke_global_approval"}:
            return True
        if not isinstance(payload.get("tool_call_id"), str):
            return False
        return True
    return False


def _tool_call_states_from_declaration(
    thread_id: str,
    ev: Dict[str, Any],
    payload: Dict[str, Any],
    ev_seq: int,
    *,
    strict: bool = False,
) -> Optional[Dict[Any, ToolCallState]]:
    msg_id = ev.get("msg_id") or ""
    role = payload.get("role")
    tcs = payload.get("tool_calls") or []
    if not isinstance(tcs, list) or not tcs:
        return {}
    out: Dict[Any, ToolCallState] = {}
    for idx, tc in enumerate(tcs):
        if not isinstance(tc, dict):
            if strict:
                return None
            continue
        explicit_tcid = tc.get("id")
        if strict and explicit_tcid is not None and not isinstance(explicit_tcid, str):
            return None
        tcid = explicit_tcid or f"{msg_id}:{idx}"
        if strict and tcid in out:
            return None
        fn = tc.get("function") or {}
        if strict and not isinstance(fn, dict):
            return None
        name = fn.get("name") or tc.get("name") or ""
        args = fn.get("arguments") if "function" in tc else tc.get("arguments")
        out[tcid] = ToolCallState(
            thread_id=thread_id,
            tool_call_id=str(tcid),
            parent_msg_id=str(msg_id),
            parent_event_seq=ev_seq,
            parent_role=str(role) if isinstance(role, str) else None,
            index=idx,
            name=str(name),
            arguments=args,
            state_event_seq=ev_seq,
        )
        if (
            (role == "user" and not bool(payload.get("requires_tool_approval")))
            or out[tcid].name in AUTO_APPROVED_TOOL_NAMES
        ):
            out[tcid] = replace(out[tcid], approval_decision="granted")
    return out


def _latest_user_seq_at_or_before(user_seqs: List[int], ev_seq: int) -> int:
    """Return the user-turn start sequence for an approval event.

    ``all-in-turn`` historically applies to the turn containing the approval:
    the latest user message at or before the approval event, through the event
    before the next user message.  If malformed/imported history contains an
    approval before any user message, full replay treats the pre-user prefix as
    a turn starting at ``-1``; preserve that behavior for incremental state.
    """

    prev_user_seq = -1
    for user_seq in user_seqs:
        if user_seq <= ev_seq:
            prev_user_seq = user_seq
        else:
            break
    return prev_user_seq


def _all_in_turn_open_for_event(
    open_user_seq: Optional[int],
    approval_seq: Optional[int],
    ev_seq: int,
) -> bool:
    return open_user_seq is not None and approval_seq is not None and ev_seq >= approval_seq


def _tool_call_ids_in_turn(
    states: Dict[Any, ToolCallState],
    user_seqs: List[int],
    ev_seq: int,
) -> List[Tuple[Any, ToolCallState]]:
    prev_user_seq = -1
    next_user_seq = None
    for user_seq in user_seqs:
        if user_seq <= ev_seq:
            prev_user_seq = user_seq
        elif next_user_seq is None:
            next_user_seq = user_seq
            break
    out: List[Tuple[Any, ToolCallState]] = []
    for tcid, tc in states.items():
        if tc.parent_event_seq < prev_user_seq:
            continue
        if next_user_seq is not None and tc.parent_event_seq >= next_user_seq:
            continue
        out.append((tcid, tc))
    return out


def _has_prior_tool_approval_for_ids(
    db: ThreadsDB,
    thread_id: str,
    max_seq: int,
    tool_call_ids: Iterable[Any],
) -> bool:
    wanted = set(tool_call_ids)
    if not wanted:
        return False
    cur = db.conn.execute(
        """
        SELECT payload_json FROM events
         WHERE thread_id=?
           AND type='tool_call.approval'
           AND event_seq<=?
        """,
        (thread_id, max_seq),
    )
    for row in cur.fetchall():
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("tool_call_id") in wanted:
            return True
    return False


def _has_incremental_safe_tail(records: List[Tuple[Dict[str, Any], Dict[str, Any], int]]) -> bool:
    """Return True when all events can be tail-applied by the cached reducer."""

    return all(_is_incremental_safe_tail_event(ev, payload) for ev, payload, _ev_seq in records)


def _interrupted_tool_update(tc: ToolCallState, reason: str, output: str) -> ToolCallState:
    # Interrupt/close reconstructs the missing finish evidence, but output
    # publication remains TC4 until finalize_tool_output commits a decision.
    # This keeps one durable authority for every TC4 -> TC5 transition.
    return replace(
        tc,
        finished_reason="interrupted",
        finished_output=output,
        finished_original_char_count=len(output),
        finished_output_capped=False,
        force_provider_output_masking=False,
        transcript_content_tool_name=None,
        output_decision=None,
        last_output_approval_payload=None,
        output_decision_event_seq=None,
    )


def _try_reduce_thread_events_incrementally(
    db: ThreadsDB,
    thread_id: str,
    previous: _ThreadEventReduction,
    max_seq: int,
) -> Optional[_ThreadEventReduction]:
    """Apply a small safe incremental reducer slice for safe tail events.

    This handles the hot RA1/LLM bookkeeping path (plain messages and stream
    boundaries), resolvable tool lifecycle tails, resolvable tool result
    publication, and well-formed tool-call declarations without replaying/parsing
    the full event log. Unresolved ids, malformed declarations, msg edits,
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
    if not _has_incremental_safe_tail(records):
        return None

    skipped_msg_ids = set(previous.skipped_msg_ids)
    consumed_user_msg_ids = set(previous.consumed_user_msg_ids)
    llm_invokes = set(previous._llm_invokes)
    user_seqs = list(previous._user_seqs)
    current_global_start = previous._current_global_start
    open_all_in_turn_user_seq = previous._open_all_in_turn_user_seq
    open_all_in_turn_approval_seq = previous._open_all_in_turn_approval_seq
    last_llm_boundary_seq = previous.last_llm_boundary_seq
    last_llm_stream_boundary_seq = previous._last_llm_stream_boundary_seq
    last_assistant_seq = previous._last_assistant_seq
    assistant_fallback_boundary = (
        previous._last_llm_stream_boundary_seq == -1
        and previous.last_llm_boundary_seq == previous._last_assistant_seq
    )
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
                last_llm_boundary_seq = ev_seq
                assistant_fallback_boundary = False
        elif ev_type == "control.interrupt":
            old_inv = payload.get("old_invoke_id")
            purpose = payload.get("purpose")
            if purpose == "llm" or (isinstance(old_inv, str) and old_inv in llm_invokes):
                last_llm_stream_boundary_seq = ev_seq
                last_llm_boundary_seq = ev_seq
                assistant_fallback_boundary = False
        elif ev_type == "msg.create":
            msg_id = ev.get("msg_id")
            skipped = msg_id and str(msg_id) in skipped_msg_ids
            if skipped:
                continue
            if payload.get("tool_call_id") is not None and payload.get("role") != "tool":
                return None
            raw_tool_calls = payload.get("tool_calls")
            if raw_tool_calls and payload.get("role") not in {"assistant", "user"}:
                return None
            if payload.get("role") == "user":
                user_seqs.append(ev_seq)
            if payload.get("role") == "assistant" and not bool(payload.get("no_api")):
                last_assistant_seq = ev_seq
                if last_llm_boundary_seq == -1 or assistant_fallback_boundary:
                    last_llm_boundary_seq = ev_seq
                    assistant_fallback_boundary = True
            new_message_records.append((ev, payload, ev_seq))

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
    get_user_wait_ids = set(previous.get_user_wait_tool_call_ids)
    touched_get_user_ids: set[str] = set()
    closed_invokes: set[str] = set()
    interrupted_invokes: set[str] = set()
    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")
        if ev_type == "msg.edit" and _is_consumed_get_user_message_edit(payload):
            tcid = str(payload.get("consumed_by_tool_call_id") or "")
            tc = tool_call_states.get(tcid)
            if tc is None or tc.name != GET_USER_MESSAGE_TOOL_NAME:
                return None
            msg_id = str(ev.get("msg_id") or "")
            try:
                reply_seq = int(payload.get("consumed_user_event_seq"))
            except (TypeError, ValueError):
                row = db.conn.execute(
                    "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? "
                    "AND type='msg.create' ORDER BY event_seq ASC LIMIT 1",
                    (thread_id, msg_id),
                ).fetchone()
                reply_seq = int(row[0]) if row is not None else -1
            consumed_user_msg_ids.add(msg_id)
            tool_call_states[tcid] = replace(
                tc,
                claimed_user_msg_id=msg_id or tc.claimed_user_msg_id,
                claimed_user_event_seq=reply_seq if reply_seq >= 0 else tc.claimed_user_event_seq,
                claimed_user_content=str(payload.get("content") or ""),
            )
            touched_get_user_ids.add(tcid)
            continue
        if ev_type == "control.interrupt":
            old_inv = payload.get("old_invoke_id")
            if isinstance(old_inv, str) and old_inv:
                interrupted_invokes.add(old_inv)
            continue
        if ev_type == "stream.close":
            inv = ev.get("invoke_id")
            if isinstance(inv, str) and inv:
                closed_invokes.add(inv)
            continue
        if ev_type == "msg.create":
            if (
                payload.get("role") == "user"
                and open_all_in_turn_user_seq is not None
                and ev_seq > open_all_in_turn_user_seq
            ):
                # ``all-in-turn`` approval is scoped to one user turn.  A new
                # user message starts a new turn, so old approvals must not
                # leak onto user-command tool calls such as ``$`` / ``$$``.
                open_all_in_turn_user_seq = None
                open_all_in_turn_approval_seq = None

            raw_tool_calls = payload.get("tool_calls")
            if isinstance(raw_tool_calls, list) and raw_tool_calls:
                msg_id = ev.get("msg_id")
                if msg_id and str(msg_id) in skipped_msg_ids:
                    continue
                declared = _tool_call_states_from_declaration(thread_id, ev, payload, ev_seq, strict=True)
                if declared is None:
                    return None
                if any(tcid in tool_call_states for tcid in declared):
                    return None
                if _has_prior_tool_approval_for_ids(db, thread_id, ev_seq, declared):
                    return None
                if current_global_start is not None or _all_in_turn_open_for_event(
                    open_all_in_turn_user_seq,
                    open_all_in_turn_approval_seq,
                    ev_seq,
                ):
                    declared = {
                        tcid: replace(tc, approval_decision="granted")
                        for tcid, tc in declared.items()
                    }
                tool_call_states.update(declared)
                continue
        tcid = payload.get("tool_call_id")
        decision = payload.get("decision") if ev_type == "tool_call.approval" else None
        broad_approval = decision in {"all-in-turn", "global_approval", "revoke_global_approval"}
        if tcid not in tool_call_states:
            if broad_approval:
                tc = None
            elif ev_type == "msg.create" and payload.get("tool_call_id") is not None:
                return None
            elif ev_type == "tool_call.summary":
                return None
            elif ev_type in {
                "tool_call.approval",
                "tool_call.execution_started",
                "tool_call.finished",
                "tool_call.output_approval",
            }:
                return None
            else:
                continue
        else:
            tc = tool_call_states[tcid]
            if ev_seq <= tc.parent_event_seq:
                if ev_type in {
                    "msg.create",
                    "tool_call.approval",
                    "tool_call.execution_started",
                    "tool_call.summary",
                    "tool_call.finished",
                    "tool_call.output_approval",
                }:
                    return None
                continue
        if ev_type == "tool_call.summary":
            summary = payload.get("summary")
            if isinstance(summary, str):
                tool_call_states[tcid] = replace(tc, summary=summary, state_event_seq=ev_seq)
        elif ev_type == "msg.create":
            if (
                payload.get("role") == "assistant"
                and payload.get("answer_user_preserve_turn")
                and payload.get("source_tool_name") == GET_USER_MESSAGE_TOOL_NAME
            ):
                note_tcid = str(payload.get("awaiting_user_message_tool_call_id") or "")
                note_tc = tool_call_states.get(note_tcid)
                if note_tc is None or note_tc.name != GET_USER_MESSAGE_TOOL_NAME:
                    return None
                tool_call_states[note_tcid] = replace(
                    note_tc,
                    waiting_note_msg_id=str(ev.get("msg_id") or "") or None,
                    waiting_note_event_seq=ev_seq,
                    waiting_note_ts=str(ev.get("ts") or "") or None,
                    waiting_note_content=str(payload.get("content") or ""),
                    waiting_note_skipped_on_continue=False,
                )
                touched_get_user_ids.add(note_tcid)
                continue
            if payload.get("role") != "tool" or payload.get("tool_call_id") is None:
                continue
            msg_id = ev.get("msg_id")
            if msg_id and str(msg_id) in skipped_msg_ids:
                tool_call_states[tcid] = replace(
                    tc,
                    result_msg_id=str(msg_id),
                    result_skipped_on_continue=True,
                )
                touched_get_user_ids.add(str(tcid))
                continue
            tool_call_states[tcid] = replace(
                tc,
                published=True,
                result_msg_id=str(msg_id or "") or None,
                result_skipped_on_continue=False,
                state_event_seq=ev_seq,
            )
            touched_get_user_ids.add(str(tcid))
        elif ev_type == "tool_call.approval":
            decision = payload.get("decision")
            if decision == "global_approval":
                current_global_start = ev_seq
                continue
            if decision == "revoke_global_approval":
                current_global_start = None
                continue
            if decision == "all-in-turn":
                open_all_in_turn_user_seq = _latest_user_seq_at_or_before(user_seqs, ev_seq)
                open_all_in_turn_approval_seq = ev_seq
                for candidate_tcid, candidate_tc in _tool_call_ids_in_turn(tool_call_states, user_seqs, ev_seq):
                    if candidate_tc.approval_decision is None:
                        tool_call_states[candidate_tcid] = replace(
                            candidate_tc,
                            approval_decision="granted",
                            state_event_seq=ev_seq,
                        )
                continue
            if decision in {"granted", "denied"}:
                if tc is None:
                    return None
                tool_call_states[tcid] = replace(tc, approval_decision=decision, state_event_seq=ev_seq)
            else:
                return None
        elif ev_type == "tool_call.execution_started":
            inv = ev.get("invoke_id")
            raw_timeout = payload.get("timeout")
            try:
                timeout_sec = float(raw_timeout) if raw_timeout is not None else None
            except (TypeError, ValueError):
                timeout_sec = None
            resume_after_lease_loss = bool(payload.get("resume_after_lease_loss"))
            tool_call_states[tcid] = replace(
                tc,
                execution_started=True,
                owner_invoke_id=inv if isinstance(inv, str) and inv else tc.owner_invoke_id,
                execution_started_ts=str(ev.get("ts") or "") or tc.execution_started_ts,
                execution_timeout_sec=(
                    timeout_sec if timeout_sec is not None and timeout_sec > 0 else tc.execution_timeout_sec
                ),
                execution_timeout_deadline=(
                    str(payload.get("timeout_deadline") or "") or tc.execution_timeout_deadline
                ),
                execution_resumes_after_lease_loss=(
                    bool(payload.get("resumes_after_lease_loss"))
                    if "resumes_after_lease_loss" in payload
                    else tc.execution_resumes_after_lease_loss
                ),
                finished_reason=None if resume_after_lease_loss else tc.finished_reason,
                finished_output=None if resume_after_lease_loss else tc.finished_output,
                finished_original_char_count=(
                    None if resume_after_lease_loss else tc.finished_original_char_count
                ),
                finished_output_capped=(
                    False if resume_after_lease_loss else tc.finished_output_capped
                ),
                force_provider_output_masking=(
                    False if resume_after_lease_loss else tc.force_provider_output_masking
                ),
                transcript_content_tool_name=(
                    None if resume_after_lease_loss else tc.transcript_content_tool_name
                ),
                output_decision=None if resume_after_lease_loss else tc.output_decision,
                last_output_approval_payload=(
                    None if resume_after_lease_loss else tc.last_output_approval_payload
                ),
                output_decision_event_seq=(
                    None if resume_after_lease_loss else tc.output_decision_event_seq
                ),
                finished_event_seq=(
                    None if resume_after_lease_loss else tc.finished_event_seq
                ),
                state_event_seq=ev_seq,
            )
        elif ev_type == "tool_call.finished":
            changes: Dict[str, Any] = {}
            reason = payload.get("reason")
            if isinstance(reason, str):
                changes["finished_reason"] = reason
            out = payload.get("output")
            if out is not None:
                changes["finished_output"] = str(out)
            stored_output = str(out) if out is not None else str(tc.finished_output or "")
            try:
                original_char_count = int(
                    payload.get("original_char_count", len(stored_output))
                )
            except (TypeError, ValueError):
                original_char_count = len(stored_output)
            changes["finished_original_char_count"] = max(
                len(stored_output), original_char_count
            )
            changes["finished_output_capped"] = bool(
                payload.get("output_capped")
                or changes["finished_original_char_count"] > len(stored_output)
            )
            from .tool_output_presentation import normalize_publication_presentation

            changes["publication_presentation"] = normalize_publication_presentation(
                payload.get("publication_presentation")
            )
            changes["force_provider_output_masking"] = bool(
                payload.get("force_provider_output_masking")
            )
            content_tool_name = payload.get("transcript_content_tool_name")
            changes["transcript_content_tool_name"] = (
                str(content_tool_name) if isinstance(content_tool_name, str) and content_tool_name else None
            )
            changes["state_event_seq"] = ev_seq
            changes["finished_event_seq"] = ev_seq
            tool_call_states[tcid] = replace(tc, **changes)
        elif ev_type == "tool_call.output_approval":
            decision = payload.get("decision")
            if not isinstance(decision, str):
                return None
            from .tool_output import output_decision_payload_priority

            current_payload = tc.last_output_approval_payload
            current_priority = output_decision_payload_priority(current_payload)
            candidate_priority = output_decision_payload_priority(payload)
            changes: Dict[str, Any] = {"state_event_seq": ev_seq}
            if tc.output_decision is None or candidate_priority > current_priority:
                changes.update({
                    "last_output_approval_payload": payload,
                    "output_decision": decision,
                    "output_decision_event_seq": ev_seq,
                })
                if tc.finished_reason is None:
                    changes["finished_reason"] = "interrupted"
                    changes["finished_output"] = str(
                        payload.get("preview")
                        or "--- INTERRUPTED ---\nTool output was decided before the tool reported a result."
                    )
            tool_call_states[tcid] = replace(tc, **changes)

    for candidate_tcid, candidate_tc in list(tool_call_states.items()):
        if not candidate_tc.execution_started or candidate_tc.finished_reason is not None:
            continue
        if (
            candidate_tc.owner_invoke_id is not None
            and candidate_tc.owner_invoke_id in interrupted_invokes
        ):
            tool_call_states[candidate_tcid] = _interrupted_tool_update(
                candidate_tc,
                "Tool execution was interrupted before the tool reported a result.",
                "--- INTERRUPTED ---\n"
                "Tool execution was interrupted before the tool reported a result.",
            )
            candidate_tc = tool_call_states[candidate_tcid]
        if (
            candidate_tc.finished_reason is None
            and candidate_tc.owner_invoke_id is not None
            and candidate_tc.owner_invoke_id in closed_invokes
        ):
            tool_call_states[candidate_tcid] = _interrupted_tool_update(
                candidate_tc,
                "Tool execution stream closed before the tool reported a result.",
                "--- INTERRUPTED ---\n"
                "Tool execution stream closed before the tool reported a result.",
            )
    for ev, payload, _ev_seq in records:
        raw_tcid = payload.get("tool_call_id") or payload.get("awaiting_user_message_tool_call_id")
        if raw_tcid:
            touched_get_user_ids.add(str(raw_tcid))
        raw_calls = payload.get("tool_calls")
        if isinstance(raw_calls, list):
            for raw_call in raw_calls:
                if isinstance(raw_call, dict) and raw_call.get("id"):
                    touched_get_user_ids.add(str(raw_call["id"]))
    for touched_id in touched_get_user_ids:
        touched = tool_call_states.get(touched_id)
        if touched is not None and touched.name == GET_USER_MESSAGE_TOOL_NAME and not touched.published:
            get_user_wait_ids.add(touched_id)
        else:
            get_user_wait_ids.discard(touched_id)

    next_runner_actionable = _next_runner_actionable_from_reduction(
        thread_id,
        tool_call_states,
        messages_after_records,
        consumed_user_msg_ids,
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
        consumed_user_msg_ids=consumed_user_msg_ids,
        last_llm_boundary_seq=last_llm_boundary_seq,
        messages_after_boundary=messages_after_boundary,
        tool_call_states=tool_call_states,
        next_runner_actionable=next_runner_actionable,
        coarse_thread_state_without_lease=coarse_state,
        get_user_wait_tool_call_ids=tuple(sorted(
            get_user_wait_ids,
            key=lambda candidate_id: (
                tool_call_states[candidate_id].waiting_note_event_seq
                if tool_call_states[candidate_id].waiting_note_event_seq is not None
                else tool_call_states[candidate_id].parent_event_seq,
                candidate_id,
            ),
        )),
        _messages_after_records=messages_after_records,
        _llm_invokes=llm_invokes,
        _last_llm_stream_boundary_seq=last_llm_stream_boundary_seq,
        _last_assistant_seq=last_assistant_seq,
        _user_seqs=user_seqs,
        _current_global_start=current_global_start,
        _open_all_in_turn_user_seq=open_all_in_turn_user_seq,
        _open_all_in_turn_approval_seq=open_all_in_turn_approval_seq,
    )


def _reduce_thread_events(db: ThreadsDB, thread_id: str) -> _ThreadEventReduction:
    """Reduce a thread's event log once into the hot state views.

    This is private and rebuildable: SQLite events remain the source of truth.
    The cache is keyed by database path, thread id, and max event sequence, so
    any appended event naturally invalidates the previous reduction.  Storing a
    new reduction prunes older entries for that same ``(db_path, thread_id)``,
    keeping at most one current projection per thread in this process.
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
    preserved_msg_ids: set[str] = set()
    consumed_user_msg_ids: set[str] = set()
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
            if msg_id and payload.get("preserve_on_continue"):
                preserved_msg_ids.add(str(msg_id))
            if msg_id and _is_consumed_get_user_message_edit(payload):
                consumed_user_msg_ids.add(str(msg_id))
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

    skipped_msg_ids.difference_update(preserved_msg_ids)

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
        declared = _tool_call_states_from_declaration(thread_id, ev, payload, ev_seq)
        if not declared:
            continue
        parent_skipped = bool(msg_id and str(msg_id) in skipped_msg_ids)
        for tcid, tc in declared.items():
            if parent_skipped and tc.name != GET_USER_MESSAGE_TOOL_NAME:
                continue
            states[tcid] = replace(tc, parent_skipped_on_continue=parent_skipped)

    global_auto_approval = False
    global_intervals: list[tuple[int, Optional[int]]] = []
    current_global_start: Optional[int] = None

    def _should_skip_tc_event(ev_seq: int, tcid: Optional[str]) -> bool:
        if continue_boundary_seq is None or continue_interrupt_seq is None:
            return False
        if ev_seq <= continue_boundary_seq or ev_seq > continue_interrupt_seq:
            return False
        if tcid and tcid in states:
            tc = states[tcid]
            # Lifecycle calls explicitly retained before continuation keep their
            # exact events/results. Other calls after the rewind boundary retain
            # the historical skip semantics.
            if tc.parent_msg_id in preserved_msg_ids:
                return False
            if tc.parent_event_seq >= continue_boundary_seq:
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
                for _tcid, tc in _tool_call_ids_in_turn(states, user_seqs, ev_seq):
                    if tc.approval_decision is not None:
                        continue
                    tc.approval_decision = "granted"
            else:
                tcid = payload.get("tool_call_id")
                if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                    if isinstance(decision, str):
                        states[tcid].approval_decision = decision
                        states[tcid].state_event_seq = ev_seq
        elif ev_type == "tool_call.execution_started":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    resume_after_lease_loss = bool(payload.get("resume_after_lease_loss"))
                    tc.execution_started = True
                    inv = ev.get("invoke_id")
                    if isinstance(inv, str) and inv:
                        tc.owner_invoke_id = inv
                    raw_timeout = payload.get("timeout")
                    try:
                        timeout_sec = float(raw_timeout) if raw_timeout is not None else None
                    except (TypeError, ValueError):
                        timeout_sec = None
                    tc.execution_started_ts = str(ev.get("ts") or "") or tc.execution_started_ts
                    if timeout_sec is not None and timeout_sec > 0:
                        tc.execution_timeout_sec = timeout_sec
                    tc.execution_timeout_deadline = (
                        str(payload.get("timeout_deadline") or "") or tc.execution_timeout_deadline
                    )
                    if "resumes_after_lease_loss" in payload:
                        tc.execution_resumes_after_lease_loss = bool(
                            payload.get("resumes_after_lease_loss")
                        )
                    if resume_after_lease_loss:
                        tc.finished_reason = None
                        tc.finished_output = None
                        tc.finished_original_char_count = None
                        tc.finished_output_capped = False
                        tc.force_provider_output_masking = False
                        tc.transcript_content_tool_name = None
                        tc.output_decision = None
                        tc.last_output_approval_payload = None
                        tc.output_decision_event_seq = None
                        tc.finished_event_seq = None
                    tc.state_event_seq = ev_seq
        elif ev_type == "tool_call.summary":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    summary = payload.get("summary")
                    if isinstance(summary, str):
                        tc.summary = summary
                    tc.state_event_seq = ev_seq
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
                    stored_output = str(out) if out is not None else str(tc.finished_output or "")
                    try:
                        original_char_count = int(
                            payload.get("original_char_count", len(stored_output))
                        )
                    except (TypeError, ValueError):
                        original_char_count = len(stored_output)
                    tc.finished_original_char_count = max(
                        len(stored_output), original_char_count
                    )
                    tc.finished_output_capped = bool(
                        payload.get("output_capped")
                        or tc.finished_original_char_count > len(stored_output)
                    )
                    from .tool_output_presentation import normalize_publication_presentation

                    tc.publication_presentation = normalize_publication_presentation(
                        payload.get("publication_presentation")
                    )
                    tc.force_provider_output_masking = bool(
                        payload.get("force_provider_output_masking")
                    )
                    content_tool_name = payload.get("transcript_content_tool_name")
                    tc.transcript_content_tool_name = (
                        str(content_tool_name)
                        if isinstance(content_tool_name, str) and content_tool_name
                        else None
                    )
                    tc.state_event_seq = ev_seq
                    tc.finished_event_seq = ev_seq
        elif ev_type == "tool_call.output_approval":
            tcid = payload.get("tool_call_id")
            if tcid in states and not _should_skip_tc_event(ev_seq, tcid):
                tc = states[tcid]
                if ev_seq > tc.parent_event_seq:
                    decision = payload.get("decision")
                    if isinstance(decision, str):
                        from .tool_output import output_decision_payload_priority

                        current_priority = output_decision_payload_priority(tc.last_output_approval_payload)
                        candidate_priority = output_decision_payload_priority(payload)
                        if tc.output_decision is None or candidate_priority > current_priority:
                            tc.output_decision = decision
                            tc.last_output_approval_payload = payload
                            tc.output_decision_event_seq = ev_seq
                    tc.state_event_seq = ev_seq
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
                    tc.state_event_seq = ev_seq

    # Fold preserve-turn note/reply/result identity once. This metadata is
    # durable recovery authority even when old /continue edits skipped the
    # declaration or note before this lifecycle repair existed.
    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")
        msg_id = str(ev.get("msg_id") or "")
        if ev_type == "msg.create":
            if (
                payload.get("role") == "assistant"
                and payload.get("answer_user_preserve_turn")
                and payload.get("source_tool_name") == GET_USER_MESSAGE_TOOL_NAME
            ):
                tcid = str(payload.get("awaiting_user_message_tool_call_id") or "")
                tc = states.get(tcid)
                if tc is not None and tc.name == GET_USER_MESSAGE_TOOL_NAME:
                    tc.waiting_note_msg_id = msg_id or None
                    tc.waiting_note_event_seq = ev_seq
                    tc.waiting_note_ts = str(ev.get("ts") or "") or None
                    tc.waiting_note_content = str(payload.get("content") or "")
                    tc.waiting_note_skipped_on_continue = bool(msg_id and msg_id in skipped_msg_ids)
            elif payload.get("role") == "tool":
                tcid = str(payload.get("tool_call_id") or "")
                tc = states.get(tcid)
                if tc is not None:
                    tc.result_msg_id = msg_id or None
                    tc.result_skipped_on_continue = bool(msg_id and msg_id in skipped_msg_ids)
        elif ev_type == "msg.edit" and _is_consumed_get_user_message_edit(payload):
            tcid = str(payload.get("consumed_by_tool_call_id") or "")
            tc = states.get(tcid)
            if tc is not None and tc.name == GET_USER_MESSAGE_TOOL_NAME:
                tc.claimed_user_msg_id = msg_id or None
                try:
                    tc.claimed_user_event_seq = int(payload.get("consumed_user_event_seq"))
                except (TypeError, ValueError):
                    tc.claimed_user_event_seq = msg_seq_by_id.get(msg_id)
                tc.claimed_user_content = str(payload.get("content") or "")

    if global_auto_approval and current_global_start is not None:
        global_intervals.append((current_global_start, None))

    open_all_in_turn_user_seq: Optional[int] = None
    open_all_in_turn_approval_seq: Optional[int] = None
    for ev, payload, ev_seq in records:
        ev_type = ev.get("type")
        if ev_type == "msg.create" and payload.get("role") == "user":
            if open_all_in_turn_user_seq is not None and ev_seq > open_all_in_turn_user_seq:
                open_all_in_turn_user_seq = None
                open_all_in_turn_approval_seq = None
        elif ev_type == "tool_call.approval" and payload.get("decision") == "all-in-turn":
            open_all_in_turn_user_seq = _latest_user_seq_at_or_before(user_seqs, ev_seq)
            open_all_in_turn_approval_seq = ev_seq

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
            updated = _interrupted_tool_update(
                tc,
                "Tool execution was interrupted before the tool reported a result.",
                "--- INTERRUPTED ---\n"
                "Tool execution was interrupted before the tool reported a result.",
            )
            tc.finished_reason = updated.finished_reason
            tc.finished_output = updated.finished_output
            tc.output_decision = updated.output_decision
            tc.last_output_approval_payload = updated.last_output_approval_payload
        if tc.execution_started and tc.finished_reason is None and tc.owner_invoke_id in closed_invokes:
            updated = _interrupted_tool_update(
                tc,
                "Tool execution stream closed before the tool reported a result.",
                "--- INTERRUPTED ---\n"
                "Tool execution stream closed before the tool reported a result.",
            )
            tc.finished_reason = updated.finished_reason
            tc.finished_output = updated.finished_output
            tc.output_decision = updated.output_decision
            tc.last_output_approval_payload = updated.last_output_approval_payload
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
        and not (
            payload.get("role") == "user"
            and ev.get("msg_id")
            and str(ev.get("msg_id")) in consumed_user_msg_ids
        )
    ]
    messages_after_boundary = [ev for ev, _payload_obj, _ev_seq in messages_after_records]
    next_runner_actionable = _next_runner_actionable_from_reduction(
        thread_id,
        states,
        messages_after_records,
        consumed_user_msg_ids,
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
        consumed_user_msg_ids=consumed_user_msg_ids,
        last_llm_boundary_seq=last_llm_boundary_seq,
        messages_after_boundary=messages_after_boundary,
        tool_call_states=states,
        next_runner_actionable=next_runner_actionable,
        coarse_thread_state_without_lease=coarse_state,
        get_user_wait_tool_call_ids=tuple(
            tc.tool_call_id
            for tc in sorted(
                (
                    candidate
                    for candidate in states.values()
                    if candidate.name == GET_USER_MESSAGE_TOOL_NAME and not candidate.published
                ),
                key=lambda candidate: (
                    candidate.waiting_note_event_seq
                    if candidate.waiting_note_event_seq is not None
                    else candidate.parent_event_seq,
                    candidate.tool_call_id,
                ),
            )
        ),
        _messages_after_records=messages_after_records,
        _llm_invokes=llm_invokes,
        _last_llm_stream_boundary_seq=last_llm_stream_boundary_seq,
        _last_assistant_seq=last_assistant_seq,
        _user_seqs=user_seqs,
        _current_global_start=current_global_start if global_auto_approval else None,
        _open_all_in_turn_user_seq=open_all_in_turn_user_seq,
        _open_all_in_turn_approval_seq=open_all_in_turn_approval_seq,
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
    consumed_user_msg_ids: set[str] | None = None,
) -> Optional[RunnerActionable]:
    consumed_user_msg_ids = consumed_user_msg_ids or set()
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
        if role == "user" and msg_id and str(msg_id) in consumed_user_msg_ids:
            continue
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


def get_user_wait_candidates(db: ThreadsDB, thread_id: str) -> List[ToolCallState]:
    """Return bounded unresolved/recoverable get-user lifecycles in order.

    The reducer performs one event fold (incremental on hot paths) and records
    candidate identity. Callers receive defensive copies just like
    :func:`build_tool_call_states`.
    """

    reduction = _reduce_thread_events(db, thread_id)
    return [
        deepcopy(reduction.tool_call_states[tool_call_id])
        for tool_call_id in reduction.get_user_wait_tool_call_ids
        if tool_call_id in reduction.tool_call_states
    ]


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
    preserved_msg_ids: set = set()
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
        if payload.get('preserve_on_continue'):
            preserved_msg_ids.add(msg_id)

    skipped_msg_ids.difference_update(preserved_msg_ids)

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

    User messages consumed by get_user_message_while_preserving_llm_turn are
    also omitted from this provider-trigger scan, while remaining visible in
    snapshots/UI.
    """
    # First, collect msg_ids that have been marked as skipped/consumed.
    skipped_msg_ids: set = set()
    preserved_msg_ids: set = set()
    consumed_user_msg_ids: set = set()
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
        if payload.get('preserve_on_continue'):
            preserved_msg_ids.add(msg_id)
        if _is_consumed_get_user_message_edit(payload):
            consumed_user_msg_ids.add(msg_id)

    skipped_msg_ids.difference_update(preserved_msg_ids)

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
        if msg_id and msg_id in consumed_user_msg_ids:
            try:
                payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload.get("role") == "user":
                continue
        yield ev


def _recovery_lease_available(db: ThreadsDB, thread_id: str) -> bool:
    """Return whether recovery may contend for this thread's lease.

    Lease state is intentionally read outside the event reducer: lease expiry or
    deletion does not append an event and therefore cannot be cached by the
    reducer's event watermark. Any live lease suppresses recovery. Uncertain
    ownership fails closed so a later scheduler poll can retry safely.
    """

    try:
        lease = db.current_open(thread_id)
        return lease is None or str(lease["lease_until"] or "") <= _utcnow_iso()
    except Exception:
        return False


def _orphaned_tc3_actionable(
    db: ThreadsDB,
    reduction: _ThreadEventReduction,
) -> Optional[RunnerActionable]:
    """Return recovery work for executing calls that have no live owner lease."""

    if not _recovery_lease_available(db, reduction.thread_id):
        return None

    def recovery_interrupt_exists(owner_invoke_id: Optional[str]) -> bool:
        if not owner_invoke_id:
            return False
        try:
            row = db.conn.execute(
                """
                SELECT 1 FROM events
                 WHERE thread_id=? AND type='control.interrupt'
                   AND json_extract(payload_json, '$.old_invoke_id')=?
                   AND json_extract(payload_json, '$.reason') IN (
                        'expired_lease_takeover',
                        'orphaned_tool_execution_recovery'
                   )
                 LIMIT 1
                """,
                (reduction.thread_id, owner_invoke_id),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    candidates = [
        tc
        for tc in reduction.tool_call_states.values()
        if tc.parent_role in {"assistant", "user"}
        and (
            tc.state == "TC3"
            or (
                tc.state == "TC4"
                and tc.finished_reason == "interrupted"
                and tc.output_decision is None
                and tc.finished_event_seq is None
                and recovery_interrupt_exists(tc.owner_invoke_id)
            )
        )
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda tc: (tc.parent_event_seq, tc.index, tc.tool_call_id))
    first = candidates[0]
    same_parent = [tc for tc in candidates if tc.parent_msg_id == first.parent_msg_id]
    return RunnerActionable(
        kind=("RA3_tools_user" if first.parent_role == "user" else "RA2_tools_assistant"),
        thread_id=reduction.thread_id,
        triggering_event_seq=first.parent_event_seq,
        msg_id=first.parent_msg_id,
        tool_calls=same_parent,
        recovery_mode="orphaned_tc3",
    )


def _is_stranded_successful_tc4(tc: ToolCallState) -> bool:
    return bool(
        tc.parent_role in {"assistant", "user"}
        and tc.state == "TC4"
        and str(tc.finished_reason or "").lower() in {"success", "ok"}
        and tc.finished_event_seq is not None
        and tc.output_decision is None
    )


def _stranded_successful_tc4_actionable(
    db: ThreadsDB,
    reduction: _ThreadEventReduction,
) -> Optional[RunnerActionable]:
    """Return automatic-publication recovery for durable successful TC4 calls."""

    if not _recovery_lease_available(db, reduction.thread_id):
        return None

    candidates = [
        tc
        for tc in reduction.tool_call_states.values()
        if _is_stranded_successful_tc4(tc)
    ]
    if not candidates:
        return None

    first = min(
        candidates,
        key=lambda tc: (tc.parent_event_seq, tc.index, tc.tool_call_id),
    )
    # Include already-decided siblings so a prior partial recovery attempt does
    # not publish later calls ahead of earlier TC5 results.
    same_parent = sorted(
        (
            tc
            for tc in reduction.tool_call_states.values()
            if tc.parent_msg_id == first.parent_msg_id
            and tc.parent_role == first.parent_role
            and (tc.state == "TC5" or _is_stranded_successful_tc4(tc))
        ),
        key=lambda tc: (tc.index, tc.tool_call_id),
    )
    return RunnerActionable(
        kind=("RA3_tools_user" if first.parent_role == "user" else "RA2_tools_assistant"),
        thread_id=reduction.thread_id,
        triggering_event_seq=first.parent_event_seq,
        msg_id=first.parent_msg_id,
        tool_calls=same_parent,
        recovery_mode="stranded_successful_tc4",
    )


def _recovery_actionable(
    db: ThreadsDB,
    reduction: _ThreadEventReduction,
) -> Optional[RunnerActionable]:
    # Never repeat uncertain tool side effects merely to recover publication.
    # Existing TC3 recovery therefore remains ahead of already-finished TC4.
    # Both helpers re-check the same lease because it can change between calls;
    # either must independently fail closed under a concurrent owner.
    orphan = _orphaned_tc3_actionable(db, reduction)
    if orphan is not None:
        return orphan
    return _stranded_successful_tc4_actionable(db, reduction)


def _runner_actionable_from_reduction(
    db: ThreadsDB,
    reduction: _ThreadEventReduction,
) -> Optional[RunnerActionable]:
    # Recovery has priority over starting any additional tool side effects in
    # the same parent batch. Normal actionability resumes after recovery has
    # published the interrupted or already-finished result.
    recovery = _recovery_actionable(db, reduction)
    return recovery if recovery is not None else reduction.next_runner_actionable


def discover_runner_actionable_cached(db: ThreadsDB, thread_id: str) -> Optional[RunnerActionable]:
    """Return the next actionable item using the cached event reduction.

    The final lease check remains uncached because open-stream leases can expire
    without changing the event-log watermark.
    """

    return _runner_actionable_from_reduction(db, _reduce_thread_events(db, thread_id))


def discover_runner_actionable(db: ThreadsDB, thread_id: str) -> Optional[RunnerActionable]:
    """Determine the next runner action, including lease-orphan recovery."""

    return _runner_actionable_from_reduction(db, _reduce_thread_events(db, thread_id))


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

    reduction = _reduce_thread_events(db, thread_id)
    if _recovery_actionable(db, reduction) is not None:
        return "running"
    return reduction.coarse_thread_state_without_lease
