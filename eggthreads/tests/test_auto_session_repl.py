from __future__ import annotations

from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_manual_parent_session_still_overrides_auto_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_SESSION_PROVIDER", "memory")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, provider="memory")

    out = ts.execute_python_repl(db, parent, "10 + 5")
    assert "15" in out

    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None
    cfg = ts.get_thread_session_config(db, runtime.runtime_thread_id)
    # Runtime inherits the parent's explicit session rather than creating a new one.
    assert cfg.session_id == sid
    assert cfg.source == f"event:{parent}"


def test_auto_session_lifecycle_event(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_SESSION_PROVIDER", "memory")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    ts.execute_python_repl(db, parent, "1")
    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None

    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' AND json_extract(payload_json, '$.action')='auto_created'",
        (runtime.runtime_thread_id,),
    ).fetchone()
    assert row is not None
