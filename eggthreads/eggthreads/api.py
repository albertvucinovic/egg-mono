from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .db import ThreadsDB, ThreadRow
from .snapshot import SnapshotBuilder
from .runner import ThreadRunner, RunnerConfig


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

    # Inherit sandbox configuration from the parent so the child's tool
    # execution policy reflects the currently effective parent policy.
    try:
        from .sandbox import get_thread_sandbox_config, set_thread_sandbox_config

        sb = get_thread_sandbox_config(db, parent_id)
        set_thread_sandbox_config(
            db,
            tid,
            enabled=bool(getattr(sb, 'enabled', False)),
            config_name=str(getattr(sb, 'config_name', 'default.json')),
            reason='inherit from parent (create_child_thread)',
        )
    except Exception:
        pass

    return tid


def duplicate_thread(db: ThreadsDB, source_thread_id: str, name: Optional[str] = None) -> str:
    """Duplicate a thread's event log into a new root thread.

    This creates a new *root* thread whose events and snapshot are a
    copy of ``source_thread_id`` at the time of invocation. The new
    thread shares no open stream with the original (no rows are added
    to ``open_streams``) but otherwise has identical history: all
    ``msg.create``, ``stream.*``, and ``tool_call.*`` events are
    replayed with fresh event_ids, preserving msg_id and invoke_id so
    that runner/actionable semantics (RA1/RA2/RA3, tool states, etc.)
    behave as if the thread had been executed separately.

    The duplicate is intended as a "checkpoint" copy: a frozen backup
    of the conversation that can be inspected or resumed independently
    of the original.
    """

    # Look up source metadata to derive a sensible name and model.
    src = db.get_thread(source_thread_id)
    if not src:
        raise ValueError(f"Source thread not found: {source_thread_id}")

    base_name = src.name or src.short_recap or "Thread"
    new_name = name or f"{base_name} [copy]"

    # Always create the duplicate as a new root thread so it is
    # independent from any existing parent/children relationships.
    new_tid = _ulid_like()
    db.create_thread(
        thread_id=new_tid,
        name=new_name,
        parent_id=None,
        initial_model_key=src.initial_model_key,
        depth=0,
    )

    # Replay all events from the source thread into the new thread with
    # fresh event_ids. We keep msg_id/invoke_id/chunk_seq so that the
    # duplicate's internal state (e.g. tool call history, stream
    # boundaries) mirrors the original.
    import json as _json

    cur = db.conn.execute(
        "SELECT type, msg_id, invoke_id, chunk_seq, payload_json FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (source_thread_id,),
    )
    rows = cur.fetchall()
    for ev_type, msg_id, invoke_id, chunk_seq, pj in rows:
        # Skip low-level streaming events; snapshots and runner logic
        # derive their state from msg.create and tool_call.* events.
        # Copying stream.open/delta/close as-is would also risk
        # violating the UNIQUE(invoke_id, chunk_seq) constraint on
        # stream.delta rows.
        if ev_type in ('stream.open', 'stream.delta', 'stream.close'):
            continue
        try:
            payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_=ev_type,
            payload=payload,
            msg_id=msg_id,
            invoke_id=invoke_id,
            chunk_seq=chunk_seq,
        )

    # Build a fresh snapshot for the duplicate so UIs and runners see a
    # consistent cached view of messages.
    create_snapshot(db, new_tid)
    return new_tid


def append_message(db: ThreadsDB, thread_id: str, role: str, content: str, extra: Optional[Dict[str, Any]] = None) -> str:
    """Append a user/assistant/system message to a thread.

    This helper is intentionally thin: policy decisions about which
    messages are sent to the provider (e.g. via ``no_api``) are handled
    elsewhere, primarily in ``thread_state`` / ``discover_runner_actionable``
    and ``ThreadRunner._sanitize_messages_for_api``.
    """

    payload_extra: Dict[str, Any] = dict(extra or {})

    msg_id = _ulid_like()
    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_='msg.create',
        payload={"role": role, "content": content, **payload_extra},
        msg_id=msg_id,
    )
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


def delete_thread(db: ThreadsDB, thread_id: str) -> None:
    """Delete a thread and cascade related rows via foreign keys.

    Removes the thread from threads; ON DELETE CASCADE removes
    - children rows that reference it (as parent or child)
    - events rows for the thread
    - open_streams row for the thread
    """
    db.conn.execute("DELETE FROM threads WHERE thread_id=?", (thread_id,))


def is_thread_runnable(db: ThreadsDB, thread_id: str) -> bool:
    """Public API to check if a thread is runnable.

    This now delegates to discover_runner_actionable so that the
    ThreadRunner and external callers share the same notion of
    runnable work (RA1/RA2/RA3).
    """
    from .tool_state import discover_runner_actionable_cached

    return discover_runner_actionable_cached(db, thread_id) is not None


# --------- Query helpers (expose common SQL as API) -------------------------
def list_threads(db: ThreadsDB) -> list[ThreadRow]:
    try:
        cur = db.conn.execute("SELECT * FROM threads")
        rows = [ThreadRow(**dict(r)) for r in cur.fetchall()]
    except Exception:
        rows = []
    return rows


def list_root_threads(db: ThreadsDB) -> list[str]:
    try:
        cur = db.conn.execute("SELECT thread_id FROM threads WHERE thread_id NOT IN (SELECT child_id FROM children)")
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def get_parent(db: ThreadsDB, child_id: str) -> Optional[str]:
    try:
        row = db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (child_id,)).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def list_children_with_meta(db: ThreadsDB, parent_id: str) -> list[tuple[str, str, str, str]]:
    """Return list of (child_id, name, short_recap, created_at) for a parent."""
    try:
        cur = db.conn.execute(
            "SELECT c.child_id, t.name, t.short_recap, t.created_at FROM children c JOIN threads t ON t.thread_id=c.child_id WHERE c.parent_id=? ORDER BY t.created_at ASC",
            (parent_id,)
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    except Exception:
        return []


def list_children_ids(db: ThreadsDB, parent_id: str) -> list[str]:
    try:
        cur = db.conn.execute("SELECT child_id FROM children WHERE parent_id=?", (parent_id,))
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def current_open_invoke(db: ThreadsDB, thread_id: str) -> Optional[str]:
    try:
        row = db.current_open(thread_id)
        return row["invoke_id"] if row else None
    except Exception:
        return None


def interrupt_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> Optional[str]:
    """Hard-preempt current step by dropping the current lease.

    Writers that gate on (thread_id, invoke_id) will fail on the next
    heartbeat because the open_streams row for that (thread, invoke)
    no longer exists. A new runner can immediately acquire a fresh
    lease for the thread.
    """
    cur = db.conn.execute("SELECT invoke_id FROM open_streams WHERE thread_id=?", (thread_id,))
    row = cur.fetchone()
    old = row[0] if row else None
    new_inv = _ulid_like()
    if old:
        # Remove the existing open_streams row so that:
        #  - the current runner loses its lease (heartbeat will fail), and
        #  - future runners can immediately acquire a new lease.
        try:
            db.conn.execute("DELETE FROM open_streams WHERE thread_id=? AND invoke_id=?", (thread_id, old))
        except Exception:
            pass
        db.append_event(
            event_id=_ulid_like(),
            thread_id=thread_id,
            type_='control.interrupt',
            payload={"reason": reason, "old_invoke_id": old, "new_invoke_id": new_inv},
        )
    return old


def pause_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> None:
    db.conn.execute("UPDATE threads SET status='paused' WHERE thread_id=?", (thread_id,))
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.pause', payload={"reason": reason})


def resume_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> None:
    db.conn.execute("UPDATE threads SET status='active' WHERE thread_id=?", (thread_id,))
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.resume', payload={"reason": reason})


def set_thread_model(db: ThreadsDB, thread_id: str, model_key: str, reason: str = 'user') -> None:
    """Append a model.switch event to a thread.

    This is the authoritative record of model selection for a thread.
    The ThreadRunner and UIs should not infer the active model from
    message payloads; they should instead call current_thread_model(),
    which uses these events.
    """
    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_='model.switch',
        payload={
            'model_key': model_key,
            'reason': reason,
        },
    )


def current_thread_model(db: ThreadsDB, thread_id: str) -> Optional[str]:
    """Return the effective model for a thread.

    Precedence:
      1. Most recent model.switch event (by event_seq) in this thread
         whose payload contains a non-empty model_key.
      2. threads.initial_model_key for this thread, if set and non-empty.
      3. None (caller may then fall back to the LLM client's default).

    This helper must be the single source of truth for determining the
    active model for a thread in eggthreads-based applications.
    """
    model_key: Optional[str] = None
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch' ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is not None:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            mk = payload.get('model_key')
            if isinstance(mk, str) and mk.strip():
                model_key = mk.strip()
    except Exception:
        model_key = None

    if not model_key:
        try:
            th = db.get_thread(thread_id)
        except Exception:
            th = None
        imk = getattr(th, 'initial_model_key', None) if th else None
        if isinstance(imk, str) and imk.strip():
            model_key = imk.strip()

    return model_key
