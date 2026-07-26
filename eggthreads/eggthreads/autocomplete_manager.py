from __future__ import annotations

"""Process-local scheduling for cross-process-owned projection sidecar builds."""

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from .autocomplete_sidecar import AutocompleteBuildResult, catch_up_autocomplete_catalog
from .db import ThreadsDB


class AutocompleteSidecarManager:
    """Bounded background builder; the sidecar lease provides cross-process authority."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="egg-autocomplete")
        self._lock = threading.Lock()
        self._future: Optional[Future[AutocompleteBuildResult]] = None
        self._pending_thread_id: Optional[str] = None
        self._closed = False

    def request_build(self, thread_id: str) -> bool:
        """Schedule the latest requested thread, without creating an unbounded queue."""

        normalized = str(thread_id or "").strip()
        if not normalized:
            return False
        with self._lock:
            if self._closed:
                return False
            if self._future is not None and not self._future.done():
                self._pending_thread_id = normalized
                return True
            self._submit_locked(normalized)
            return True

    def _submit_locked(self, thread_id: str) -> None:
        future = self._executor.submit(self._build, thread_id)
        self._future = future
        future.add_done_callback(self._finished)

    def _build(self, thread_id: str) -> AutocompleteBuildResult:
        db = ThreadsDB(self.db_path)
        try:
            return catch_up_autocomplete_catalog(db, thread_id)
        finally:
            db.close()

    def _finished(self, future: Future[AutocompleteBuildResult]) -> None:
        with self._lock:
            if self._future is future:
                self._future = None
            pending = self._pending_thread_id
            self._pending_thread_id = None
            if pending and not self._closed:
                self._submit_locked(pending)

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            self._pending_thread_id = None
        self._executor.shutdown(wait=wait, cancel_futures=True)
