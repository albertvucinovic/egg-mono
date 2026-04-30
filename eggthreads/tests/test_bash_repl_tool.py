from __future__ import annotations

from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_execute_bash_repl_memory_provider_persists_environment(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out1 = ts.execute_bash_repl(db, parent, "export FOO=bar")
    assert "ERROR" not in out1

    out2 = ts.execute_bash_repl(db, parent, "echo $FOO")
    assert "bar" in out2

    runtime = ts.find_runtime_thread(db, parent, language="bash")
    assert runtime is not None
    assert runtime.runtime_thread_id in ts.list_children_ids(db, parent)


def test_execute_bash_repl_requires_enabled_session(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_bash_repl(db, parent, "echo hi")
    assert "session is not enabled" in out


def test_bash_repl_tool_registered():
    tools = ts.create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "bash_repl" in names


def test_bash_repl_memory_can_use_eval_token_env(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out = ts.execute_bash_repl(db, parent, "test -n \"$EGG_EVAL_TOKEN\" && echo token-present")
    assert "token-present" in out
