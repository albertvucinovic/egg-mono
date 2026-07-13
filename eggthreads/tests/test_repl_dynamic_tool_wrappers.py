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

    assert "reserved tool context" in out
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


def test_docker_runtime_handwritten_skill_wrapper_can_forward_name(tmp_path, monkeypatch):
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

        seen: dict = {}
        monkeypatch.setattr(module, "_eval_token", lambda: "test-token")
        monkeypatch.setattr(module, "_atomic_write_json", lambda _path, payload: seen.update(payload))

        class ResponsePath:
            def exists(self):
                return True

            def read_text(self, *, encoding):
                assert encoding == "utf-8"
                return '{"ok": true, "result": "loaded"}'

            def unlink(self):
                return None

        class RequestPath:
            def with_suffix(self, _suffix):
                return self

        class BridgePath:
            def __truediv__(self, value):
                return ResponsePath() if value.endswith(".res.json") else RequestPath()

        monkeypatch.setattr(module, "_bridge_dir", BridgePath)

        assert module.skill("compaction-checkpoint") == "loaded"
        assert seen["name"] == "skill"
        assert seen["arguments"]["name"] == "compaction-checkpoint"
    finally:
        sys.modules.pop("eggtools", None)
        if old_module is not None:
            sys.modules["eggtools"] = old_module


def test_generated_eggtools_wrappers_include_compact_thread_but_not_removed_compaction_helpers():
    from eggthreads.session_runtime.tool_wrappers import generate_tool_wrappers_source
    from eggthreads.tools import create_default_tools

    calls: list[tuple[str, dict, object]] = []

    def tool(name: str, timeout_sec=None, **kwargs):
        calls.append((name, kwargs, timeout_sec))
        return f"called:{name}"

    specs = [entry["spec"] for entry in create_default_tools()._tools.values()]
    ns = {"tool": tool, "Any": object, "__name__": "eggtools._generated"}
    exec(compile(generate_tool_wrappers_source(specs), "<eggtools-generated-test>", "exec"), ns, ns)

    assert "compact_thread" in ns["__all__"]
    assert callable(ns["compact_thread"])
    for name in ("show_compaction_start", "search_compaction_sources", "fetch_compaction_source"):
        assert name not in ns["__all__"]
        assert name not in ns

    assert ns["compact_thread"]() == "called:compact_thread"
    assert ns["compact_thread"](start_message="last_user", timeout_sec=3) == "called:compact_thread"

    assert calls == [
        ("compact_thread", {}, None),
        ("compact_thread", {"start_message": "last_user"}, 3),
    ]
