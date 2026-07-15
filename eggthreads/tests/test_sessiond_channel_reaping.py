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
    sessiond.CHANNEL_STARTING.clear()
    sessiond.CHANNEL_PROCESS_META.clear()
    sessiond.CHANNEL_GENERATION_COUNTER = 0
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
    monkeypatch.setattr(
        sessiond,
        "_kill_and_verify_process_group",
        lambda pgid, proc: killed.append((pgid, proc)) or (True, ""),
    )

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == ["python:old"]
    assert len(killed) == 1
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



def _spawn_dead_leader_with_descendant(tmp_path: Path, name: str) -> tuple[int, int]:
    pid_file = tmp_path / f"{name}.pid"
    script = (
        "import os,subprocess; "
        "p=subprocess.Popen(['python','-c',"
        "\"import time; open('/proc/self/comm','w').write('egg worker name\\\\n'); time.sleep(99)\"]); "
        f"open({str(pid_file)!r},'w').write(str(p.pid)); "
        "os._exit(0)"
    )
    leader = subprocess.Popen(["python", "-c", script], start_new_session=True)
    pgid = os.getpgid(leader.pid)
    leader.wait(timeout=2)
    deadline = time.time() + 2
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.01)
    descendant = int(pid_file.read_text())
    while time.time() < deadline:
        try:
            if "egg worker name" in Path(f"/proc/{descendant}/stat").read_text():
                break
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    assert "egg worker name" in Path(f"/proc/{descendant}/stat").read_text()
    assert sessiond._process_group_has_live_members(pgid)
    return pgid, descendant


def _wait_process_group_gone(pgid: int, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while sessiond._process_group_has_live_members(pgid) and time.time() < deadline:
        time.sleep(0.01)
    assert not sessiond._process_group_has_live_members(pgid)


def test_reap_python_dead_leader_kills_live_group_descendant(tmp_path):
    pgid, descendant = _spawn_dead_leader_with_descendant(tmp_path, "python")
    key = "python:dead"
    sessiond.CHANNEL_PROCESS_META[key] = {"generation": 1, "pgid": pgid, "pid": pgid}
    sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == [key]
    assert not sessiond._process_group_has_live_members(pgid)
    assert key not in sessiond.CHANNEL_PROCESS_META
    assert sessiond.CHANNEL_ACTIVITY[key]["reaped_at"] == 100.0


def test_reap_bash_dead_leader_kills_live_group_descendant(tmp_path):
    pgid, descendant = _spawn_dead_leader_with_descendant(tmp_path, "bash")
    key = "bash:dead"
    sessiond.CHANNEL_PROCESS_META[key] = {"generation": 1, "pgid": pgid, "pid": pgid}
    sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == [key]
    assert not sessiond._process_group_has_live_members(pgid)
    assert key not in sessiond.CHANNEL_PROCESS_META


@pytest.mark.parametrize("language", ["python", "bash"])
def test_dead_leader_descendant_is_contained_before_same_channel_successor(
    language, tmp_path,
):
    pgid, _descendant = _spawn_dead_leader_with_descendant(tmp_path, f"successor-{language}")
    channel = "same"
    key = f"{language}:{channel}"
    sessiond.CHANNEL_PROCESS_META[key] = {"generation": 1, "pgid": pgid, "pid": pgid}
    sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": time.time()}

    if language == "python":
        dead = SimpleNamespace(is_alive=lambda: False)
        sessiond.PY_WORKERS[channel] = (dead, SimpleNamespace(close=lambda: None))
        worker = sessiond._get_python_worker(channel)
        assert worker[0].is_alive()
    else:
        sessiond.BASH_REPLS[channel] = SimpleNamespace(poll=lambda: 0)
        worker = sessiond._bash_proc(channel, tmp_path, "token", tmp_path)
        assert worker.poll() is None

    _wait_process_group_gone(pgid)
    assert sessiond.CHANNEL_PROCESS_META[key]["pgid"] != pgid


def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm():
    state, pgid = sessiond._proc_stat_state_and_pgid(
        "321 (worker name (with spaces)) S 111 222 333 0 0 0"
    )
    assert state == "S"
    assert pgid == 222


@pytest.mark.parametrize("language", ["python", "bash"])
def test_dead_leader_teardown_failure_quarantines_successor(monkeypatch, language):
    channel = "unsafe"
    key = f"{language}:{channel}"
    sessiond.CHANNEL_PROCESS_META[key] = {"generation": 1, "pgid": 7654, "pid": 7654}
    sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}
    monkeypatch.setattr(
        sessiond, "_kill_and_verify_process_group",
        lambda *_a, **_k: (False, "permission denied"),
    )
    if language == "python":
        dead = SimpleNamespace(is_alive=lambda: False)
        sessiond.PY_WORKERS[channel] = (dead, SimpleNamespace(close=lambda: None))
        start = lambda: sessiond._get_python_worker(channel)
    else:
        sessiond.BASH_REPLS[channel] = SimpleNamespace(poll=lambda: 0)
        start = lambda: sessiond._bash_proc(channel, Path("."), "token", Path("."))

    with pytest.raises(RuntimeError, match="could not be contained"):
        start()
    with pytest.raises(RuntimeError, match="quarantined"):
        start()

    payload = sessiond._daemon_status_payload(now=100.0)
    assert payload["channel_state"][key] == {
        "state": "reap_failed",
        "last_activity_at": 1.0,
        "reap_reason": "teardown_failed",
        "reap_error": "permission denied",
    }


def test_reap_kill_failure_is_truthful_and_retryable(monkeypatch):
    key = "python:failure"
    sessiond.CHANNEL_PROCESS_META[key] = {"generation": 1, "pgid": 4444, "pid": 4444}
    sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}
    monkeypatch.setattr(
        sessiond, "_kill_and_verify_process_group",
        lambda *_a, **_k: (False, "permission denied"),
    )

    assert sessiond.reap_idle_channels(timeout_sec=10, now=100.0) == []
    assert key in sessiond.CHANNEL_PROCESS_META
    assert "reaped_at" not in sessiond.CHANNEL_ACTIVITY[key]
    assert sessiond.CHANNEL_ACTIVITY[key]["reap_reason"] == "teardown_failed"
    assert sessiond.CHANNEL_ACTIVITY[key]["reap_error"] == "permission denied"
    payload = sessiond._daemon_status_payload(now=101.0)
    assert payload["channel_state"][key]["state"] == "reap_failed"


def test_touch_does_not_split_atomic_teardown_failure_state():
    key = "python:unsafe"
    sessiond.CHANNEL_ACTIVITY[key] = {
        "last_activity_at": 1.0,
        "reap_reason": "teardown_failed",
        "reap_error": "permission denied",
    }

    sessiond.touch_channel("python", "unsafe", now=2.0)

    assert sessiond.CHANNEL_ACTIVITY[key] == {
        "last_activity_at": 2.0,
        "reap_reason": "teardown_failed",
        "reap_error": "permission denied",
    }


def test_timeout_and_cancel_remove_unique_channel_activity(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    release = threading.Event()

    def fake_python(*_a, cancel_check=None, **_k):
        while not (cancel_check and cancel_check()):
            time.sleep(0.005)
        sessiond._kill_python_worker("unique-py")
        return "--- INTERRUPTED ---"

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    request = bridge / "eval_py.req.json"
    request.write_text(json.dumps({
        "protocol_version": 2, "request_id": "py", "language": "python",
        "channel": "unique-py", "host_owner_id": "host",
    }))
    sessiond.process_eval_request(request, bridge, runtime)
    deadline = time.time() + 2
    while "py" not in sessiond.ACTIVE_EVALS and time.time() < deadline:
        time.sleep(0.005)
    (bridge / "eval_py.cancel.json").write_text(json.dumps({"host_owner_id": "host", "reason": "timeout"}))
    sessiond.service_cancel_requests(bridge)
    deadline = time.time() + 2
    while "py" in sessiond.ACTIVE_EVALS and time.time() < deadline:
        time.sleep(0.005)
    assert "python:unique-py" not in sessiond.CHANNEL_ACTIVITY

    def fake_bash(*_a, cancel_check=None, **_k):
        while not (cancel_check and cancel_check()):
            time.sleep(0.005)
        sessiond._terminate_bash_channel("unique-bash")
        return "--- INTERRUPTED ---"

    monkeypatch.setattr(sessiond, "execute_bash", fake_bash)
    request = bridge / "eval_bash.req.json"
    request.write_text(json.dumps({
        "protocol_version": 2, "request_id": "bash", "language": "bash",
        "channel": "unique-bash", "host_owner_id": "host",
    }))
    sessiond.process_eval_request(request, bridge, runtime)
    deadline = time.time() + 2
    while "bash" not in sessiond.ACTIVE_EVALS and time.time() < deadline:
        time.sleep(0.005)
    (bridge / "eval_bash.cancel.json").write_text(json.dumps({"host_owner_id": "host", "reason": "timeout"}))
    sessiond.service_cancel_requests(bridge)
    deadline = time.time() + 2
    while "bash" in sessiond.ACTIVE_EVALS and time.time() < deadline:
        time.sleep(0.005)
    assert "bash:unique-bash" not in sessiond.CHANNEL_ACTIVITY


def test_process_map_stress_creation_reset_and_reap(monkeypatch):
    errors = []
    stop = threading.Event()

    def mutate(prefix: str):
        try:
            for index in range(300):
                channel = f"{prefix}-{index % 11}"
                key = f"python:{channel}"
                with sessiond.ACTIVE_EVALS_LOCK:
                    sessiond.PY_WORKERS[channel] = _fake_python_worker()
                    generation = sessiond._next_channel_generation_locked()
                    sessiond.CHANNEL_PROCESS_META[key] = {
                        "generation": generation, "pgid": 10000 + index, "pid": 10000 + index,
                    }
                    sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}
                if index % 3 == 0:
                    with sessiond.ACTIVE_EVALS_LOCK:
                        sessiond.PY_WORKERS.pop(channel, None)
                        sessiond.CHANNEL_PROCESS_META.pop(key, None)
        except Exception as exc:
            errors.append(exc)

    def snapshot_and_reap():
        try:
            for _ in range(500):
                sessiond._daemon_status_payload()
                sessiond.reap_idle_channels(timeout_sec=10, now=100.0)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(sessiond, "_kill_and_verify_process_group", lambda *_a, **_k: (True, ""))
    threads = [
        threading.Thread(target=mutate, args=("a",)),
        threading.Thread(target=mutate, args=("b",)),
        threading.Thread(target=snapshot_and_reap),
        threading.Thread(target=snapshot_and_reap),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []


def test_unique_channel_generation_churn_has_bounded_residual_state(monkeypatch):
    monkeypatch.setattr(
        sessiond, "_kill_and_verify_process_group",
        lambda *_a, **_k: (True, ""),
    )

    for index in range(500):
        channel = f"unique-{index}"
        key = f"python:{channel}"
        with sessiond.ACTIVE_EVALS_LOCK:
            sessiond.PY_WORKERS[channel] = _fake_python_worker()
            sessiond.CHANNEL_PROCESS_META[key] = {
                "generation": sessiond._next_channel_generation_locked(),
                "pgid": 20_000 + index,
                "pid": 20_000 + index,
            }
            sessiond.CHANNEL_ACTIVITY[key] = {"last_activity_at": 1.0}
        assert sessiond._kill_python_worker(channel)

    assert sessiond.CHANNEL_PROCESS_META == {}
    assert sessiond.CHANNEL_ACTIVITY == {}
    assert not hasattr(sessiond, "CHANNEL_GENERATIONS")
    assert sessiond.CHANNEL_GENERATION_COUNTER == 500


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
