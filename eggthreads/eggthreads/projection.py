from __future__ import annotations

"""Canonical, watermark-bounded thread message projection.

This module is the event-store boundary for projected messages: SQLite rows and
``payload_json`` are decoded here and do not leak through the typed projection
API. Snapshots are optional acceleration state; events remain authoritative.
"""

import copy
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .db import ThreadsDB


PROJECTION_SNAPSHOT_VERSION = 1
_PROJECTION_METADATA_KEY = "_thread_projection"


class ThreadProjectionError(RuntimeError):
    """Raised when a bounded event stream cannot be projected faithfully."""


@dataclass(frozen=True)
class ProjectedMessage:
    """Typed effective message view with full payload and event metadata."""

    thread_id: str
    msg_id: str
    payload: Mapping[str, Any]
    created_event_seq: int
    created_event_id: Optional[str]
    created_at: Optional[str]
    last_event_seq: int
    last_event_id: Optional[str]
    updated_at: Optional[str]
    deleted: bool = False
    skipped_on_continue: bool = False

    @property
    def is_effective(self) -> bool:
        return not self.deleted and not self.skipped_on_continue

    def as_message_dict(self) -> Dict[str, Any]:
        """Return the compatibility snapshot message without losing payload keys.

        Snapshot consumers historically expect ``msg_id``, ``ts``, and
        ``event_seq`` at top level. The exact original payload remains available
        separately on this typed view and in namespaced snapshot acceleration
        metadata, including in the rare case of a provider key collision.
        """

        message = copy.deepcopy(dict(self.payload))
        public_msg_id = None if self.msg_id.startswith("event:") else self.msg_id
        message["msg_id"] = public_msg_id
        message["role"] = message.get("role")
        if self.created_at is not None:
            message["ts"] = self.created_at
        message["event_seq"] = self.created_event_seq
        return message

    def _state_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "payload": copy.deepcopy(dict(self.payload)),
            "created_event_seq": self.created_event_seq,
            "created_event_id": self.created_event_id,
            "created_at": self.created_at,
            "last_event_seq": self.last_event_seq,
            "last_event_id": self.last_event_id,
            "updated_at": self.updated_at,
            "deleted": self.deleted,
            "skipped_on_continue": self.skipped_on_continue,
        }

    @classmethod
    def _from_state_dict(cls, thread_id: str, raw: Mapping[str, Any]) -> "ProjectedMessage":
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise ThreadProjectionError("Snapshot projection message payload is invalid")
        msg_id = raw.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            raise ThreadProjectionError("Snapshot projection message id is invalid")
        try:
            created_event_seq = int(raw.get("created_event_seq"))
            last_event_seq = int(raw.get("last_event_seq"))
        except (TypeError, ValueError) as exc:
            raise ThreadProjectionError("Snapshot projection message watermark is invalid") from exc
        if last_event_seq < created_event_seq:
            raise ThreadProjectionError("Snapshot projection message watermark regressed")
        return cls(
            thread_id=thread_id,
            msg_id=msg_id,
            payload=copy.deepcopy(dict(payload)),
            created_event_seq=created_event_seq,
            created_event_id=_optional_text(raw.get("created_event_id")),
            created_at=_optional_text(raw.get("created_at")),
            last_event_seq=last_event_seq,
            last_event_id=_optional_text(raw.get("last_event_id")),
            updated_at=_optional_text(raw.get("updated_at")),
            deleted=bool(raw.get("deleted")),
            skipped_on_continue=bool(raw.get("skipped_on_continue")),
        )


@dataclass(frozen=True)
class ThreadProjection:
    """Canonical message state for one thread through an explicit event watermark."""

    thread_id: str
    through_event_seq: int
    message_states: Tuple[ProjectedMessage, ...]
    started_from_snapshot_event_seq: int = -1
    tail_event_types: Tuple[str, ...] = ()
    _base_snapshot: Optional[Mapping[str, Any]] = field(default=None, repr=False, compare=False)

    @property
    def messages(self) -> Tuple[ProjectedMessage, ...]:
        return tuple(message for message in self.message_states if message.is_effective)

    def message_dicts(self) -> List[Dict[str, Any]]:
        return [message.as_message_dict() for message in self.messages]

    def to_snapshot_dict(self) -> Dict[str, Any]:
        return {
            "messages": self.message_dicts(),
            _PROJECTION_METADATA_KEY: {
                "version": PROJECTION_SNAPSHOT_VERSION,
                "thread_id": self.thread_id,
                "through_event_seq": self.through_event_seq,
                "message_states": [message._state_dict() for message in self.message_states],
            },
        }

    @property
    def base_snapshot(self) -> Optional[Mapping[str, Any]]:
        return self._base_snapshot


@dataclass(frozen=True)
class _ProjectionEvent:
    thread_id: str
    event_seq: int
    event_id: Optional[str]
    type: str
    msg_id: Optional[str]
    ts: Optional[str]
    payload: Mapping[str, Any]


def _optional_text(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    try:
        return record[key]
    except (KeyError, IndexError):
        return default


def _decode_event_record(record: Any, *, default_thread_id: str = "") -> _ProjectionEvent:
    try:
        event_seq = int(_record_value(record, "event_seq"))
    except (TypeError, ValueError) as exc:
        raise ThreadProjectionError("Projection event has no valid event_seq") from exc
    event_type = _record_value(record, "type")
    if not isinstance(event_type, str) or not event_type:
        raise ThreadProjectionError(f"Projection event {event_seq} has no valid type")
    payload_json = _record_value(record, "payload_json")
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    except Exception as exc:
        raise ThreadProjectionError(f"Projection event {event_seq} has invalid payload JSON") from exc
    if not isinstance(payload, Mapping):
        raise ThreadProjectionError(f"Projection event {event_seq} payload is not an object")
    return _ProjectionEvent(
        thread_id=str(_record_value(record, "thread_id", default_thread_id) or default_thread_id),
        event_seq=event_seq,
        event_id=_optional_text(_record_value(record, "event_id")),
        type=event_type,
        msg_id=_optional_text(_record_value(record, "msg_id")),
        ts=_optional_text(_record_value(record, "ts")),
        payload=copy.deepcopy(dict(payload)),
    )


def _load_events(
    db: ThreadsDB,
    thread_id: str,
    *,
    after_event_seq: int,
    through_event_seq: int,
) -> Tuple[_ProjectionEvent, ...]:
    rows = db.conn.execute(
        """
        SELECT event_seq, event_id, ts, thread_id, type, msg_id, payload_json
          FROM events
         WHERE thread_id=? AND event_seq>? AND event_seq<=?
         ORDER BY event_seq ASC
        """,
        (thread_id, int(after_event_seq), int(through_event_seq)),
    ).fetchall()
    return tuple(_decode_event_record(row, default_thread_id=thread_id) for row in rows)


def _projection_from_snapshot(
    snapshot_json: Optional[str],
    *,
    thread_id: str,
    snapshot_event_seq: int,
    through_event_seq: int,
) -> Optional[ThreadProjection]:
    if not isinstance(snapshot_json, str) or not snapshot_json or snapshot_event_seq < 0:
        return None
    if snapshot_event_seq > through_event_seq:
        return None
    try:
        snapshot = json.loads(snapshot_json)
    except Exception:
        return None
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("messages"), list):
        return None
    metadata = snapshot.get(_PROJECTION_METADATA_KEY)
    if not isinstance(metadata, Mapping):
        return None
    try:
        version = int(metadata.get("version"))
        metadata_event_seq = int(metadata.get("through_event_seq"))
    except (TypeError, ValueError):
        return None
    if (
        version != PROJECTION_SNAPSHOT_VERSION
        or metadata.get("thread_id") != thread_id
        or metadata_event_seq != snapshot_event_seq
    ):
        return None
    raw_states = metadata.get("message_states")
    if not isinstance(raw_states, list):
        return None
    try:
        states = tuple(ProjectedMessage._from_state_dict(thread_id, raw) for raw in raw_states)
    except (ThreadProjectionError, TypeError):
        return None
    if any(state.last_event_seq > snapshot_event_seq for state in states):
        return None
    projection = ThreadProjection(
        thread_id=thread_id,
        through_event_seq=snapshot_event_seq,
        message_states=states,
        started_from_snapshot_event_seq=snapshot_event_seq,
        _base_snapshot=copy.deepcopy(dict(snapshot)),
    )
    # Validate the public cache and internal state as one coherent version.
    if projection.message_dicts() != snapshot.get("messages"):
        return None
    return projection


def _apply_events(
    thread_id: str,
    base_states: Sequence[ProjectedMessage],
    events: Sequence[_ProjectionEvent],
    *,
    through_event_seq: int,
    started_from_snapshot_event_seq: int,
    base_snapshot: Optional[Mapping[str, Any]],
) -> ThreadProjection:
    ordered_ids: List[str] = []
    states: Dict[str, ProjectedMessage] = {}
    for state in base_states:
        if state.msg_id in states:
            raise ThreadProjectionError(f"Duplicate projected message id: {state.msg_id}")
        ordered_ids.append(state.msg_id)
        states[state.msg_id] = state

    last_applied_event_seq = started_from_snapshot_event_seq
    for event in events:
        if event.event_seq <= last_applied_event_seq:
            raise ThreadProjectionError(
                f"Projection events are not strictly ordered at {event.event_seq}"
            )
        last_applied_event_seq = event.event_seq
        if event.thread_id and event.thread_id != thread_id:
            raise ThreadProjectionError(
                f"Projection event {event.event_seq} belongs to {event.thread_id}, not {thread_id}"
            )
        if event.type == "msg.create":
            if not event.msg_id:
                # A message without an identity cannot safely receive later
                # edits/deletes. Keep the old builder's display behavior while
                # assigning a stable event-derived internal identity.
                msg_id = f"event:{event.event_seq}"
            else:
                msg_id = event.msg_id
            if msg_id in states:
                # Event IDs/message IDs are expected to be unique. Replaying a
                # duplicate must not silently replace provider payload.
                raise ThreadProjectionError(f"Duplicate msg.create id: {msg_id}")
            ordered_ids.append(msg_id)
            states[msg_id] = ProjectedMessage(
                thread_id=thread_id,
                msg_id=msg_id,
                payload=copy.deepcopy(dict(event.payload)),
                created_event_seq=event.event_seq,
                created_event_id=event.event_id,
                created_at=event.ts,
                last_event_seq=event.event_seq,
                last_event_id=event.event_id,
                updated_at=event.ts,
            )
            continue

        if event.type in {"msg.edit", "msg.delete"}:
            if not event.msg_id or event.msg_id not in states:
                continue
            current = states[event.msg_id]
            if event.type == "msg.delete":
                states[event.msg_id] = replace(
                    current,
                    deleted=True,
                    last_event_seq=event.event_seq,
                    last_event_id=event.event_id,
                    updated_at=event.ts,
                )
                continue
            if event.payload.get("skipped_on_continue"):
                states[event.msg_id] = replace(
                    current,
                    skipped_on_continue=True,
                    last_event_seq=event.event_seq,
                    last_event_id=event.event_id,
                    updated_at=event.ts,
                )
                continue
            updated_payload = copy.deepcopy(dict(current.payload))
            updated_payload.update(copy.deepcopy(dict(event.payload)))
            states[event.msg_id] = replace(
                current,
                payload=updated_payload,
                last_event_seq=event.event_seq,
                last_event_id=event.event_id,
                updated_at=event.ts,
            )
            continue

        if event.type == "control.interrupt" and event.payload.get("purpose") == "continue":
            continue_from = event.payload.get("continue_from_msg_id")
            if not isinstance(continue_from, str) or continue_from not in states:
                continue
            continue_seq = states[continue_from].created_event_seq
            for msg_id in ordered_ids:
                current = states[msg_id]
                if not (continue_seq < current.created_event_seq < event.event_seq):
                    continue
                if current.payload.get("preserve_on_continue"):
                    continue
                states[msg_id] = replace(
                    current,
                    skipped_on_continue=True,
                    last_event_seq=event.event_seq,
                    last_event_id=event.event_id,
                    updated_at=event.ts,
                )

    return ThreadProjection(
        thread_id=thread_id,
        through_event_seq=int(through_event_seq),
        message_states=tuple(states[msg_id] for msg_id in ordered_ids),
        started_from_snapshot_event_seq=int(started_from_snapshot_event_seq),
        tail_event_types=tuple(event.type for event in events),
        _base_snapshot=copy.deepcopy(dict(base_snapshot)) if isinstance(base_snapshot, Mapping) else None,
    )


def project_event_records(
    events: Iterable[Any],
    *,
    thread_id: str = "",
    through_event_seq: Optional[int] = None,
) -> ThreadProjection:
    """Project already-loaded event records (compatibility/testing entry point)."""

    decoded = tuple(_decode_event_record(record, default_thread_id=thread_id) for record in events)
    if through_event_seq is None:
        through_event_seq = max((event.event_seq for event in decoded), default=-1)
    bounded = tuple(event for event in decoded if event.event_seq <= int(through_event_seq))
    effective_thread_id = thread_id or next((event.thread_id for event in bounded if event.thread_id), "")
    return _apply_events(
        effective_thread_id,
        (),
        bounded,
        through_event_seq=int(through_event_seq),
        started_from_snapshot_event_seq=-1,
        base_snapshot=None,
    )


def load_thread_projection(
    db: ThreadsDB,
    thread_id: str,
    through_event_seq: int,
    *,
    use_snapshot: bool = True,
) -> ThreadProjection:
    """Load canonical message state through exactly ``through_event_seq``.

    A coherent versioned snapshot at or before the target may seed replay. If
    absent, malformed, newer than the target, or internally inconsistent, full
    replay starts at the event log. Both paths apply the same reducer.
    """

    target = int(through_event_seq)
    if target < -1:
        raise ValueError("through_event_seq must be >= -1")
    thread = db.get_thread(thread_id)
    if thread is None:
        raise ThreadProjectionError(f"Thread not found: {thread_id}")
    max_event_seq = db.max_event_seq(thread_id)
    if target > max_event_seq:
        raise ThreadProjectionError(
            f"Projection watermark {target} exceeds thread {thread_id} maximum {max_event_seq}"
        )

    base: Optional[ThreadProjection] = None
    if use_snapshot:
        base = _projection_from_snapshot(
            thread.snapshot_json,
            thread_id=thread_id,
            snapshot_event_seq=int(thread.snapshot_last_event_seq),
            through_event_seq=target,
        )
    after_seq = base.through_event_seq if base is not None else -1
    events = _load_events(
        db,
        thread_id,
        after_event_seq=after_seq,
        through_event_seq=target,
    )
    return _apply_events(
        thread_id,
        base.message_states if base is not None else (),
        events,
        through_event_seq=target,
        started_from_snapshot_event_seq=after_seq,
        base_snapshot=base.base_snapshot if base is not None else None,
    )


__all__ = [
    "PROJECTION_SNAPSHOT_VERSION",
    "ProjectedMessage",
    "ThreadProjection",
    "ThreadProjectionError",
    "load_thread_projection",
    "project_event_records",
]
