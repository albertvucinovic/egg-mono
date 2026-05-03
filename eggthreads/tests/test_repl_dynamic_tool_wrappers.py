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
    ts.set_thread_tool_allowlist(db, runtime, ["replace_between"])
    (tmp_path / "sample.txt").write_text("hello old bye", encoding="utf-8")

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import replace_between\n"
        "print(replace_between(file_path='sample.txt', start_text='old', end_text=' bye', new_text='new bye'))",
        bridge_timeout_sec=5,
        drive_runtime_tools=True,
    )

    assert "Success: replaced region." in out
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "hello new bye"
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

        from eggtools import replace_between  # type: ignore

        assert callable(replace_between)
        assert replace_between.__name__ == "replace_between"
        try:
            replace_between({"file_path": "x"})
        except TypeError as e:
            assert "positional" in str(e)
        else:
            raise AssertionError("generated wrapper should not accept dict positional calls")
    finally:
        sys.modules.pop("eggtools", None)
        if old_module is not None:
            sys.modules["eggtools"] = old_module
