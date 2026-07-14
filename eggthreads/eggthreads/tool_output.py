from __future__ import annotations

"""Transactional authority for tool-output publication decisions.

Raw tool output stays durable in ``tool_call.finished``.  This module owns the
single TC4 -> TC5 transition that chooses how that output is published.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .db import InvocationEventWriter, LeaseLost, ThreadsDB


OUTPUT_DECISIONS = frozenset({"whole", "partial", "omit"})

# First committed automatic/manual decisions are idempotent. Explicit user
# cancellation/explicit omission are the allowed supersessions and have highest precedence.
# Persisted priorities also make legacy/imported competing events project one
# deterministic authoritative decision.
OUTPUT_DECISION_SOURCE_PRIORITIES: Dict[str, int] = {
    "automatic_policy": 10,
    "automatic_synthetic": 20,
    "system": 30,
    "manual": 50,
    "user": 60,
    "user_omit": 100,
    "user_cancel": 100,
}


class ToolOutputFinalizationError(RuntimeError):
    """Base error for a retriable tool-output finalization failure."""

    def __init__(self, thread_id: str, tool_call_id: str, message: str):
        self.thread_id = thread_id
        self.tool_call_id = tool_call_id
        super().__init__(message)


class ToolOutputStateConflict(ToolOutputFinalizationError):
    """The tool call no longer has the expected state/version."""

    def __init__(
        self,
        thread_id: str,
        tool_call_id: str,
        *,
        expected_states: Iterable[str],
        actual_state: str,
        expected_event_seq: Optional[int],
        actual_event_seq: int,
    ):
        self.expected_states = tuple(expected_states)
        self.actual_state = actual_state
        self.expected_event_seq = expected_event_seq
        self.actual_event_seq = actual_event_seq
        expected = "/".join(self.expected_states)
        version = (
            f" at event {expected_event_seq}" if expected_event_seq is not None else ""
        )
        super().__init__(
            thread_id,
            tool_call_id,
            f"Tool output finalization expected {expected}{version}, "
            f"found {actual_state} at event {actual_event_seq}",
        )


class ToolOutputPlanError(ToolOutputFinalizationError):
    """A publication plan could not be built or validated."""


class ToolOutputPersistenceError(ToolOutputFinalizationError):
    """The authoritative decision could not be persisted."""


@dataclass(frozen=True)
class ToolOutputPublicationPlan:
    decision: str
    preview: str
    reason: str = ""
    artifact_path: str = ""
    channels: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutputFinalizationResult:
    thread_id: str
    tool_call_id: str
    decision: str
    source: str
    payload: Mapping[str, Any]
    event_seq: Optional[int]
    committed: bool
    idempotent: bool
    state_event_seq: int


def output_decision_source_priority(source: str) -> int:
    normalized = str(source or "").strip().lower()
    if normalized in OUTPUT_DECISION_SOURCE_PRIORITIES:
        return OUTPUT_DECISION_SOURCE_PRIORITIES[normalized]
    if "cancel" in normalized or "interrupt" in normalized:
        return OUTPUT_DECISION_SOURCE_PRIORITIES["user_cancel"]
    if "user" in normalized or "manual" in normalized or "ui" in normalized:
        return OUTPUT_DECISION_SOURCE_PRIORITIES["user"]
    if "auto" in normalized or "runner" in normalized or "policy" in normalized:
        return OUTPUT_DECISION_SOURCE_PRIORITIES["automatic_policy"]
    return OUTPUT_DECISION_SOURCE_PRIORITIES["system"]


def output_decision_payload_priority(payload: Mapping[str, Any] | None) -> int:
    """Return deterministic precedence for new and legacy decision payloads."""

    data = payload if isinstance(payload, Mapping) else {}
    explicit = data.get("decision_priority")
    try:
        if explicit is not None:
            return int(explicit)
    except (TypeError, ValueError):
        pass

    source = str(data.get("decision_source") or data.get("source") or "").strip()
    if source:
        return output_decision_source_priority(source)

    # Legacy events did not identify their source.  Preserve explicit user
    # cancellation semantics by recognizing their durable reason text; old
    # automatic policy reasons consistently begin with "Auto:".
    reason = str(data.get("reason") or "").lower()
    decision = str(data.get("decision") or "").lower()
    if decision == "omit" and ("cancel" in reason or "interrupt" in reason):
        return OUTPUT_DECISION_SOURCE_PRIORITIES["user_cancel"]
    if reason.startswith("auto:") or "auto-approved" in reason:
        return OUTPUT_DECISION_SOURCE_PRIORITIES["automatic_policy"]
    return OUTPUT_DECISION_SOURCE_PRIORITIES["manual"]


def _artifact_is_ready(path: str) -> bool:
    artifact = Path(str(path or "").strip())
    if not str(path or "").strip():
        return False
    return artifact.is_file() or (
        artifact.is_dir()
        and (artifact / "metadata.json").is_file()
        and any(artifact.glob("chunk-*.txt"))
    )


def _validate_plan(
    thread_id: str,
    tool_call_id: str,
    plan: ToolOutputPublicationPlan,
) -> None:
    if plan.decision not in OUTPUT_DECISIONS:
        raise ToolOutputPlanError(
            thread_id,
            tool_call_id,
            f"Invalid tool output decision: {plan.decision!r}",
        )
    if not isinstance(plan.preview, str):
        raise ToolOutputPlanError(thread_id, tool_call_id, "Tool output preview must be text")
    if plan.decision == "partial":
        artifact_path = str(plan.artifact_path or "").strip()
        if not artifact_path:
            raise ToolOutputPlanError(
                thread_id,
                tool_call_id,
                "Partial publication requires a recoverable raw-output artifact",
            )
        if not _artifact_is_ready(artifact_path):
            raise ToolOutputPlanError(
                thread_id,
                tool_call_id,
                f"Tool output artifact is unavailable: {artifact_path}",
            )


def _tool_name_for_call(db: ThreadsDB, thread_id: str, tool_call_id: str) -> str:
    try:
        from .tool_state import _reduce_thread_events

        tc = _reduce_thread_events(db, thread_id).tool_call_states.get(str(tool_call_id))
        return str(getattr(tc, "name", "") or "")
    except Exception:
        return ""


def _validate_bounded_bypass_plan(
    thread_id: str,
    tool_call_id: str,
    *,
    tool_name: str,
    full_output: str,
    plan: ToolOutputPublicationPlan,
) -> None:
    from .tool_output_contract import validate_bounded_tool_output

    if plan.decision != "whole" or plan.artifact_path:
        raise ToolOutputPlanError(
            thread_id,
            tool_call_id,
            f"{tool_name} must publish one bounded whole result without a long-output artifact",
        )
    try:
        validate_bounded_tool_output(tool_name, full_output, plan.preview)
    except ValueError as exc:
        raise ToolOutputPlanError(thread_id, tool_call_id, str(exc)) from exc


def _route_long_whole_output(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    full_output: str,
    plan: ToolOutputPublicationPlan,
    publication_presentation: Mapping[str, Any] | None = None,
) -> ToolOutputPublicationPlan:
    """Apply the canonical long-output policy to every ``whole`` plan.

    Output decisions can originate from the runner, Terminal Egg, EggW REST,
    EggW WebSocket, or recovery/interrupt code.  Keeping the size check at the
    shared TC4 -> TC5 authority prevents any of those callers from publishing
    an unbounded tool message merely by requesting ``whole``.  Short outputs
    and already-bounded decisions retain their caller-supplied plan.
    """

    from .tool_output_contract import tool_output_contract

    tool_name = _tool_name_for_call(db, thread_id, tool_call_id)
    presentation = dict(publication_presentation or {})
    contract = tool_output_contract(tool_name)
    if plan.decision == "omit":
        return plan
    if contract.bypass_long_output_routing:
        from .tool_output_contract import bounded_bypass_publication

        bounded_preview, violated = bounded_bypass_publication(
            tool_name,
            full_output,
            plan.preview,
        )
        if violated:
            plan = ToolOutputPublicationPlan(
                decision="whole",
                preview=bounded_preview,
                reason=f"{tool_name} bounded safe-output contract violation",
                channels=dict(plan.channels or {}),
                metadata={**dict(plan.metadata or {}), "bounded_contract_violation": True},
            )
        _validate_bounded_bypass_plan(
            thread_id,
            tool_call_id,
            tool_name=tool_name,
            full_output=full_output,
            plan=plan,
        )
        return plan
    if plan.decision != "whole":
        return plan

    output = full_output if isinstance(full_output, str) else str(full_output or "")
    try:
        from .builtin_plugins.output_policies import DefaultOutputPolicy
        from .output_policy import OutputPolicyRequest

        routed = DefaultOutputPolicy().decide(
            OutputPolicyRequest(
                db=db,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                output=output,
                origin="output_finalization",
                metadata={
                    "original_line_count": len(output.splitlines()),
                    "original_char_count": len(output),
                    "publication_presentation": presentation,
                },
            )
        )
    except Exception as exc:
        raise ToolOutputPlanError(
            thread_id,
            tool_call_id,
            f"Long-output routing failed: {type(exc).__name__}: {exc}",
        ) from exc

    if routed.decision == "whole":
        return plan

    reason = str(plan.reason or "").strip()
    routed_reason = str(routed.reason or "").strip()
    if reason and routed_reason:
        reason = f"{reason}; {routed_reason}"
    else:
        reason = reason or routed_reason
    return ToolOutputPublicationPlan(
        decision=routed.decision,
        preview=routed.preview,
        reason=reason,
        artifact_path=routed.artifact_path,
        channels={**dict(plan.channels or {}), **dict(routed.channels or {})},
        metadata=dict(plan.metadata or {}),
    )


def _manual_plan(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    decision: str,
    full_output: str,
    reason: str,
    publication_presentation: Mapping[str, Any] | None = None,
) -> ToolOutputPublicationPlan:
    output = full_output if isinstance(full_output, str) else str(full_output or "")
    from .tool_output_presentation import apply_output_presentation

    presentation = dict(publication_presentation or {})
    output_stats = {
        "line_count": len(output.splitlines()) if output else 0,
        "char_count": len(output),
        "publication_presentation": presentation,
    }
    if decision == "whole":
        return ToolOutputPublicationPlan(
            decision,
            apply_output_presentation(output, presentation),
            reason=reason,
            metadata=output_stats,
        )
    if decision == "omit":
        return ToolOutputPublicationPlan(decision, "Output omitted.", reason=reason, metadata=output_stats)
    if decision != "partial":
        return ToolOutputPublicationPlan(decision, "", reason=reason)

    # Local import avoids making the low-level runner dependency part of module
    # import order.  The shared artifact helper preserves existing ownership,
    # isolation, chunking, and recovery-note semantics.
    try:
        from .runner import stash_tool_output_and_build_preview

        preview, artifact_path = stash_tool_output_and_build_preview(
            db,
            thread_id,
            tool_call_id,
            output,
            publication_presentation=presentation,
        )
    except Exception as exc:
        raise ToolOutputPlanError(
            thread_id,
            tool_call_id,
            f"Tool output artifact creation failed: {type(exc).__name__}: {exc}",
        ) from exc
    return ToolOutputPublicationPlan(
        decision,
        preview,
        reason=reason,
        artifact_path=artifact_path,
        metadata=output_stats,
    )


def _existing_result(thread_id: str, tool_call_id: str, tc: Any) -> ToolOutputFinalizationResult:
    payload = dict(tc.last_output_approval_payload or {})
    source = str(payload.get("decision_source") or payload.get("source") or "legacy")
    event_seq = getattr(tc, "output_decision_event_seq", None)
    return ToolOutputFinalizationResult(
        thread_id=thread_id,
        tool_call_id=tool_call_id,
        decision=str(tc.output_decision or payload.get("decision") or ""),
        source=source,
        payload=payload,
        event_seq=int(event_seq) if event_seq is not None else None,
        committed=False,
        idempotent=True,
        state_event_seq=int(getattr(tc, "state_event_seq", -1)),
    )


def _lifecycle_event_seq_for_tool_call(db: ThreadsDB, thread_id: str, tool_call_id: str) -> int:
    row = db.conn.execute(
        """
        SELECT COALESCE(MAX(event_seq), -1) FROM events
         WHERE thread_id=?
           AND type IN (
                'tool_call.approval',
                'tool_call.execution_started',
                'tool_call.summary',
                'tool_call.finished',
                'tool_call.output_approval'
           )
           AND json_extract(payload_json, '$.tool_call_id')=?
        """,
        (thread_id, tool_call_id),
    ).fetchone()
    event_seq = int(row[0]) if row else -1
    if event_seq >= 0:
        return event_seq
    declaration = db.conn.execute(
        "SELECT event_seq, msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq",
        (thread_id,),
    )
    for row in declaration.fetchall():
        try:
            tool_calls = json.loads(row[2]).get("tool_calls") or []
        except Exception:
            continue
        for index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                continue
            candidate = call.get("id") or f"{row[1] or ''}:{index}"
            if str(candidate) == tool_call_id:
                event_seq = int(row[0])
    return event_seq


def _append_decision_with_expected_state(
    db: ThreadsDB,
    *,
    event_id: str,
    thread_id: str,
    tool_call_id: str,
    payload: Mapping[str, Any],
    expected_state_event_seq: int,
    invocation_writer: Optional[InvocationEventWriter],
) -> int:
    """Append only while the tool-call lifecycle watermark is unchanged.

    The outer write transaction prevents an interleaving writer; this SQL CAS
    keeps the expected lifecycle watermark and (for runners) live lease in the
    same statement as the event insertion.
    """

    actual_event_seq = _lifecycle_event_seq_for_tool_call(db, thread_id, tool_call_id)
    if actual_event_seq != int(expected_state_event_seq):
        raise ToolOutputStateConflict(
            thread_id,
            tool_call_id,
            expected_states=("TC4",),
            actual_state="changed",
            expected_event_seq=expected_state_event_seq,
            actual_event_seq=actual_event_seq,
        )

    lease_sql = ""
    params = [
        event_id,
        thread_id,
        "tool_call.output_approval",
        None,
        invocation_writer.invoke_id if invocation_writer is not None else None,
        None,
        json.dumps(dict(payload)),
        thread_id,
        tool_call_id,
    ]
    if invocation_writer is not None:
        lease_sql = """
          AND EXISTS (
                SELECT 1 FROM open_streams
                 WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')
          )
        """
        params.extend([thread_id, invocation_writer.invoke_id])

    cur = db.conn.execute(
        f"""
        INSERT INTO events(event_id, thread_id, type, msg_id, invoke_id, chunk_seq, payload_json)
        SELECT ?, ?, ?, ?, ?, ?, ?
         WHERE (
            SELECT COALESCE(MAX(event_seq), -1) FROM events
             WHERE thread_id=?
               AND type IN (
                    'tool_call.approval',
                    'tool_call.execution_started',
                    'tool_call.summary',
                    'tool_call.finished',
                    'tool_call.output_approval'
               )
               AND json_extract(payload_json, '$.tool_call_id')=?
         )=?
         {lease_sql}
        """,
        tuple(params[:9] + [int(expected_state_event_seq)] + params[9:]),
    )
    if cur.rowcount == 1:
        return int(cur.lastrowid)
    if invocation_writer is not None:
        lease = db.conn.execute(
            "SELECT 1 FROM open_streams WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')",
            (thread_id, invocation_writer.invoke_id),
        ).fetchone()
        if lease is None:
            raise LeaseLost(thread_id, invocation_writer.invoke_id, "tool_call.output_approval")
    raise ToolOutputStateConflict(
        thread_id,
        tool_call_id,
        expected_states=("TC4",),
        actual_state="changed",
        expected_event_seq=expected_state_event_seq,
        actual_event_seq=db.max_event_seq(thread_id),
    )


def finalize_tool_output(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    decision: str,
    source: str,
    reason: str = "",
    expected_state: str | Iterable[str] = "TC4",
    expected_event_seq: Optional[int] = None,
    publication_plan: Optional[ToolOutputPublicationPlan] = None,
    invocation_writer: Optional[InvocationEventWriter] = None,
    event_id: Optional[str] = None,
) -> ToolOutputFinalizationResult:
    """Commit the one authoritative tool-output decision.

    The operation acquires SQLite's write lock, reconstructs the current tool
    state under that lock, validates the expected state/version, and appends at
    most one ``tool_call.output_approval`` event.  A runner must supply its
    ``InvocationEventWriter`` so the commit is additionally lease-fenced.

    Duplicate automatic/manual callers receive the first committed idempotent
    result. An unpublished decision may be superseded only by explicit
    ``user_cancel`` or ``user_omit``; interrupt removes the active lease before this transition,
    so a stale publisher cannot use the superseded decision. Legacy duplicate
    events use deterministic source precedence for recovery/imported logs.
    """

    normalized_thread_id = str(thread_id)
    normalized_tool_call_id = str(tool_call_id)
    normalized_decision = str(decision or "").strip().lower()
    normalized_source = str(source or "").strip().lower()
    if isinstance(expected_state, str):
        expected_states = (expected_state,)
    else:
        expected_states = tuple(str(value) for value in expected_state)
    if not expected_states:
        expected_states = ("TC4",)
    if normalized_decision not in OUTPUT_DECISIONS:
        raise ToolOutputPlanError(
            normalized_thread_id,
            normalized_tool_call_id,
            f"Invalid tool output decision: {normalized_decision!r}",
        )
    if not normalized_source:
        raise ToolOutputPlanError(
            normalized_thread_id,
            normalized_tool_call_id,
            "Tool output decision source is required",
        )
    if invocation_writer is not None and (
        invocation_writer.db is not db
        or invocation_writer.thread_id != normalized_thread_id
    ):
        raise ToolOutputPlanError(
            normalized_thread_id,
            normalized_tool_call_id,
            "Invocation writer does not belong to this database/thread",
        )

    savepoint = f"finalize_tool_output_{uuid.uuid4().hex}"
    db.conn.execute(f"SAVEPOINT {savepoint}")
    try:
        # A no-op write obtains SQLite's single-writer reservation before any
        # state read, preventing another connection from changing TC4 between
        # reconstruction and append.  This also composes with outer savepoints.
        locked = db.conn.execute(
            "UPDATE threads SET status=status WHERE thread_id=?",
            (normalized_thread_id,),
        )
        if locked.rowcount != 1:
            raise ToolOutputStateConflict(
                normalized_thread_id,
                normalized_tool_call_id,
                expected_states=expected_states,
                actual_state="missing_thread",
                expected_event_seq=expected_event_seq,
                actual_event_seq=-1,
            )

        from .tool_state import _reduce_thread_events

        tc = _reduce_thread_events(db, normalized_thread_id).tool_call_states.get(normalized_tool_call_id)
        if tc is None:
            raise ToolOutputStateConflict(
                normalized_thread_id,
                normalized_tool_call_id,
                expected_states=expected_states,
                actual_state="missing",
                expected_event_seq=expected_event_seq,
                actual_event_seq=-1,
            )

        # Idempotency and precedence happen before expected-version validation.
        # Equal/lower-priority retries return the committed result.  A higher
        # priority explicit user decision may supersede an unpublished automatic
        # decision even when both callers observed the same TC4 watermark; this
        # is the deliberate precedence rule that makes cancel/manual win races.
        existing_priority = output_decision_payload_priority(tc.last_output_approval_payload)
        incoming_priority = output_decision_source_priority(normalized_source)
        superseding = bool(
            tc.output_decision is not None
            and not tc.published
            and normalized_source in {"user_cancel", "user_omit"}
            and incoming_priority > existing_priority
        )
        if tc.output_decision is not None and not superseding:
            result = _existing_result(normalized_thread_id, normalized_tool_call_id, tc)
            db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result

        actual_event_seq = int(getattr(tc, "state_event_seq", tc.parent_event_seq))
        state_matches = tc.state in expected_states or (superseding and tc.state == "TC5")
        version_matches = expected_event_seq is None or actual_event_seq == int(expected_event_seq)
        if not state_matches or (not version_matches and not superseding):
            raise ToolOutputStateConflict(
                normalized_thread_id,
                normalized_tool_call_id,
                expected_states=expected_states,
                actual_state=tc.state,
                expected_event_seq=expected_event_seq,
                actual_event_seq=actual_event_seq,
            )

        full_output = str(tc.finished_output or "")
        existing_payload = dict(tc.last_output_approval_payload or {})
        existing_artifact_path = str(existing_payload.get("artifact_path") or "")
        if (
            publication_plan is None
            and superseding
            and normalized_decision == "whole"
            and existing_payload.get("decision") == "partial"
            and _artifact_is_ready(existing_artifact_path)
        ):
            # An interrupt may supersede an automatic partial decision before
            # its tool message is published. Reuse that already-authoritative
            # raw artifact rather than creating a duplicate during the higher-
            # priority cancellation decision.
            plan = ToolOutputPublicationPlan(
                decision="partial",
                preview=str(existing_payload.get("preview") or ""),
                reason=reason,
                artifact_path=existing_artifact_path,
                channels=dict(existing_payload.get("channels") or {}),
                metadata={
                    key: existing_payload[key]
                    for key in ("line_count", "char_count", "original_char_count", "output_capped")
                    if key in existing_payload
                },
            )
        else:
            plan = publication_plan or _manual_plan(
                db,
                normalized_thread_id,
                normalized_tool_call_id,
                decision=normalized_decision,
                full_output=full_output,
                reason=reason,
                publication_presentation=getattr(tc, "publication_presentation", {}),
            )
        plan = _route_long_whole_output(
            db,
            normalized_thread_id,
            normalized_tool_call_id,
            full_output=full_output,
            plan=plan,
            publication_presentation=getattr(tc, "publication_presentation", {}),
        )
        auto_routed_whole = normalized_decision == "whole" and plan.decision == "partial"
        if plan.decision != normalized_decision and not auto_routed_whole:
            raise ToolOutputPlanError(
                normalized_thread_id,
                normalized_tool_call_id,
                "Publication plan decision does not match requested decision",
            )
        _validate_plan(normalized_thread_id, normalized_tool_call_id, plan)

        payload: Dict[str, Any] = {
            "tool_call_id": normalized_tool_call_id,
            "decision": plan.decision,
            "reason": str(plan.reason or reason or ""),
            "preview": plan.preview,
            "artifact_path": str(plan.artifact_path or ""),
            "channels": dict(plan.channels or {}),
            "decision_source": normalized_source,
            "decision_priority": output_decision_source_priority(normalized_source),
            "expected_state": tc.state,
            "expected_event_seq": actual_event_seq,
        }
        if auto_routed_whole:
            payload["requested_decision"] = normalized_decision
        if superseding:
            payload["supersedes_event_seq"] = getattr(tc, "output_decision_event_seq", None)
            payload["supersedes_source"] = str(
                (tc.last_output_approval_payload or {}).get("decision_source") or "legacy"
            )
        reserved = set(payload)
        for key, value in dict(plan.metadata or {}).items():
            if key not in reserved:
                payload[str(key)] = value

        chosen_event_id = event_id or os.urandom(10).hex()
        appended_seq = _append_decision_with_expected_state(
            db,
            event_id=chosen_event_id,
            thread_id=normalized_thread_id,
            tool_call_id=normalized_tool_call_id,
            payload=payload,
            expected_state_event_seq=actual_event_seq,
            invocation_writer=invocation_writer,
        )
        db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return ToolOutputFinalizationResult(
            thread_id=normalized_thread_id,
            tool_call_id=normalized_tool_call_id,
            decision=plan.decision,
            source=normalized_source,
            payload=payload,
            event_seq=int(appended_seq),
            committed=True,
            idempotent=False,
            state_event_seq=int(appended_seq),
        )
    except Exception as exc:
        try:
            db.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as rollback_exc:
            raise ToolOutputPersistenceError(
                normalized_thread_id,
                normalized_tool_call_id,
                f"Tool output finalization rollback failed after {type(exc).__name__}: {exc}; "
                f"rollback={type(rollback_exc).__name__}: {rollback_exc}",
            ) from rollback_exc
        if isinstance(exc, (LeaseLost, ToolOutputFinalizationError)):
            raise
        raise ToolOutputPersistenceError(
            normalized_thread_id,
            normalized_tool_call_id,
            f"Tool output decision append failed: {type(exc).__name__}: {exc}",
        ) from exc


__all__ = [
    "OUTPUT_DECISIONS",
    "OUTPUT_DECISION_SOURCE_PRIORITIES",
    "ToolOutputFinalizationError",
    "ToolOutputFinalizationResult",
    "ToolOutputPersistenceError",
    "ToolOutputPlanError",
    "ToolOutputPublicationPlan",
    "ToolOutputStateConflict",
    "finalize_tool_output",
    "output_decision_payload_priority",
    "output_decision_source_priority",
]
