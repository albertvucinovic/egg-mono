from __future__ import annotations

"""Thread-tree paths for long tool output stashes."""

from pathlib import Path
from typing import List


def safe_thread_dir_name(thread_id: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in str(thread_id or 'thread'))
    return safe or 'thread'


def thread_ancestry(db, thread_id: str) -> List[str]:
    """Return root-to-thread ids for ``thread_id`` using the children table."""

    chain: List[str] = []
    cur = str(thread_id or "")
    seen: set[str] = set()
    for _ in range(2048):
        if not cur or cur in seen:
            break
        seen.add(cur)
        chain.append(cur)
        try:
            row = db.conn.execute("SELECT parent_id FROM children WHERE child_id=?", (cur,)).fetchone()
            cur = row[0] if row and row[0] else ""
        except Exception:
            cur = ""
    chain.reverse()
    return chain or [safe_thread_dir_name(thread_id)]


def thread_output_relative_dir(db, thread_id: str) -> Path:
    """Return ``.egg_outputs/<root>/.../<thread>`` for a thread."""

    parts = [safe_thread_dir_name(tid) for tid in thread_ancestry(db, thread_id)]
    return Path(".egg_outputs", *parts)


def thread_output_dir(db, workspace: Path, thread_id: str) -> Path:
    return workspace.resolve() / thread_output_relative_dir(db, thread_id)
