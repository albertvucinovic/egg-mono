from __future__ import annotations

import os
from pathlib import Path

import pytest

import eggthreads as ts


def _docker_available() -> bool:
    try:
        return ts.docker_session_available()
    except Exception:
        return False


@pytest.mark.skipif(os.environ.get("EGG_SKIP_DOCKER_REPL_TESTS") == "1", reason="Docker REPL tests disabled")
def test_docker_python_repl_persists_state_and_eggtools(tmp_path, monkeypatch):
    if not _docker_available():
        pytest.skip("Docker is not available")
    monkeypatch.chdir(tmp_path)

    # Use a broadly available image by default so the integration test does not
    # require building egg-rlm-session first. Users can override to test the
    # project image explicitly.
    image = os.environ.get("EGG_RLM_TEST_IMAGE", "python:3.12-slim")

    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="docker", image=image)

    runtime = None
    try:
        out1 = ts.execute_python_repl(db, parent, "x = 123", timeout_sec=20)
        assert "Error:" not in out1

        out2 = ts.execute_python_repl(db, parent, "x + 1", timeout_sec=20)
        assert "124" in out2

        runtime = ts.find_runtime_thread(db, parent, language="python")
        assert runtime is not None
        ts.set_thread_tools_enabled(db, runtime.runtime_thread_id, True)
        ts.set_thread_tool_allowlist(db, runtime.runtime_thread_id, ["bash", "get_child_status"])

        out3 = ts.execute_python_repl(
            db,
            parent,
            "from eggtools import bash\nprint(bash('echo docker-eggtools'))",
            timeout_sec=30,
            drive_runtime_tools=True,
        )
        assert "docker-eggtools" in out3

        child = ts.create_child_thread(db, runtime.runtime_thread_id, name="status-child")
        ts.append_message(db, child, "user", "hello")
        ts.append_message(db, child, "assistant", "done")
        ts.create_snapshot(db, child)

        out4 = ts.execute_python_repl(
            db,
            parent,
            "from eggtools import get_child_status\nprint(get_child_status())",
            timeout_sec=30,
            drive_runtime_tools=True,
        )
        assert child in out4
        assert "context_tokens" in out4
    finally:
        if runtime is None:
            runtime = ts.find_runtime_thread(db, parent, language="python")
        if runtime is not None:
            status = ts.get_thread_session_status(db, runtime.runtime_thread_id)
            if status.container_name:
                # Best-effort cleanup so repeated local runs don't accumulate test containers.
                import subprocess
                subprocess.run(["docker", "rm", "-f", status.container_name], capture_output=True, timeout=10)


@pytest.mark.skipif(os.environ.get("EGG_SKIP_DOCKER_REPL_TESTS") == "1", reason="Docker REPL tests disabled")
def test_docker_repl_cancellation_resets_only_affected_channels(tmp_path, monkeypatch):
    if not _docker_available():
        pytest.skip("Docker is not available")
    monkeypatch.chdir(tmp_path)
    image = os.environ.get("EGG_RLM_TEST_IMAGE", "python:3.12-slim")
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="docker", image=image)

    try:
        assert "77" in ts.execute_python_repl(
            db, parent, "value = 77\nvalue", repl_name="sibling", timeout_sec=20
        )
        timed = ts.execute_python_repl(
            db, parent, "while True: pass", repl_name="cancelled", timeout_sec=1
        )
        assert "TIMEOUT" in timed
        assert "77" in ts.execute_python_repl(db, parent, "value", repl_name="sibling", timeout_sec=20)
        assert "2" in ts.execute_python_repl(db, parent, "1 + 1", repl_name="cancelled", timeout_sec=20)

        bash_timed = ts.execute_bash_repl(
            db, parent, "sleep 99", repl_name="shell", timeout_sec=1
        )
        assert "TIMEOUT" in bash_timed
        bash_after = ts.execute_bash_repl(db, parent, "echo after", repl_name="shell", timeout_sec=20)
        assert "after" in bash_after

        runtime = ts.find_runtime_thread(db, parent, language="python")
        assert runtime is not None
        status = ts.get_thread_session_status(db, runtime.runtime_thread_id)
        bridge = Path(ts.get_or_start_docker_session_handle(db, runtime.runtime_thread_id).bridge_dir)
        assert not list(bridge.glob("eval_*.processing"))
        assert status.status == "ready"
        assert status.daemon_generation
        assert status.active_requests == ()
    finally:
        runtime = ts.find_runtime_thread(db, parent, language="python")
        if runtime is not None:
            status = ts.get_thread_session_status(db, runtime.runtime_thread_id)
            if status.container_name:
                import subprocess
                subprocess.run(["docker", "rm", "-f", status.container_name], capture_output=True, timeout=10)
