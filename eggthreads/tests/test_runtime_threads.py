from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_get_or_create_runtime_thread_creates_child_and_config(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    runtime = ts.get_or_create_runtime_thread(db, parent, language="python", name="default", reason="test")

    assert runtime in ts.list_children_ids(db, parent)
    row = db.get_thread(runtime)
    assert row is not None
    assert row.name == "@runtime:python"

    cfg = ts.find_runtime_thread(db, parent, language="python", name="default")
    assert cfg is not None
    assert cfg.runtime_thread_id == runtime
    assert cfg.parent_thread_id == parent
    assert cfg.language == "python"
    assert cfg.name == "default"

    tools_cfg = ts.get_thread_tools_config(db, runtime)
    assert tools_cfg.llm_tools_enabled is True

    # Runtime marker exists on the runtime thread itself.
    marker = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='runtime.thread'",
        (runtime,),
    ).fetchone()
    assert marker is not None
    payload = json.loads(marker[0])
    assert payload["parent_thread_id"] == parent
    assert payload["language"] == "python"


def test_get_or_create_runtime_thread_reuses_existing(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    first = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")
    second = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")

    assert first == second
    assert ts.list_children_ids(db, parent).count(first) == 1


def test_runtime_thread_label_includes_non_default_name():
    assert ts.runtime_thread_label(language="python", name="default") == "@runtime:python"
    assert ts.runtime_thread_label(language="bash", name="analysis") == "@runtime:bash:analysis"


def test_find_runtime_thread_ignores_stale_deleted_runtime(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")

    ts.delete_thread(db, runtime)
    assert ts.find_runtime_thread(db, parent, language="python", name="default") is None

    replacement = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")
    assert replacement != runtime
    assert replacement in ts.list_children_ids(db, parent)


def test_find_runtime_thread_repairs_missing_child_link(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.create_root_thread(db, name="@runtime:python")

    ts.append_runtime_config(db, parent, runtime, language="python", name="default", reason="legacy")
    assert runtime not in ts.list_children_ids(db, parent)

    cfg = ts.find_runtime_thread(db, parent, language="python", name="default")

    assert cfg is not None
    assert cfg.runtime_thread_id == runtime
    assert runtime in ts.list_children_ids(db, parent)
    assert db.get_thread(runtime).depth == db.get_thread(parent).depth + 1


def test_python_repl_tool_uses_runner_context_for_runtime_parent(tmp_path, monkeypatch):
    from eggthreads.tools import create_default_tools

    monkeypatch.setenv("EGG_RLM_SESSION_PROVIDER", "memory")
    monkeypatch.setenv("EGG_ALLOW_MEMORY_SESSION_WITH_SANDBOX", "1")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    tools = create_default_tools()
    out = tools.execute(
        "python_repl",
        {"code": "1 + 1", "_thread_id": "wrong-thread-id"},
        db=db,
        thread_id=parent,
        preserve_tool_result=True,
    )

    assert "2" in out
    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None
    assert runtime.runtime_thread_id in ts.list_children_ids(db, parent)
    assert ts.find_runtime_thread(db, "wrong-thread-id", language="python") is None


def _legacy_runtime_fixture(db):
    root = ts.create_root_thread(db, name="root")
    session_id = ts.set_thread_session_config(
        db,
        root,
        enabled=True,
        provider="docker",
        reason="auto:python_repl:python",
    )
    ordinary = ts.create_child_thread(db, root, name="ordinary")
    runtime = ts.create_root_thread(db, name="@runtime:python")
    db.append_event(
        event_id="runtime-marker-legacy",
        thread_id=runtime,
        type_="runtime.thread",
        payload={"parent_thread_id": ordinary, "language": "python", "name": "default"},
    )
    ts.append_runtime_config(
        db, ordinary, runtime,
        language="python", name="default", reason="legacy",
    )
    return root, ordinary, runtime, session_id


def test_find_runtime_thread_legacy_repair_holds_inherited_session_guard(tmp_path):
    import threading
    import time

    db = _make_db(tmp_path)
    _root, ordinary, runtime, session_id = _legacy_runtime_fixture(db)
    repaired = threading.Event()

    def find_and_repair():
        worker_db = ts.ThreadsDB(db.path)
        try:
            found = ts.find_runtime_thread(worker_db, ordinary, language="python")
            assert found is not None and found.runtime_thread_id == runtime
            repaired.set()
        finally:
            worker_db.conn.close()

    from eggthreads import session

    with session._session_activity_guard(db, session_id):
        worker = threading.Thread(target=find_and_repair)
        worker.start()
        time.sleep(0.05)
        assert not repaired.is_set()
        assert runtime not in ts.list_children_ids(db, ordinary)
    worker.join(2)

    assert repaired.is_set()
    assert runtime in ts.list_children_ids(db, ordinary)


def test_reaper_scan_blocks_legacy_find_repair_before_stop(tmp_path, monkeypatch):
    import threading
    import time

    from eggthreads import session

    db = _make_db(tmp_path)
    root, ordinary, runtime, session_id = _legacy_runtime_fixture(db)
    cfg = ts.get_thread_session_config(db, root)
    scan_complete = threading.Event()
    allow_stop = threading.Event()
    repaired = threading.Event()
    stopped = []
    original_refs = session._session_reference_thread_ids

    def refs_then_pause(*args):
        result = original_refs(*args)
        scan_complete.set()
        return result

    def ready_status(*_args):
        return session.SessionStatus(
            True, "docker", session_id, "ready",
            container_name="container", last_activity=1.0,
            heartbeat_at=100.0, daemon_generation="generation",
        )

    def stop_captured(_db, _thread, captured, *, reason):
        assert allow_stop.wait(2)
        stopped.append(captured.session_id)
        return session.SessionStatus(True, "docker", captured.session_id, "stopped")

    monkeypatch.setattr(session, "_session_reference_thread_ids", refs_then_pause)
    monkeypatch.setattr(session, "_session_status_for_config", ready_status)
    monkeypatch.setattr(session, "_stop_captured_session", stop_captured)
    result = []

    def reap():
        worker_db = ts.ThreadsDB(db.path)
        try:
            result.extend(ts.reap_idle_auto_docker_sessions(worker_db, idle_timeout_sec=10, now=100.0))
        finally:
            worker_db.conn.close()

    reaper = threading.Thread(target=reap)
    reaper.start()
    assert scan_complete.wait(1)

    def find_and_repair():
        worker_db = ts.ThreadsDB(db.path)
        try:
            found = ts.find_runtime_thread(worker_db, ordinary, language="python")
            assert found is not None
            repaired.set()
        finally:
            worker_db.conn.close()

    finder = threading.Thread(target=find_and_repair)
    finder.start()
    time.sleep(0.05)
    assert not repaired.is_set()
    assert runtime not in ts.list_children_ids(db, ordinary)
    allow_stop.set()
    reaper.join(2)
    finder.join(2)

    assert stopped == [session_id]
    assert result[0]["reclaimed"] is True
    assert repaired.is_set()
    assert runtime in ts.list_children_ids(db, ordinary)
    assert cfg.session_id == session_id
