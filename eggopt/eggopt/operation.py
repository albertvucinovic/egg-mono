from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, keyed
from eggthreads import ThreadsDB, create_root_thread, list_root_threads

from .context import _operation_scope
from .identity import canonical_value, digest_payload
from .runtime import Runtime, sync

_DEFAULT_RUN_DIR = ".eggopt/operation"


def run_operation(
    task: Task,
    *,
    identity: Any,
    name: str = "Operation",
    run_dir: str | Path = _DEFAULT_RUN_DIR,
) -> Any:
    """Run or resume one standalone Eggflow task as an Eggopt operation.

    The runner owns the generic bootstrap that GEPA and Physics otherwise provide:
    it opens ``run_dir/.egg`` for the Eggflow cache and Eggthreads tree, creates or
    reuses one named root operation thread, creates ``workspace/innerContext``, and
    exposes those public paths and the operation thread through
    :func:`current_operation` while ``task`` runs. ``identity`` must be a finite
    JSON value; changing it creates a distinct durable task namespace without
    discarding the run directory's previously cached work. Reusing ``run_dir``,
    ``name``, and ``identity`` therefore resumes the same operation.

    Use this boundary for finite standalone compositions such as ``ActorCritic``
    rather than importing Eggopt's private context or runtime helpers. The task
    must be an :class:`eggflow.Task`, and the synchronous helper cannot be called
    from an already-running asyncio event loop.
    """

    if not isinstance(task, Task):
        raise TypeError("task must be an Eggflow Task")
    operation_name = _operation_name(name)
    operation_identity = canonical_value(identity, what="operation identity")
    with Runtime.open(run_dir) as runtime:
        operation_id = sync(
            runtime.flow.run(_EnsureOperationThread(runtime.threads, operation_name)),
            operation="run_operation",
        )
        return sync(
            runtime.flow.run(
                _RunOperation(
                    task,
                    runtime.runtime_key,
                    str(runtime.root),
                    operation_id,
                    operation_name,
                    operation_identity,
                )
            ),
            operation="run_operation",
        )


@dataclass
class _EnsureOperationThread(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    name: str

    def get_cache_key(self) -> str:
        return digest_payload("eggopt.operation.ensure-thread.v1", {"name": self.name})

    def run(self) -> str:
        matches = [
            thread_id
            for thread_id in list_root_threads(self.threads)
            if self.threads.get_thread(thread_id).name == self.name
        ]
        if len(matches) > 1:
            raise RuntimeError(f"run directory has multiple {self.name!r} root threads")
        return matches[0] if matches else create_root_thread(self.threads, name=self.name)


@dataclass
class _RunOperation(Task):
    cacheable = False

    task: Task = field(repr=False, compare=False)
    runtime_key: str
    run_dir: str
    operation_id: str
    name: str
    identity: Any

    def run(self):
        outer = Path(self.run_dir) / "workspace"
        inner = outer / "innerContext"
        inner.mkdir(parents=True, exist_ok=True)
        operation_key = digest_payload(
            "eggopt.operation.v1", {"name": self.name, "identity": self.identity}
        )
        context = {
            "operation_thread_id": self.operation_id,
            # ActorCritic's historical field name remains its composition port.
            "evaluation_thread_id": self.operation_id,
            "outer_context": str(outer),
            "inner_context": str(inner),
            "_runtime_key": self.runtime_key,
            "_evaluation_key": operation_key,
            "_context_limit": None,
        }
        with _operation_scope(context):
            return (yield keyed(self.task, operation_key))


def _operation_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if name != name.strip():
        raise ValueError("name must not have leading or trailing whitespace")
    return name


__all__ = ["run_operation"]
