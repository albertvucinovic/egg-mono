from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import eggthreads as ts
import eggthreads.session as session
import pytest


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


def test_memory_repl_exposes_python_exec_wrapper_not_python(monkeypatch):
    import eggthreads.repl_bridge as repl_bridge

    monkeypatch.setattr(repl_bridge, "call_tool", lambda *_args, **_kwargs: "ok")
    module = session._make_eggtools_module("test-token")

    assert callable(module.python_exec)
    assert not hasattr(module, "python")


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

        assert module.tool("skill", name="rlm") == "loaded"
        assert seen["name"] == "skill"
        assert seen["arguments"]["name"] == "rlm"
    finally:
        sys.modules.pop("eggtools", None)
        if old_module is not None:
            sys.modules["eggtools"] = old_module


def test_docker_runtime_refreshes_eggtools_without_losing_repl_state(tmp_path, monkeypatch):
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

        imported_skill = module.skill
        nested_generated_wrapper = {"wrapper": module.compact_thread}

        def stale_tool(name, **kwargs):
            return name, kwargs

        monkeypatch.setattr(module, "tool", stale_tool)
        try:
            imported_skill("compaction-checkpoint")
        except TypeError as error:
            assert "multiple values" in str(error)
        else:
            raise AssertionError("stale dispatcher should reproduce the name collision")

        repl_globals = {
            "__name__": "__egg_repl__",
            "imported_skill": imported_skill,
            "nested_generated_wrapper": nested_generated_wrapper,
            "user_state": {"preserve": True},
        }
        refresh_path = runtime_dir / "repl_refresh.py"
        refresh_globals = {
            "__name__": "__egg_runtime_refresh__",
            "repl_globals": repl_globals,
            "runtime_dir": str(runtime_dir),
            "expected_hash": "new-runtime-hash",
        }
        exec(
            compile(refresh_path.read_text(encoding="utf-8"), str(refresh_path), "exec"),
            refresh_globals,
            refresh_globals,
        )

        assert repl_globals["user_state"] == {"preserve": True}
        assert repl_globals["imported_skill"] is module.skill
        assert repl_globals["nested_generated_wrapper"]["wrapper"] is module.compact_thread
        assert module.extract_tool_output.__globals__["_MISSING"] is module.extract_tool_output.__kwdefaults__["source_tool_call_id"]
        assert repl_globals["__egg_runtime_code_hash__"] == "new-runtime-hash"
        assert next(iter(inspect.signature(module.tool).parameters.values())).kind is inspect.Parameter.POSITIONAL_ONLY

        calls = []

        def recording_tool(tool_name, /, **kwargs):
            calls.append((tool_name, kwargs))
            return "loaded"

        monkeypatch.setattr(module, "tool", recording_tool)
        assert repl_globals["imported_skill"]("compaction-checkpoint") == "loaded"
        assert calls == [("skill", {"timeout_sec": None, "name": "compaction-checkpoint"})]
    finally:
        sys.modules.pop("eggtools", None)
        if old_module is not None:
            sys.modules["eggtools"] = old_module


def test_python_repl_runtime_code_hash_tracks_staged_helpers(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session._write_runtime_files(runtime_dir)

    baseline = session._python_repl_runtime_code_hash(runtime_dir)
    assert baseline == session._python_repl_runtime_code_hash(runtime_dir)

    for name in session._PYTHON_REPL_RUNTIME_FILES:
        path = runtime_dir / name
        original = path.read_bytes()
        path.write_bytes(original + b"\n# changed\n")
        try:
            assert session._python_repl_runtime_code_hash(runtime_dir) != baseline
        finally:
            path.write_bytes(original)


def test_runtime_refresh_eval_is_separate_from_user_code():
    refresh_code = session._python_repl_runtime_refresh_code("new-runtime-hash")

    compile(refresh_code, "<runtime-refresh>", "exec")
    assert "new-runtime-hash" in refresh_code
    assert "repl_refresh.py" in refresh_code


def test_docker_python_eval_refreshes_runtime_before_user_code(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session._write_runtime_files(runtime_dir)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    calls = []

    handle = session.DockerSessionHandle(
        session_id="session",
        container_name="container",
        bridge_dir=str(bridge_dir),
        runtime_dir=str(runtime_dir),
        mount_dir=str(tmp_path),
        workspace="/workspace",
    )
    cfg = session.SessionConfig(
        enabled=True,
        provider="docker",
        session_id=handle.session_id,
        workspace=handle.workspace,
    )
    monkeypatch.setattr(
        session,
        "_get_or_start_docker_session_locked",
        lambda *_args: session.SessionStatus(
            True, "docker", handle.session_id, "ready", container_name=handle.container_name,
        ),
    )
    monkeypatch.setattr(session, "_session_bridge_dir", lambda *_args: Path(handle.bridge_dir))
    monkeypatch.setattr(session, "_session_runtime_dir", lambda *_args: Path(handle.runtime_dir))
    monkeypatch.setattr(session, "docker_session_mount_dir", lambda *_args: Path(handle.mount_dir))
    monkeypatch.setattr(
        session,
        "_run_docker_python_eval_request",
        lambda _db, _thread, _bridge, payload, _timeout, _cancel=None: calls.append(payload) or "--- The Python REPL executed successfully and produced no output ---",
    )

    result = session._execute_python_docker_captured(
        object(),
        "runtime-thread",
        cfg,
        "this is invalid syntax !",
        repl_name="default",
        eval_token="token",
        timeout_sec=5,
    )

    assert "successfully" in result
    assert len(calls) == 2
    assert "repl_refresh.py" in calls[0]["code"]
    assert calls[1]["code"] == "this is invalid syntax !"

    session._execute_python_docker_captured(
        object(),
        "runtime-thread",
        cfg,
        "42",
        repl_name="default",
        eval_token="token",
        timeout_sec=5,
    )
    assert len(calls) == 4
    assert "repl_refresh.py" in calls[2]["code"]
    assert calls[3]["code"] == "42"


def test_docker_python_eval_refreshes_after_alternating_host_staging(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    staged_hash = {"value": "host-a"}
    worker_hash = {"value": "host-b"}
    user_observations = []
    calls = []
    cfg = session.SessionConfig(
        enabled=True, provider="docker", session_id="session", workspace="/workspace",
    )
    monkeypatch.setattr(
        session, "_get_or_start_docker_session_locked",
        lambda *_args: session.SessionStatus(
            True, "docker", "session", "ready", container_name="container",
        ),
    )
    monkeypatch.setattr(session, "_session_bridge_dir", lambda *_args: bridge_dir)
    monkeypatch.setattr(session, "_session_runtime_dir", lambda *_args: runtime_dir)
    monkeypatch.setattr(session, "docker_session_mount_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(
        session, "_python_repl_runtime_code_hash", lambda _path: staged_hash["value"],
    )

    def run(_db, _thread, _bridge, payload, _timeout, _cancel=None):
        calls.append(payload)
        if "repl_refresh.py" in payload["code"]:
            worker_hash["value"] = staged_hash["value"]
            return session._PYTHON_REFRESH_SUCCESS_OUTPUT
        user_observations.append(worker_hash["value"])
        return f"--- STDOUT ---\n{worker_hash['value']}"

    monkeypatch.setattr(session, "_run_docker_python_eval_request", run)

    first = session._execute_python_docker_captured(
        object(), "runtime-thread", cfg, "observe()",
        repl_name="default", eval_token="token-a", timeout_sec=5,
    )
    # Another host stages/loads B. Host A then restages A before its next call;
    # its stale process-local belief must never suppress the refresh guard.
    staged_hash["value"] = "host-b"
    worker_hash["value"] = "host-b"
    staged_hash["value"] = "host-a"
    second = session._execute_python_docker_captured(
        object(), "runtime-thread", cfg, "observe()",
        repl_name="default", eval_token="token-a2", timeout_sec=5,
    )

    assert first.endswith("host-a")
    assert second.endswith("host-a")
    assert user_observations == ["host-a", "host-a"]
    assert len(calls) == 4
    assert all("repl_refresh.py" in calls[index]["code"] for index in (0, 2))


@pytest.mark.parametrize(
    "terminal",
    [
        "--- INTERRUPTED ---\nPython REPL eval was cancelled.",
        "--- TIMEOUT ---\nPython REPL timed out.",
        "--- CONTAINMENT FAILURE ---\nPython channel could not be contained.",
        "Error: Docker session daemon restarted before completion.",
        "Error: Docker REPL failed: worker exited.",
        "--- STDERR ---\nTraceback (most recent call last):\nRuntimeError: refresh failed",
    ],
)
def test_docker_python_interrupted_or_failed_refresh_aborts_user_code_and_retries(
    tmp_path, monkeypatch, terminal,
):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    cfg = session.SessionConfig(
        enabled=True, provider="docker", session_id="session", workspace="/workspace",
    )
    monkeypatch.setattr(
        session, "_get_or_start_docker_session_locked",
        lambda *_args: session.SessionStatus(
            True, "docker", "session", "ready", container_name="container",
        ),
    )
    monkeypatch.setattr(session, "_session_bridge_dir", lambda *_args: bridge_dir)
    monkeypatch.setattr(session, "_session_runtime_dir", lambda *_args: runtime_dir)
    monkeypatch.setattr(session, "docker_session_mount_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(session, "_python_repl_runtime_code_hash", lambda _path: "runtime-hash")
    responses = [terminal, session._PYTHON_REFRESH_SUCCESS_OUTPUT, "--- STDOUT ---\nuser-ok"]
    calls = []
    monkeypatch.setattr(
        session,
        "_run_docker_python_eval_request",
        lambda _db, _thread, _bridge, payload, _timeout, _cancel=None:
            calls.append(payload) or responses.pop(0),
    )

    first = session._execute_python_docker_captured(
        object(), "runtime-thread", cfg, "user_code()",
        repl_name="default", eval_token="token", timeout_sec=5,
    )
    assert first.startswith("Error: Egg could not refresh")
    assert terminal in first
    assert len(calls) == 1
    assert "repl_refresh.py" in calls[0]["code"]

    second = session._execute_python_docker_captured(
        object(), "runtime-thread", cfg, "user_code()",
        repl_name="default", eval_token="token-2", timeout_sec=5,
    )
    assert second == "--- STDOUT ---\nuser-ok"
    assert len(calls) == 3
    assert "repl_refresh.py" in calls[1]["code"]
    assert calls[2]["code"] == "user_code()"


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
    assert "python_exec" in ns["__all__"]
    assert callable(ns["python_exec"])
    assert "python" not in ns["__all__"]
    assert "python" not in ns
    for name in ("show_compaction_start", "search_compaction_sources", "fetch_compaction_source"):
        assert name not in ns["__all__"]
        assert name not in ns

    assert ns["compact_thread"]() == "called:compact_thread"
    assert ns["compact_thread"](start_message="last_user", timeout_sec=3) == "called:compact_thread"
    assert ns["python_exec"]("print('ok')") == "called:python_exec"

    assert calls == [
        ("compact_thread", {}, None),
        ("compact_thread", {"start_message": "last_user"}, 3),
        ("python_exec", {"script": "print('ok')"}, None),
    ]
