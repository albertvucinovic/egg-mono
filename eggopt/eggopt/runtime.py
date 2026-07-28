from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from eggflow import FlowExecutor, TaskStore
from eggthreads import ThreadsDB

from .context import _bind_operation_runtime
from .identity import digest_payload


@dataclass
class Runtime:
    """One run-owned Eggflow store and Eggthreads tree."""

    root: Path
    store: TaskStore
    flow: FlowExecutor
    threads: ThreadsDB
    runtime_key: str

    @classmethod
    def open(cls, root: str | Path) -> Runtime:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        egg = root / ".egg"
        egg.mkdir(exist_ok=True)
        store = TaskStore(str(egg / "flow.db"))
        threads = ThreadsDB(egg / "threads.sqlite")
        threads.init_schema()
        runtime_key = digest_payload("eggopt.runtime.v1", {"root": str(root)})
        _bind_operation_runtime(runtime_key, threads)
        return cls(root, store, FlowExecutor(store), threads, runtime_key)

    def close(self) -> None:
        self.threads.close()
        self.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def sync(awaitable: Any, *, operation: str) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError(f"{operation} cannot run inside an active asyncio loop")


__all__ = ["Runtime", "sync"]
