from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from eggflow import Task, TaskError
from eggopt import current_operation, run_operation
from eggthreads import ThreadsDB, create_root_thread, list_root_threads


@dataclass
class ObserveOperation(Task):
    calls: list[dict]

    def get_cache_key(self) -> str:
        return "test.observe-operation.v1"

    def run(self):
        value = dict(current_operation())
        self.calls.append(value)
        return value


def test_run_operation_exposes_context_and_resumes_cached_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    first = run_operation(
        ObserveOperation(calls),
        identity={"source": "news", "version": 1},
        name="News extraction",
        run_dir="run",
    )
    second = run_operation(
        ObserveOperation(calls),
        identity={"version": 1, "source": "news"},
        name="News extraction",
        run_dir="run",
    )

    expected_outer = str((tmp_path / "run" / "workspace").resolve())
    assert first == second
    assert len(calls) == 1
    assert first["operation_thread_id"] == first["evaluation_thread_id"]
    assert first["outer_context"] == expected_outer
    assert first["inner_context"] == str(Path(expected_outer) / "innerContext")
    assert (Path(first["inner_context"])).is_dir()

    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        roots = list_root_threads(db)
        assert roots == [first["operation_thread_id"]]
        assert db.get_thread(roots[0]).name == "News extraction"
    finally:
        db.close()


def test_run_operation_identity_scopes_cached_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    for version in (1, 2):
        run_operation(
            ObserveOperation(calls),
            identity={"version": version},
            run_dir="run",
        )

    assert len(calls) == 2


def test_run_operation_validates_public_arguments_before_opening_runtime(tmp_path):
    with pytest.raises(TypeError, match="Eggflow Task"):
        run_operation(object(), identity={"version": 1}, run_dir=tmp_path / "run")
    with pytest.raises(TypeError, match="finite JSON"):
        run_operation(
            ObserveOperation([]), identity={"bad": float("nan")}, run_dir=tmp_path / "run"
        )
    with pytest.raises(ValueError, match="non-empty"):
        run_operation(ObserveOperation([]), identity={}, name="", run_dir=tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_run_operation_rejects_ambiguous_named_roots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "run"
    db = ThreadsDB(run_dir / ".egg" / "threads.sqlite")
    db.init_schema()
    try:
        create_root_thread(db, name="Duplicate")
        create_root_thread(db, name="Duplicate")
    finally:
        db.close()

    with pytest.raises(TaskError, match="multiple 'Duplicate' root threads"):
        run_operation(
            ObserveOperation([]), identity={"version": 1}, name="Duplicate", run_dir=run_dir
        )
