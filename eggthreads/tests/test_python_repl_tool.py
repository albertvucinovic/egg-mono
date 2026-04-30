from __future__ import annotations

from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_execute_python_repl_memory_provider_persists_state(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out1 = ts.execute_python_repl(db, parent, "x = 41")
    assert "ERROR" not in out1

    out2 = ts.execute_python_repl(db, parent, "x + 1")
    assert "42" in out2

    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None
    assert runtime.runtime_thread_id in ts.list_children_ids(db, parent)


def test_execute_python_repl_auto_creates_session_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_SESSION_PROVIDER", "memory")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_python_repl(db, parent, "1 + 1")
    assert "2" in out
    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None
    cfg = ts.get_thread_session_config(db, runtime.runtime_thread_id)
    assert cfg.enabled is True
    assert cfg.provider == "memory"


def test_execute_python_repl_reports_disabled_auto_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_AUTO_SESSION", "0")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_python_repl(db, parent, "1 + 1")
    assert "auto-create is disabled" in out


def test_python_repl_tool_registered():
    tools = ts.create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "python_repl" in names
