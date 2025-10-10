from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from .schema import SCHEMA_SQL


SQLITE_PATH = Path('.egg/threads.sqlite')


def _ensure_db(path: Path = SQLITE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)  # autocommit
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
                      waiting_until: Optional[str] = None, initial_model_key: Optional[str] = None, depth: int = 0) -> str:
        self.conn.execute(
            "INSERT INTO threads(thread_id, name, initial_model_key, depth) VALUES (?,?,?,?)",
            (thread_id, name, initial_model_key, depth)
        )
        if parent_id:
            self.conn.execute(
                "INSERT INTO children(parent_id, child_id, waiting_until) VALUES (?,?,?)",
                (parent_id, thread_id, waiting_until)
            )
        return thread_id

    def get_thread(self, thread_id: str) -> Optional[ThreadRow]:
        cur = self.conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,))
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

    def max_chunk_seq(self, invoke_id: str) -> int:
        cur = self.conn.execute("SELECT MAX(chunk_seq) FROM events WHERE invoke_id=? AND type='stream.delta'", (invoke_id,))
        v = cur.fetchone()[0]
        return int(v) if v is not None else -1

    def events_since(self, thread_id: str, after_seq: int) -> Iterable[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                                (thread_id, after_seq))
        yield from cur

    # Open streams (per-thread lease) ----------------------------------
    def try_open_stream(self, thread_id: str, invoke_id: str, lease_until_iso: str,
                        owner: Optional[str] = None, purpose: Optional[str] = None) -> bool:
        # insert if no active or expired; else update if same invoke_id
        cur = self.conn.execute(
            """
            INSERT INTO open_streams(thread_id, invoke_id, lease_until, owner, purpose, heartbeat_at)
            SELECT ?,?,?,?, ?, datetime('now')
            WHERE NOT EXISTS (
              SELECT 1 FROM open_streams WHERE thread_id=? AND lease_until>datetime('now')
            )
            """,
            (thread_id, invoke_id, lease_until_iso, owner, purpose, thread_id)
        )
        if cur.rowcount == 1:
            return True
        # Try takeover if expired
        cur = self.conn.execute(
            """
            UPDATE open_streams
               SET invoke_id=?, lease_until=?, owner=?, purpose=?, heartbeat_at=datetime('now')
             WHERE thread_id=? AND lease_until<=datetime('now')
            """,
            (invoke_id, lease_until_iso, owner, purpose, thread_id)
        )
        return cur.rowcount == 1

    def heartbeat(self, thread_id: str, invoke_id: str, lease_until_iso: str) -> bool:
        cur = self.conn.execute(
            "UPDATE open_streams SET lease_until=?, heartbeat_at=datetime('now') WHERE thread_id=? AND invoke_id=?",
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
