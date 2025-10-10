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

    def __init__(self, db: ThreadsDB, thread_id: str, after_seq: int = -1, poll_sec: float = 0.05):
        self.db = db
        self.thread_id = thread_id
        self.after_seq = after_seq
        self.poll_sec = poll_sec

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
            else:
                # lightweight backoff to reduce CPU when idle, but still responsive
                idle = min(idle + 1, 5)
            await asyncio.sleep(self.poll_sec if idle < 3 else self.poll_sec * 2)
