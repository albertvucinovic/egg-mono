from __future__ import annotations

"""Typed, cursor-resumable event feed shared by EggW transports.

The feed exposes SQLite's canonical ``event_seq`` as the resume cursor while
keeping raw rows and ``payload_json`` decoding behind this boundary.
"""

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .db import ThreadsDB


class ThreadEventFeedError(RuntimeError):
    """Base error for event-feed reads."""


class ThreadEventFeedNotFound(ThreadEventFeedError):
    """Raised when the requested thread does not exist."""


class ThreadEventCursorError(ThreadEventFeedError, ValueError):
    """Raised when a cursor cannot be interpreted safely."""


@dataclass(frozen=True)
class ThreadEventEnvelope:
    """Canonical durable event shape exposed to transport clients."""

    event_id: str
    event_seq: int
    type: str
    ts: str
    msg_id: Optional[str]
    invoke_id: Optional[str]
    chunk_seq: Optional[int]
    payload: Mapping[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_seq": self.event_seq,
            "type": self.type,
            "ts": self.ts,
            "msg_id": self.msg_id,
            "invoke_id": self.invoke_id,
            "chunk_seq": self.chunk_seq,
            "payload": copy.deepcopy(dict(self.payload)),
        }


@dataclass(frozen=True)
class ActiveThreadLease:
    """Unexpired durable work lease at event-feed read time."""

    thread_id: str
    invoke_id: str
    purpose: Optional[str]
    lease_until: str
    owner: Optional[str]


@dataclass(frozen=True)
class ThreadEventBatch:
    """Bounded events strictly after ``after_seq`` in canonical order."""

    thread_id: str
    after_seq: int
    cursor: int
    events: Tuple[ThreadEventEnvelope, ...]
    active_lease: Optional[ActiveThreadLease]


def parse_event_cursor(value: Any, *, source: str = "cursor") -> int:
    """Parse a non-negative event cursor; ``-1`` is the beginning sentinel."""

    if isinstance(value, bool) or value is None or (
        isinstance(value, str) and not value.strip()
    ):
        raise ThreadEventCursorError(f"{source} must be an integer >= -1")
    try:
        text = str(value).strip()
        invalid = (
            any(character not in "-0123456789" for character in text)
            or text.count("-") > 1
            or ("-" in text and not text.startswith("-"))
        )
        if invalid:
            raise ValueError
        cursor = int(text)
    except (TypeError, ValueError) as exc:
        raise ThreadEventCursorError(f"{source} must be an integer >= -1") from exc
    if cursor < -1:
        raise ThreadEventCursorError(f"{source} must be an integer >= -1")
    return cursor


def resolve_event_cursor(
    *,
    after_seq: Any = None,
    last_event_id: Any = None,
    default: int = -1,
) -> int:
    """Resolve resume cursor with explicit query parameter precedence.

    ``after_seq`` wins when present; otherwise ``Last-Event-ID`` is used. This
    permits explicit application reconciliation while retaining standard SSE
    browser reconnect behavior.
    """

    if after_seq is not None:
        return parse_event_cursor(after_seq, source="after_seq")
    if last_event_id is not None and str(last_event_id).strip():
        return parse_event_cursor(last_event_id, source="Last-Event-ID")
    return parse_event_cursor(default, source="default cursor")


def _decode_envelope(row: Any) -> ThreadEventEnvelope:
    try:
        payload_json = row["payload_json"]
        payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    except Exception as exc:
        seq = row["event_seq"] if row is not None else "unknown"
        raise ThreadEventFeedError(f"Invalid payload_json for event {seq}") from exc
    if not isinstance(payload, Mapping):
        raise ThreadEventFeedError(f"Event {row['event_seq']} payload is not an object")
    return ThreadEventEnvelope(
        event_id=str(row["event_id"]),
        event_seq=int(row["event_seq"]),
        type=str(row["type"]),
        ts=str(row["ts"]),
        msg_id=str(row["msg_id"]) if row["msg_id"] is not None else None,
        invoke_id=str(row["invoke_id"]) if row["invoke_id"] is not None else None,
        chunk_seq=int(row["chunk_seq"]) if row["chunk_seq"] is not None else None,
        payload=copy.deepcopy(dict(payload)),
    )


class ThreadEventFeed:
    """Authoritative bounded event/cursor reader for one ``ThreadsDB``."""

    def __init__(self, db: ThreadsDB, *, batch_size: int = 256):
        if int(batch_size) < 1:
            raise ValueError("batch_size must be positive")
        self.db = db
        self.batch_size = int(batch_size)

    def thread_exists(self, thread_id: str) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return row is not None

    def active_lease(self, thread_id: str) -> Optional[ActiveThreadLease]:
        row = self.db.conn.execute(
            """
            SELECT thread_id, invoke_id, purpose, lease_until, owner
              FROM open_streams
             WHERE thread_id=? AND lease_until>datetime('now')
             LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        return ActiveThreadLease(
            thread_id=str(row["thread_id"]),
            invoke_id=str(row["invoke_id"]),
            purpose=str(row["purpose"]) if row["purpose"] is not None else None,
            lease_until=str(row["lease_until"]),
            owner=str(row["owner"]) if row["owner"] is not None else None,
        )

    def active_replay_after_seq(self, thread_id: str) -> Optional[int]:
        """Return cursor just before the live lease's stream.open, if present.

        Historical unmatched opens are ignored. The current unexpired lease is
        the sole active-work authority, and stream replay is scoped to its exact
        ``invoke_id``.
        """

        lease = self.active_lease(thread_id)
        if lease is None:
            return None
        row = self.db.conn.execute(
            """
            SELECT event_seq FROM events
             WHERE thread_id=? AND invoke_id=? AND type='stream.open'
             ORDER BY event_seq ASC LIMIT 1
            """,
            (thread_id, lease.invoke_id),
        ).fetchone()
        return int(row["event_seq"]) - 1 if row is not None else None

    def current_cursor(self, thread_id: str) -> int:
        if not self.thread_exists(thread_id):
            raise ThreadEventFeedNotFound(f"Thread not found: {thread_id}")
        row = self.db.conn.execute(
            "SELECT MAX(event_seq) AS cursor FROM events WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        return int(row["cursor"]) if row and row["cursor"] is not None else -1

    def read_after(
        self,
        thread_id: str,
        after_seq: int,
        *,
        limit: Optional[int] = None,
    ) -> ThreadEventBatch:
        cursor = parse_event_cursor(after_seq, source="after_seq")
        if not self.thread_exists(thread_id):
            raise ThreadEventFeedNotFound(f"Thread not found: {thread_id}")
        batch_limit = self.batch_size if limit is None else int(limit)
        if batch_limit < 1 or batch_limit > self.batch_size:
            raise ValueError(
                f"limit must be between 1 and configured batch_size {self.batch_size}"
            )
        rows = self.db.conn.execute(
            """
            SELECT event_id, event_seq, type, ts, msg_id, invoke_id, chunk_seq, payload_json
              FROM events
             WHERE thread_id=? AND event_seq>?
             ORDER BY event_seq ASC
             LIMIT ?
            """,
            (thread_id, cursor, batch_limit),
        ).fetchall()
        events = tuple(_decode_envelope(row) for row in rows)
        next_cursor = events[-1].event_seq if events else cursor
        return ThreadEventBatch(
            thread_id=thread_id,
            after_seq=cursor,
            cursor=next_cursor,
            events=events,
            active_lease=self.active_lease(thread_id),
        )


__all__ = [
    "ActiveThreadLease",
    "ThreadEventBatch",
    "ThreadEventCursorError",
    "ThreadEventEnvelope",
    "ThreadEventFeed",
    "ThreadEventFeedError",
    "ThreadEventFeedNotFound",
    "parse_event_cursor",
    "resolve_event_cursor",
]
