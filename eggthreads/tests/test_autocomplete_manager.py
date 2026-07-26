from __future__ import annotations

import threading
from pathlib import Path

import eggthreads as ts
import eggthreads.autocomplete_manager as manager_module


def test_manager_coalesces_to_one_running_and_latest_pending(tmp_path: Path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    first = ts.create_root_thread(db, "first")
    second = ts.create_root_thread(db, "second")
    third = ts.create_root_thread(db, "third")
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_build(worker_db, thread_id):
        calls.append(thread_id)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return manager_module.AutocompleteBuildResult("ready", thread_id)

    monkeypatch.setattr(manager_module, "catch_up_autocomplete_catalog", blocking_build)
    manager = ts.AutocompleteSidecarManager(db.path)
    try:
        assert manager.request_build(first)
        assert started.wait(5)
        assert manager.request_build(second)
        assert manager.request_build(third)
        release.set()
        for _ in range(100):
            with manager._lock:
                done = manager._future is None and manager._pending_thread_id is None
            if done:
                break
            threading.Event().wait(0.01)
        assert calls == [first, third]
    finally:
        manager.close(wait=True)


def test_manager_builds_using_worker_owned_database_connection(tmp_path: Path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, "root")
    ts.append_message(db, thread_id, "user", "full history")
    manager = ts.AutocompleteSidecarManager(db.path)
    try:
        assert manager.request_build(thread_id)
        for _ in range(500):
            if ts.autocomplete_catalog_status(db, thread_id).state == "ready":
                break
            threading.Event().wait(0.01)
        assert ts.autocomplete_catalog_status(db, thread_id).state == "ready"
        assert ts.query_autocomplete_terms(db, thread_id, "ful") == ("ready", ("full",))
    finally:
        manager.close(wait=True)
