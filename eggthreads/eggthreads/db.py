from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from .schema import SCHEMA_SQL


SQLITE_PATH = Path('.egg/threads.sqlite')


def _ensure_db(path: Path = SQLITE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # An in-memory database cannot be cloned by path for worker ownership.
    # SQLite is built in serialized mode; explicitly allow that one connection
    # to move to a bounded tool worker while direct execute blocks its caller.
    conn = sqlite3.connect(
        str(path),
        timeout=10,
        isolation_level=None,
        check_same_thread=str(path) != ":memory:",
    )  # autocommit
    conn.row_factory = sqlite3.Row
    # WAL for concurrency
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@dataclass
class ThreadRow:
    thread_id: str
    name: Optional[str]
    short_recap: str
    status: str
    snapshot_json: Optional[str]
    snapshot_last_event_seq: int
    initial_model_key: Optional[str]
    depth: int
    created_at: str


class LeaseLost(RuntimeError):
    """Raised when an invocation attempts to write without its live lease."""

    def __init__(self, thread_id: str, invoke_id: str, operation: str = "append_event"):
        self.thread_id = thread_id
        self.invoke_id = invoke_id
        self.operation = operation
        super().__init__(
            f"Invocation lease lost for thread {thread_id}, invoke {invoke_id} during {operation}"
        )


class InvocationEventWriter:
    """Lease-fenced persistence authority for one runner invocation."""

    def __init__(self, db: "ThreadsDB", thread_id: str, invoke_id: str):
        self.db = db
        self.thread_id = thread_id
        self.invoke_id = invoke_id

    def append_event(
        self,
        *,
        event_id: str,
        type_: str,
        payload: Dict[str, Any],
        msg_id: Optional[str] = None,
        chunk_seq: Optional[int] = None,
    ) -> int:
        return self.db.append_event_with_lease(
            event_id=event_id,
            thread_id=self.thread_id,
            invoke_id=self.invoke_id,
            type_=type_,
            payload=payload,
            msg_id=msg_id,
            chunk_seq=chunk_seq,
        )

    def close(self, *, event_id: str, payload: Optional[Dict[str, Any]] = None) -> int:
        """Append a lease-fenced stream.close event."""

        return self.db.close_invocation_with_lease(
            event_id=event_id,
            thread_id=self.thread_id,
            invoke_id=self.invoke_id,
            payload=payload or {},
        )

    def release(self) -> bool:
        """Release only this invocation's still-live lease."""

        return self.db.release_invocation_with_lease(self.thread_id, self.invoke_id)


class ThreadsDB:
    """Thin DB layer adhering to ../egg/SQLITE_PLAN_CLEAN.md schema."""

    def __init__(self, db_path: Path | str = SQLITE_PATH):
        self.path = Path(db_path)
        self.conn = _ensure_db(self.path)

    # Schema management -------------------------------------------------
    def init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(SCHEMA_SQL)
        cur.close()

    # Threads -----------------------------------------------------------
    def create_thread(self, thread_id: str, name: Optional[str] = None, parent_id: Optional[str] = None,
                      waiting_until: Optional[str] = None, initial_model_key: Optional[str] = None, depth: int = 0,
                      initial_events: Optional[Iterable[tuple[str, Any]]] = None) -> str:
        """Create a thread and mandatory initial events in one transaction."""

        savepoint = f"create_thread_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.conn.execute(
                "INSERT INTO threads(thread_id, name, initial_model_key, depth) VALUES (?,?,?,?)",
                (thread_id, name, initial_model_key, depth)
            )
            if parent_id:
                self.conn.execute(
                    "INSERT INTO children(parent_id, child_id, waiting_until) VALUES (?,?,?)",
                    (parent_id, thread_id, waiting_until)
                )
            for type_, payload_source in initial_events or ():
                payload = payload_source() if callable(payload_source) else payload_source
                self.append_event(
                    event_id=uuid.uuid4().hex,
                    thread_id=thread_id,
                    type_=type_,
                    payload=payload,
                )
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return thread_id

    def get_thread(self, thread_id: str) -> Optional[ThreadRow]:
        cur = self.conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,))
        row = cur.fetchone()
        return ThreadRow(**dict(row)) if row else None

    def get_thread_metadata(self, thread_id: str) -> Optional[ThreadRow]:
        """Return thread row metadata without loading the snapshot JSON blob."""
        cur = self.conn.execute(
            "SELECT thread_id, name, short_recap, status, NULL AS snapshot_json, "
            "snapshot_last_event_seq, initial_model_key, depth, created_at "
            "FROM threads WHERE thread_id=?",
            (thread_id,),
        )
        row = cur.fetchone()
        return ThreadRow(**dict(row)) if row else None

    def set_thread_status(self, thread_id: str, status: str) -> None:
        self.conn.execute("UPDATE threads SET status=? WHERE thread_id=?", (status, thread_id))

    # Children ----------------------------------------------------------
    def list_children(self, parent_id: str) -> List[str]:
        cur = self.conn.execute("SELECT child_id FROM children WHERE parent_id=? ORDER BY child_id", (parent_id,))
        return [r[0] for r in cur.fetchall()]

    # Events ------------------------------------------------------------
    def append_event(self, event_id: str, thread_id: str, type_: str, payload: Dict[str, Any],
                     msg_id: Optional[str] = None, invoke_id: Optional[str] = None,
                     chunk_seq: Optional[int] = None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO events(event_id, thread_id, type, msg_id, invoke_id, chunk_seq, payload_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (event_id, thread_id, type_, msg_id, invoke_id, chunk_seq, json.dumps(payload))
        )
        return cur.lastrowid

    def invocation_writer(self, thread_id: str, invoke_id: str) -> InvocationEventWriter:
        return InvocationEventWriter(self, thread_id, invoke_id)

    def append_event_with_lease(
        self,
        *,
        event_id: str,
        thread_id: str,
        invoke_id: str,
        type_: str,
        payload: Dict[str, Any],
        msg_id: Optional[str] = None,
        chunk_seq: Optional[int] = None,
    ) -> int:
        """Append only if the exact invocation owns an unexpired lease.

        The lease predicate and event insertion are one SQLite statement, so a
        takeover/interrupt cannot interleave between authorization and append.
        """

        cur = self.conn.execute(
            """
            INSERT INTO events(event_id, thread_id, type, msg_id, invoke_id, chunk_seq, payload_json)
            SELECT ?, ?, ?, ?, ?, ?, ?
            WHERE EXISTS (
                SELECT 1 FROM open_streams
                WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')
            )
            """,
            (
                event_id,
                thread_id,
                type_,
                msg_id,
                invoke_id,
                chunk_seq,
                json.dumps(payload),
                thread_id,
                invoke_id,
            ),
        )
        if cur.rowcount != 1:
            raise LeaseLost(thread_id, invoke_id, type_)
        return cur.lastrowid

    def close_invocation_with_lease(
        self,
        *,
        event_id: str,
        thread_id: str,
        invoke_id: str,
        payload: Dict[str, Any],
    ) -> int:
        """Append a lease-fenced stream.close event.

        Release remains a separate exact-owner operation so runner post-stream
        work can stay fenced by the same lease until all owned writes finish.
        """

        return self.append_event_with_lease(
            event_id=event_id,
            thread_id=thread_id,
            invoke_id=invoke_id,
            type_="stream.close",
            payload=payload,
        )

    def release_invocation_with_lease(self, thread_id: str, invoke_id: str) -> bool:
        cur = self.conn.execute(
            """
            DELETE FROM open_streams
            WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')
            """,
            (thread_id, invoke_id),
        )
        if cur.rowcount != 1:
            raise LeaseLost(thread_id, invoke_id, "release")
        return True

    def max_chunk_seq(self, invoke_id: str) -> int:
        cur = self.conn.execute("SELECT MAX(chunk_seq) FROM events WHERE invoke_id=? AND type='stream.delta'", (invoke_id,))
        v = cur.fetchone()[0]
        return int(v) if v is not None else -1

    def events_since(self, thread_id: str, after_seq: int) -> Iterable[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                                (thread_id, after_seq))
        yield from cur

    def max_event_seq(self, thread_id: str) -> int:
        """Return the maximum event_seq for a thread, or -1 if none.

        This small helper centralises the common
        ``SELECT MAX(event_seq) FROM events`` pattern so callers do not
        duplicate SQL text and error handling in multiple modules.
        """
        try:
            cur = self.conn.execute(
                "SELECT MAX(event_seq) FROM events WHERE thread_id=?",
                (thread_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else -1
        except Exception:
            return -1

    # Open streams (per-thread lease) ----------------------------------
    def try_open_stream(self, thread_id: str, invoke_id: str, lease_until_iso: str,
                        owner: Optional[str] = None, purpose: Optional[str] = None) -> bool:
        """Acquire an absent lease or atomically take over an expired one."""

        savepoint = f"open_stream_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            expired = self.conn.execute(
                """
                SELECT invoke_id, purpose FROM open_streams
                WHERE thread_id=? AND lease_until<=datetime('now')
                """,
                (thread_id,),
            ).fetchone()
            if expired is not None:
                updated = self.conn.execute(
                    """
                    UPDATE open_streams
                       SET invoke_id=?, lease_until=?, owner=?, purpose=?, heartbeat_at=datetime('now')
                     WHERE thread_id=? AND invoke_id=? AND lease_until<=datetime('now')
                    """,
                    (invoke_id, lease_until_iso, owner, purpose, thread_id, expired[0]),
                )
                if updated.rowcount == 1:
                    self.append_event(
                        event_id=f"lease-takeover-{invoke_id}",
                        thread_id=thread_id,
                        type_="control.interrupt",
                        payload={
                            "reason": "expired_lease_takeover",
                            "old_invoke_id": expired[0],
                            "new_invoke_id": invoke_id,
                            "purpose": expired[1] or purpose,
                        },
                    )
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    return True

            active = self.conn.execute(
                "SELECT 1 FROM open_streams WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if active is not None:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                return False
            self.conn.execute(
                """
                INSERT INTO open_streams(thread_id, invoke_id, lease_until, owner, purpose, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (thread_id, invoke_id, lease_until_iso, owner, purpose),
            )
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return True
        except sqlite3.IntegrityError:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return False
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def heartbeat(self, thread_id: str, invoke_id: str, lease_until_iso: str) -> bool:
        cur = self.conn.execute(
            """
            UPDATE open_streams SET lease_until=?, heartbeat_at=datetime('now')
            WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')
            """,
            (lease_until_iso, thread_id, invoke_id)
        )
        return cur.rowcount == 1

    def switch_invoke(self, thread_id: str, old_invoke_id: str, new_invoke_id: str) -> bool:
        """Atomically switch the invoke_id for an active open stream row of a thread.
        Keeps the same lease_until value and ownership.
        Returns True if the row was updated.
        """
        cur = self.conn.execute(
            """
            UPDATE open_streams
               SET invoke_id=?, heartbeat_at=datetime('now')
             WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')
            """,
            (new_invoke_id, thread_id, old_invoke_id)
        )
        return cur.rowcount == 1

    def release(self, thread_id: str, invoke_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM open_streams WHERE thread_id=? AND invoke_id=?", (thread_id, invoke_id))
        return cur.rowcount == 1

    def current_open(self, thread_id: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM open_streams WHERE thread_id=?", (thread_id,))
        return cur.fetchone()
