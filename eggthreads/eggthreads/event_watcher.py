from __future__ import annotations

import asyncio
from typing import AsyncIterator, List

from .db import ThreadsDB


class EventWatcher:
    """Polls events for a thread and yields new ones since a given sequence.

    Usage:
      watcher = EventWatcher(db, thread_id)
      async for batch in watcher.aiter():
          ...
    """

    def __init__(self, db: ThreadsDB, thread_id: str, after_seq: int = -1,
                 poll_sec: float = 0.05, max_backoff: float = 0.2):
        self.db = db
        self.thread_id = thread_id
        self.after_seq = after_seq
        self.poll_sec = poll_sec
        self.max_backoff = max_backoff

    async def aiter(self) -> AsyncIterator[List]:
        idle = 0
        while True:
            cur = self.db.conn.execute(
                "SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                (self.thread_id, self.after_seq)
            )
            rows = cur.fetchall()
            if rows:
                self.after_seq = rows[-1]["event_seq"]
                idle = 0
                yield rows
                # During active streaming, poll immediately without sleeping
                # to stay responsive. Only sleep after idle iterations.
                continue
            else:
                # Lightweight backoff to reduce CPU when idle, but stay responsive
                # during active streaming.
                idle = min(idle + 1, 4)
            # Stay responsive for the first few idle cycles, then back off gently.
            if idle < 2:
                delay = self.poll_sec
            else:
                delay = min(self.poll_sec * (idle + 1), self.max_backoff)
            await asyncio.sleep(delay)
