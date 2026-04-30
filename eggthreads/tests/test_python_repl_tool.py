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
    assert "session_status" in names
    assert "session_reset" in names
    assert "session_stop" in names


def test_shared_session_uses_separate_repl_channel_by_default(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_repl=False)
    child = ts.create_child_thread(db, parent, name="child")
    ts.set_thread_session_config(
        db,
        child,
        enabled=True,
        provider="memory",
        share="session",
        session_id=sid,
        owner_thread_id=parent,
    )

    assert "ERROR" not in ts.execute_python_repl(db, parent, "x = 'parent'")
    child_out = ts.execute_python_repl(db, child, "globals().get('x', 'missing')")
    assert "missing" in child_out


def test_share_repl_true_shares_interpreter_channel(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_repl=True)
    child = ts.create_child_thread(db, parent, name="child")
    ts.set_thread_session_config(
        db,
        child,
        enabled=True,
        provider="memory",
        share="session",
        session_id=sid,
        owner_thread_id=parent,
        share_repl=True,
    )

    assert "ERROR" not in ts.execute_python_repl(db, parent, "shared_value = 99")
    child_out = ts.execute_python_repl(db, child, "shared_value")
    assert "99" in child_out


def test_direct_drive_reports_error_inside_running_loop(tmp_path):
    import asyncio

    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    async def run():
        return ts.execute_python_repl(db, parent, "1 + 1", drive_runtime_tools=True)

    out = asyncio.run(run())
    assert "drive_runtime_tools=True cannot be used" in out
