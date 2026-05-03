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

    status = ts.get_thread_session_status(db, runtime.runtime_thread_id)
    if status.container_name:
        # Best-effort cleanup so repeated local runs don't accumulate test containers.
        import subprocess
        subprocess.run(["docker", "rm", "-f", status.container_name], capture_output=True, timeout=10)
