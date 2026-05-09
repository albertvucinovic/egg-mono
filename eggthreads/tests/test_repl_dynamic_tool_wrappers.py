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


def test_generated_eggtools_wrappers_include_compaction_source_helpers():
    from eggthreads.session_runtime.tool_wrappers import generate_tool_wrappers_source
    from eggthreads.tools import create_default_tools

    calls: list[tuple[str, dict, object]] = []

    def tool(name: str, timeout_sec=None, **kwargs):
        calls.append((name, kwargs, timeout_sec))
        return f"called:{name}"

    specs = [entry["spec"] for entry in create_default_tools()._tools.values()]
    ns = {"tool": tool, "Any": object, "__name__": "eggtools._generated"}
    exec(compile(generate_tool_wrappers_source(specs), "<eggtools-generated-test>", "exec"), ns, ns)

    for name in ("show_compaction_start", "search_compaction_sources", "fetch_compaction_source"):
        assert name in ns["__all__"]
        assert callable(ns[name])

    assert ns["show_compaction_start"]() == "called:show_compaction_start"
    assert ns["search_compaction_sources"]("needle", max_results=2, timeout_sec=3) == "called:search_compaction_sources"
    assert ns["fetch_compaction_source"]("msg_visible", max_chars=100) == "called:fetch_compaction_source"

    assert calls == [
        ("show_compaction_start", {}, None),
        ("search_compaction_sources", {"query": "needle", "max_results": 2}, 3),
        ("fetch_compaction_source", {"source_id": "msg_visible", "max_chars": 100}, None),
    ]


def test_memory_repl_compaction_source_wrappers_skip_hidden_pre_start_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(
        db,
        runtime,
        ["show_compaction_start", "search_compaction_sources", "fetch_compaction_source"],
    )

    visible = ts.append_message(db, runtime, "user", "needle visible old detail")
    hidden = ts.append_message(
        db,
        runtime,
        "user",
        "needle DO_NOT_LEAK_HIDDEN_PRE_START detail",
        extra={"no_api": True},
    )
    start = ts.append_message(db, runtime, "assistant", "summary start")
    committed = ts.commit_thread_compaction(db, runtime, start, created_by="test")
    assert committed.success is True

    code = f'''
from eggtools import fetch_compaction_source, search_compaction_sources, show_compaction_start
print("HAS_WRAPPERS", callable(show_compaction_start), callable(search_compaction_sources), callable(fetch_compaction_source))
print("STATUS", show_compaction_start())
print("SEARCH", search_compaction_sources("needle", max_results=5, max_chars=1000))
print("FETCH", fetch_compaction_source({visible!r}, max_chars=1000))
'''

    out = ts.execute_python_repl(
        db,
        parent,
        code,
        drive_runtime_tools=True,
        timeout_sec=5,
    )

    assert "HAS_WRAPPERS True True True" in out
    assert visible in out
    assert "needle visible old detail" in out
    assert start in out
    assert hidden not in out
    assert "DO_NOT_LEAK_HIDDEN_PRE_START" not in out

    states = ts.build_tool_call_states(db, runtime)
    finished_tool_names = {tc.name for tc in states.values() if tc.state == "TC6"}
    assert {"show_compaction_start", "search_compaction_sources", "fetch_compaction_source"}.issubset(finished_tool_names)

