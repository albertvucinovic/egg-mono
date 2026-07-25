from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eggflow import FlowExecutor, Task, TaskStore
from eggthreads import ThreadsDB, create_child_thread, create_root_thread

from ..context import _bind_evaluation_runtime
from ..identity import digest_payload


@dataclass
class Runtime:
    root: Path
    store: TaskStore
    flow: FlowExecutor
    threads: ThreadsDB
    study_id: str
    mutation_id: str
    runtime_key: str

    @classmethod
    def open(cls, root: str | Path) -> Runtime:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        egg = root / ".egg"
        egg.mkdir(exist_ok=True)
        store = TaskStore(str(egg / "flow.db"))
        flow = FlowExecutor(store)
        threads = ThreadsDB(egg / "threads.sqlite")
        threads.init_schema()
        study_id, mutation_id = _sync(flow.run(_CreateStudy(threads)))
        runtime_key = digest_payload("eggopt.gepa.runtime.v1", {"root": str(root)})
        _bind_evaluation_runtime(runtime_key, threads)
        return cls(root, store, flow, threads, study_id, mutation_id, runtime_key)

    def close(self) -> None:
        self.threads.close()
        self.store.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class _CreateStudy(Task):
    threads: ThreadsDB

    def get_cache_key(self) -> str:
        return digest_payload("eggopt.gepa.create-study.v1", {})

    def run(self) -> tuple[str, str]:
        study_id = create_root_thread(self.threads, name="GEPA")
        validation_id = create_child_thread(
            self.threads,
            study_id,
            name="Validation",
            inherit_tools_config=False,
        )
        mutation_id = create_child_thread(
            self.threads,
            validation_id,
            name="Mutation",
            inherit_tools_config=False,
        )
        return study_id, mutation_id


def _sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("GEPA cannot run inside an active asyncio loop")


__all__ = ["Runtime"]
