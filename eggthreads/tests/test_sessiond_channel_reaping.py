from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from eggthreads.session_runtime import sessiond


def _reset_state() -> None:
    deadline = time.monotonic() + 2
    while sessiond.ACTIVE_EVALS and time.monotonic() < deadline:
        time.sleep(0.005)
    for channel in list(sessiond.PY_WORKERS):
        sessiond._kill_python_worker(channel)
    for channel in list(sessiond.BASH_REPLS):
        sessiond._terminate_bash_channel(channel)
    sessiond.ACTIVE_EVALS.clear()
    sessiond.CHANNEL_QUEUES.clear()
    sessiond.CHANNEL_CONDITIONS.clear()
    sessiond.CHANNEL_ACTIVITY.clear()
    sessiond.CHANNEL_REAPING.clear()
    sessiond.CHANNEL_IDLE_TIMEOUT_SEC = None
    sessiond.STATUS_PATH = None


@pytest.fixture(autouse=True)
def clean_state():
    _reset_state()
    yield
    _reset_state()


def _fake_python_worker(alive: bool = True):
    class Proc:
        pid = 1234

        def is_alive(self):
            return alive

        def join(self, _timeout):
            pass

        def kill(self):
            pass

    class Conn:
        def close(self):
            pass

    return Proc(), Conn()


def _fake_bash_proc(running: bool = True):
    class Proc:
        pid = 4321

        def poll(self):
            return None if running else 0

        def kill(self):
            pass

        def wait(self, timeout):
            return 0

    return Proc()


def test_channel_idle_policy_disabled_and_invalid():
    assert sessiond.parse_positive_timeout(None) is None
    for value in ("", "off", "bad", 0, -1, float("nan"), float("inf")):
        assert sessiond.parse_positive_timeout(value) is None

    sessiond.PY_WORKERS["py"] = _fake_python_worker()
    sessiond.CHANNEL_ACTIVITY["python:py"] = {"last_activity_at": 1.0}
    assert sessiond.reap_idle_channels(now=100.0) == []
    assert "py" in sessiond.PY_WORKERS


def test_host_passes_only_valid_positive_channel_policy_to_sessiond(monkeypatch, tmp_path):
    import eggthreads as ts
    import eggthreads.session as host_session

    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="root")
    ts.enable_thread_session(db, thread_id, provider="docker")
    cfg = ts.get_thread_session_config(db, thread_id)
    bridge = host_session._session_bridge_dir(cfg.session_id)
    runtime = host_session._session_runtime_dir(cfg.session_id)
    commands = []
    monkeypatch.setattr(host_session, "_docker_inspect_running", lambda _name: None)
    monkeypatch.setattr(
        host_session.subprocess,
        "run",
        lambda argv, **_kwargs: commands.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="id", stderr=""),
    )

    monkeypatch.setenv("EGG_RLM_CHANNEL_IDLE_TIMEOUT", "2m")
    host_session._start_docker_container(db, thread_id, cfg, "container-a", bridge, runtime)
    assert "--channel-idle-timeout-sec" in commands[-1]
    assert commands[-1][commands[-1].index("--channel-idle-timeout-sec") + 1] == "120"

    commands.clear()
    monkeypatch.setenv("EGG_RLM_CHANNEL_IDLE_TIMEOUT", "off")
    host_session._start_docker_container(db, thread_id, cfg, "container-b", bridge, runtime)
    assert "--channel-idle-timeout-sec" not in commands[-1]


def test_threshold_and_independent_channels(monkeypatch):
    sessiond.PY_WORKERS.update({
        "old": _fake_python_worker(),
        "fresh": _fake_python_worker(),
    })
    sessiond.CHANNEL_ACTIVITY.update({
        "python:old": {"last_activity_at": 1.0},
        "python:fresh": {"last_activity_at": 95.0},
    })
    killed = []
    original = sessiond._kill_python_worker

    def kill(channel, *, preserve_activity=False):
        killed.append(channel)
        original(channel, preserve_activity=preserve_activity)

    monkeypatch.setattr(sessiond, "_kill_python_worker", kill)

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == ["python:old"]
    assert killed == ["old"]
    assert "fresh" in sessiond.PY_WORKERS
    assert sessiond.CHANNEL_ACTIVITY["python:old"]["reap_reason"] == "idle_timeout:10s"


def test_running_queued_and_cancelling_requests_protect_channels():
    for name in ("running", "queued", "cancelling"):
        sessiond.PY_WORKERS[name] = _fake_python_worker()
        key = f"python:{name}"
        sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}
        request_id = f"req-{name}"
        cancel = threading.Event()
        if name == "cancelling":
            cancel.set()
        sessiond.ACTIVE_EVALS[request_id] = {
            "running": name == "running",
            "cancel": cancel,
            "cancel_reason": "interrupted" if name == "cancelling" else None,
            "payload": {"language": "python", "channel": name},
        }
        sessiond.CHANNEL_QUEUES[key] = [request_id]

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == []
    assert set(sessiond.PY_WORKERS) == {"running", "queued", "cancelling"}


def test_python_state_is_reset_after_reap(tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = Path(sessiond.__file__).resolve().parent

    first = sessiond.execute_python("value = 41\nvalue", "py", bridge, "", runtime, timeout_sec=2)
    assert "41" in first
    sessiond.CHANNEL_ACTIVITY["python:py"]["last_activity_at"] = 1.0

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == ["python:py"]
    assert "py" not in sessiond.PY_WORKERS
    after = sessiond.execute_python("value", "py", bridge, "", runtime, timeout_sec=2)
    assert "NameError" in after
    assert "reaped_at" not in sessiond.CHANNEL_ACTIVITY["python:py"]


def test_bash_reap_kills_process_group_and_descendant(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = Path(sessiond.__file__).resolve().parent
    proc = sessiond._bash_proc("shell", bridge, "", runtime)
    assert proc.stdin is not None
    child_file = tmp_path / "child.pid"
    proc.stdin.write(f"sleep 99 & echo $! > {child_file}\n")
    proc.stdin.flush()
    deadline = time.time() + 2
    while not child_file.exists() and time.time() < deadline:
        time.sleep(0.01)
    child_pid = int(child_file.read_text().strip())
    assert Path(f"/proc/{child_pid}").exists()
    pgid = os.getpgid(proc.pid)
    killed = []
    original_killpg = sessiond.os.killpg

    def record_killpg(seen_pgid, sig):
        killed.append((seen_pgid, sig))
        return original_killpg(seen_pgid, sig)

    monkeypatch.setattr(sessiond.os, "killpg", record_killpg)
    sessiond.CHANNEL_ACTIVITY["bash:shell"]["last_activity_at"] = 1.0

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == ["bash:shell"]
    assert killed == [(pgid, sessiond.signal.SIGKILL)]
    assert proc.poll() is not None
    assert "shell" not in sessiond.BASH_REPLS
    deadline = time.time() + 2
    while Path(f"/proc/{child_pid}").exists() and time.time() < deadline:
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
            if state == "Z":
                break
        except FileNotFoundError:
            break
        time.sleep(0.01)
    if Path(f"/proc/{child_pid}").exists():
        assert Path(f"/proc/{child_pid}/stat").read_text().split()[2] == "Z"


def test_same_channel_admission_waits_for_reap_reservation(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    sessiond.PY_WORKERS["race"] = _fake_python_worker()
    sessiond.CHANNEL_ACTIVITY["python:race"] = {"last_activity_at": 1.0}
    teardown_entered = threading.Event()
    allow_teardown = threading.Event()
    executed = threading.Event()

    def before_teardown(_key):
        teardown_entered.set()
        allow_teardown.wait(2)

    def fake_python(*_args, **_kwargs):
        executed.set()
        return "after-reap"

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    reaper = threading.Thread(
        target=lambda: sessiond.reap_idle_channels(
            timeout_sec=10, now=100.0, before_teardown=before_teardown,
        )
    )
    reaper.start()
    assert teardown_entered.wait(1)

    request = bridge / "eval_next.req.json"
    request.write_text(json.dumps({
        "protocol_version": 2,
        "request_id": "next",
        "language": "python",
        "channel": "race",
        "repl_name": "race",
        "host_owner_id": "host",
    }))
    admitted = threading.Thread(target=sessiond.process_eval_request, args=(request, bridge, runtime))
    admitted.start()
    time.sleep(0.05)
    assert not executed.is_set()
    assert "next" not in sessiond.ACTIVE_EVALS
    allow_teardown.set()
    reaper.join(2); admitted.join(2)
    deadline = time.time() + 2
    while not executed.is_set() and time.time() < deadline:
        time.sleep(0.005)
    assert executed.is_set()
    response = bridge / "eval_next.res.json"
    deadline = time.time() + 2
    while not response.exists() and time.time() < deadline:
        time.sleep(0.005)
    assert json.loads(response.read_text())["output"] == "after-reap"


def test_channel_status_truthfully_reports_ready_busy_reaping_and_reaped(tmp_path):
    path = tmp_path / "status.json"
    sessiond.STATUS_PATH = path
    sessiond.PY_WORKERS["ready"] = _fake_python_worker()
    sessiond.PY_WORKERS["work"] = _fake_python_worker()
    sessiond.CHANNEL_ACTIVITY.update({
        "python:ready": {"last_activity_at": 10.0},
        "python:work": {"last_activity_at": 11.0},
        "bash:gone": {"last_activity_at": 12.0, "reaped_at": 12.0, "reap_reason": "idle_timeout:5s"},
        "python:teardown": {"last_activity_at": 13.0},
    })
    sessiond.ACTIVE_EVALS["req"] = {
        "running": True,
        "cancel": threading.Event(),
        "cancel_reason": None,
        "payload": {"language": "python", "channel": "work", "created_at": 11.0},
    }
    sessiond.CHANNEL_QUEUES["python:work"] = ["req"]
    sessiond.CHANNEL_REAPING.add("python:teardown")

    payload = sessiond._daemon_status_payload(now=20.0)

    assert payload["channel_state"]["python:ready"] == {
        "state": "ready", "last_activity_at": 10.0,
    }
    assert payload["channel_state"]["python:work"]["state"] == "busy"
    assert payload["channel_state"]["python:teardown"]["state"] == "reaping"
    assert payload["channel_state"]["bash:gone"] == {
        "state": "reaped",
        "last_activity_at": 12.0,
        "reaped_at": 12.0,
        "reap_reason": "idle_timeout:5s",
    }
