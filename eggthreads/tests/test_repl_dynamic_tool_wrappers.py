from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import eggthreads as ts
import eggthreads.session as session


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_memory_repl_generated_tool_wrapper_supports_from_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["python_repl"])

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import python_repl\n"
        "print(python_repl(code='pass', _thread_id=''))",
        timeout_sec=5,
        drive_runtime_tools=True,
    )

    assert "Error: python_repl requires thread context." in out
    assert "ImportError" not in out


def test_docker_runtime_eggtools_generated_wrapper_supports_from_import(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session._write_runtime_files(runtime_dir)
    eggtools_path = runtime_dir / "eggtools.py"

    old_module = sys.modules.pop("eggtools", None)
    try:
        spec = importlib.util.spec_from_file_location("eggtools", eggtools_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["eggtools"] = module
        spec.loader.exec_module(module)

        from eggtools import python_repl  # type: ignore

        assert callable(python_repl)
        assert python_repl.__name__ == "python_repl"
        try:
            python_repl()
        except TypeError as e:
            assert "code" in str(e)
        else:
            raise AssertionError("generated wrapper should require schema-required args")
    finally:
        sys.modules.pop("eggtools", None)
        if old_module is not None:
            sys.modules["eggtools"] = old_module
