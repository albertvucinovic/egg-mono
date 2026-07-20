"""Optional cached Eggthreads run roots.

Persistent Solver/Execution composition lives in :mod:`eggopt.solver_execution`.
"""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass

from eggflow import Task
from eggthreads import (
    ThreadsDB,
    create_child_thread,
    create_root_thread,
    set_thread_tools_enabled,
)

_ROOTS_SCHEMA = b"eggopt.CreateRunRoots:v1\0"

__all__ = ["CreateRunRoots", "RunRoots"]


@dataclass(frozen=True)
class RunRoots:
    """Authoritative cached references to one study and strategy hierarchy."""

    study_thread_id: str
    strategy_thread_id: str

    def __post_init__(self) -> None:
        _nonempty(self.study_thread_id, "study_thread_id")
        _nonempty(self.strategy_thread_id, "strategy_thread_id")


@dataclass
class CreateRunRoots(Task):
    """Create one study root and strategy child without scanning by name."""

    threads_db_path: str
    study_name: str
    strategy_name: str

    def __post_init__(self) -> None:
        _nonempty(self.threads_db_path, "threads_db_path")
        _nonempty(self.study_name, "study_name")
        _nonempty(self.strategy_name, "strategy_name")

    def get_cache_key(self) -> str:
        return hashlib.sha256(
            _ROOTS_SCHEMA
            + pickle.dumps(
                (self.threads_db_path, self.study_name, self.strategy_name),
                protocol=5,
            )
        ).hexdigest()

    def run(self) -> RunRoots:
        db = ThreadsDB(self.threads_db_path)
        try:
            db.init_schema()
            study = create_root_thread(db, name=self.study_name)
            set_thread_tools_enabled(db, study, False)
            strategy = create_child_thread(db, study, name=self.strategy_name)
            set_thread_tools_enabled(db, strategy, False)
            return RunRoots(study, strategy)
        finally:
            db.conn.close()


def _nonempty(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
