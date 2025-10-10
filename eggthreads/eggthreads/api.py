from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .db import ThreadsDB
from .snapshot import SnapshotBuilder


def _ulid_like() -> str:
    # Real ULID using Crockford's Base32. Minimal local implementation to avoid extra deps.
    import os, time
    ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    t = int(time.time() * 1000)
    def enc128(x: int, n: int) -> str:
        out = []
        for _ in range(n):
            out.append(ENCODING[x & 31])
            x >>= 5
        return ''.join(reversed(out))
    # 48-bit timestamp -> 10 chars; 80-bit randomness -> 16 chars
    ts = enc128(t, 10)
    rand = int.from_bytes(os.urandom(10), 'big')
    rd = enc128(rand, 16)
    return ts + rd


def create_root_thread(db: ThreadsDB, name: Optional[str] = None, initial_model_key: Optional[str] = None) -> str:
    tid = _ulid_like()
    db.create_thread(thread_id=tid, name=name, parent_id=None, initial_model_key=initial_model_key, depth=0)
    return tid


def create_child_thread(db: ThreadsDB, parent_id: str, name: Optional[str] = None, initial_model_key: Optional[str] = None) -> str:
    parent = db.get_thread(parent_id)
    depth = (parent.depth + 1) if parent else 1
    tid = _ulid_like()
    db.create_thread(thread_id=tid, name=name, parent_id=parent_id, initial_model_key=initial_model_key, depth=depth)
    return tid


def append_message(db: ThreadsDB, thread_id: str, role: str, content: str, extra: Optional[Dict[str, Any]] = None) -> str:
    msg_id = _ulid_like()
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='msg.create', payload={"role": role, "content": content, **(extra or {})}, msg_id=msg_id)
    return msg_id


def edit_message(db: ThreadsDB, thread_id: str, msg_id: str, new_content: str, extra: Optional[Dict[str, Any]] = None) -> None:
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='msg.edit', payload={"content": new_content, **(extra or {})}, msg_id=msg_id)


def delete_message(db: ThreadsDB, thread_id: str, msg_id: str) -> None:
    # Event-driven delete; snapshot builder may interpret to drop the message
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='msg.delete', payload={"reason": "user"}, msg_id=msg_id)


def create_snapshot(db: ThreadsDB, thread_id: str) -> None:
    # Build from all events; you can optimize by reading from last snapshot seq later
    cur = db.conn.execute("SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC", (thread_id,))
    evs = cur.fetchall()
    builder = SnapshotBuilder()
    snap = builder.build(evs)
    last_seq = evs[-1]["event_seq"] if evs else -1
    db.conn.execute("UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?",
                    (json.dumps(snap), last_seq, thread_id))


def interrupt_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> Optional[str]:
    """Hard-preempt current step by flipping invoke_id. Returns previous invoke_id if any.

    Writers that gate on (thread_id, invoke_id) will fail on next write.
    """
    cur = db.conn.execute("SELECT invoke_id FROM open_streams WHERE thread_id=?", (thread_id,))
    row = cur.fetchone()
    old = row[0] if row else None
    new_inv = _ulid_like()
    db.conn.execute("UPDATE open_streams SET invoke_id=?, heartbeat_at=datetime('now'), lease_until=datetime('now','+10 seconds') WHERE thread_id=?",
                    (new_inv, thread_id))
    if old:
        db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.interrupt', payload={"reason": reason, "old_invoke_id": old, "new_invoke_id": new_inv})
    return old


def pause_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> None:
    db.conn.execute("UPDATE threads SET status='paused' WHERE thread_id=?", (thread_id,))
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.pause', payload={"reason": reason})


def resume_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> None:
    db.conn.execute("UPDATE threads SET status='active' WHERE thread_id=?", (thread_id,))
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.resume', payload={"reason": reason})
