from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from eggthreads.session_runtime import sessiond


def _reset_sessiond_state() -> None:
    deadline = time.monotonic() + 2.0
    while sessiond.ACTIVE_EVALS and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not sessiond.ACTIVE_EVALS
    for channel in list(sessiond.PY_WORKERS):
        sessiond._kill_python_worker(channel)
    for channel, proc in list(sessiond.BASH_REPLS.items()):
        if proc.poll() is None:
            try:
                sessiond.os.killpg(sessiond.os.getpgid(proc.pid), sessiond.signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        sessiond.BASH_REPLS.pop(channel, None)
    sessiond.CHANNEL_QUEUES.clear()
    sessiond.CHANNEL_CONDITIONS.clear()
    sessiond.CHANNEL_ACTIVITY.clear()
    sessiond.CHANNEL_REAPING.clear()
    sessiond.CHANNEL_STARTING.clear()
    sessiond.CHANNEL_PROCESS_META.clear()
    sessiond.CHANNEL_GENERATION_COUNTER = 0
    sessiond.CHANNEL_IDLE_TIMEOUT_SEC = None


@pytest.fixture(autouse=True)
def reset_sessiond_state():
    _reset_sessiond_state()
    yield
    _reset_sessiond_state()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.005)


def _request(bridge: Path, request_id: str, *, language: str, channel: str, code: str = "") -> Path:
    path = bridge / f"eval_{request_id}.req.json"
    path.write_text(json.dumps({
        "protocol_version": 2,
        "request_id": request_id,
        "language": language,
        "channel": channel,
        "repl_name": channel,
        "code": code,
        "script": code,
        "host_owner_id": "host-a",
    }))
    return path


def test_python_cancel_resets_only_requested_channel(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    releases = {"python:a": threading.Event(), "python:b": threading.Event()}
    killed: list[str] = []

    def fake_python(code, repl_name, *_args, cancel_check=None, **_kwargs):
        key = f"python:{repl_name}"
        while not releases[key].is_set():
            if cancel_check and cancel_check():
                killed.append(repl_name)
                return "--- INTERRUPTED ---\nreset"
            time.sleep(0.005)
        return f"done-{repl_name}"

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    sessiond.process_eval_request(_request(bridge, "a1", language="python", channel="a"), bridge, runtime)
    sessiond.process_eval_request(_request(bridge, "b1", language="python", channel="b"), bridge, runtime)
    _wait_until(lambda: len(sessiond.ACTIVE_EVALS) == 2 and all(item["running"] for item in sessiond.ACTIVE_EVALS.values()))
    (bridge / "eval_a1.cancel.json").write_text(json.dumps({"host_owner_id": "host-a", "reason": "interrupted"}))
    sessiond.service_cancel_requests(bridge)
    _wait_until(lambda: (bridge / "eval_a1.res.json").exists())
    _wait_until(lambda: killed == ["a"])
    assert not (bridge / "eval_b1.res.json").exists()
    releases["python:b"].set()
    _wait_until(lambda: (bridge / "eval_b1.res.json").exists())
    assert json.loads((bridge / "eval_b1.res.json").read_text())["output"] == "done-b"


def test_bash_cancel_and_result_race_is_exact_once(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    release = threading.Event()

    def fake_bash(*_args, cancel_check=None, **_kwargs):
        while not release.is_set():
            if cancel_check and cancel_check():
                return "--- INTERRUPTED ---\nBash reset"
            time.sleep(0.005)
        return "success"

    monkeypatch.setattr(sessiond, "execute_bash", fake_bash)
    sessiond.process_eval_request(_request(bridge, "bash1", language="bash", channel="shell"), bridge, runtime)
    _wait_until(lambda: "bash1" in sessiond.ACTIVE_EVALS)
    (bridge / "eval_bash1.cancel.json").write_text(json.dumps({"host_owner_id": "host-a", "reason": "interrupted"}))
    sessiond.service_cancel_requests(bridge)
    release.set()
    _wait_until(lambda: (bridge / "eval_bash1.res.json").exists())
    response = json.loads((bridge / "eval_bash1.res.json").read_text())
    assert response["request_id"] == "bash1"
    assert response["reason"] == "cancelled"
    assert len(list(bridge.glob("eval_bash1.res.json"))) == 1


def test_same_channel_serializes_while_independent_channel_runs(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    release_first = threading.Event()
    order: list[str] = []

    def fake_python(code, repl_name, *_args, **_kwargs):
        order.append(code)
        if code == "first":
            release_first.wait()
        return code

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    sessiond.process_eval_request(_request(bridge, "one", language="python", channel="same", code="first"), bridge, runtime)
    sessiond.process_eval_request(_request(bridge, "two", language="python", channel="same", code="second"), bridge, runtime)
    sessiond.process_eval_request(_request(bridge, "other", language="python", channel="other", code="other"), bridge, runtime)
    _wait_until(lambda: (bridge / "eval_other.res.json").exists())
    assert order[0] == "first"
    assert "other" in order
    assert "second" not in order
    release_first.set()
    _wait_until(lambda: (bridge / "eval_two.res.json").exists())
    assert order.index("second") > order.index("first")


def test_channel_condition_shares_eval_state_lock(monkeypatch, tmp_path):
    """Queue admission and head promotion use one re-entrant lock order."""

    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    release = threading.Event()

    monkeypatch.setattr(
        sessiond,
        "execute_python",
        lambda *_args, **_kwargs: release.wait() or "done",
    )
    sessiond.process_eval_request(
        _request(bridge, "shared-lock", language="python", channel="same"),
        bridge,
        runtime,
    )
    _wait_until(lambda: "python:same" in sessiond.CHANNEL_CONDITIONS)
    condition = sessiond.CHANNEL_CONDITIONS["python:same"]
    assert condition._lock is sessiond.ACTIVE_EVALS_LOCK
    release.set()
    _wait_until(lambda: (bridge / "eval_shared-lock.res.json").exists())


def test_cancel_queued_request_never_executes(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    release_first = threading.Event()
    calls: list[str] = []

    def fake_python(code, *_args, **_kwargs):
        calls.append(code)
        if code == "first":
            release_first.wait()
        return code

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    sessiond.process_eval_request(_request(bridge, "one", language="python", channel="same", code="first"), bridge, runtime)
    sessiond.process_eval_request(_request(bridge, "two", language="python", channel="same", code="side_effect"), bridge, runtime)
    _wait_until(lambda: "two" in sessiond.ACTIVE_EVALS)
    (bridge / "eval_two.cancel.json").write_text(json.dumps({"host_owner_id": "host-a", "reason": "timeout"}))
    sessiond.service_cancel_requests(bridge)
    _wait_until(lambda: (bridge / "eval_two.res.json").exists())
    response = json.loads((bridge / "eval_two.res.json").read_text())
    assert response["reason"] == "timeout"
    assert response["output"].startswith("--- TIMEOUT ---")
    assert calls == ["first"]
    release_first.set()
    _wait_until(lambda: (bridge / "eval_one.res.json").exists())


def test_cancel_kill_completes_before_same_channel_successor_starts(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    first_release = threading.Event()
    successor_started = threading.Event()
    kill_entered = threading.Event()
    allow_kill = threading.Event()

    def fake_python(code, *_args, cancel_check=None, **_kwargs):
        if code == "first":
            while not first_release.is_set():
                if cancel_check and cancel_check():
                    first_release.set()
                time.sleep(0.005)
            return "--- INTERRUPTED ---\nreset"
        successor_started.set()
        return "second"

    def blocking_kill(active):
        kill_entered.set()
        assert allow_kill.wait(2)
        first_release.set()

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    monkeypatch.setattr(sessiond, "_cancel_active_channel", blocking_kill)
    sessiond.process_eval_request(
        _request(bridge, "first", language="python", channel="same", code="first"),
        bridge, runtime,
    )
    sessiond.process_eval_request(
        _request(bridge, "second", language="python", channel="same", code="second"),
        bridge, runtime,
    )
    _wait_until(lambda: sessiond.ACTIVE_EVALS.get("first", {}).get("running") is True)
    (bridge / "eval_first.cancel.json").write_text(json.dumps({
        "host_owner_id": "host-a", "reason": "interrupted",
    }))
    cancel_thread = threading.Thread(target=sessiond.service_cancel_requests, args=(bridge,))
    cancel_thread.start()
    assert kill_entered.wait(2)
    assert not successor_started.wait(0.05)
    allow_kill.set()
    cancel_thread.join(2)
    assert not cancel_thread.is_alive()
    _wait_until(successor_started.is_set)
    _wait_until(lambda: (bridge / "eval_second.res.json").exists())


def test_finished_result_wins_late_cancel_without_channel_reset(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    req_id = "done"
    (bridge / f"eval_{req_id}.res.json").write_text(json.dumps({"request_id": req_id, "reason": "success"}))
    (bridge / f"eval_{req_id}.cancel.json").write_text(json.dumps({"host_owner_id": "host-a", "reason": "timeout"}))
    killed: list[str] = []
    monkeypatch.setattr(sessiond, "_cancel_active_channel", lambda _active: killed.append("called"))

    sessiond.service_cancel_requests(bridge)

    ack = json.loads((bridge / f"eval_{req_id}.cancel.ack.json").read_text())
    assert ack["state"] == "already_finished"
    assert killed == []
    assert json.loads((bridge / f"eval_{req_id}.res.json").read_text())["reason"] == "success"


def test_cancel_owner_mismatch_is_rejected(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    release = threading.Event()

    def fake_python(*_args, **_kwargs):
        release.wait()
        return "done"

    monkeypatch.setattr(sessiond, "execute_python", fake_python)
    sessiond.process_eval_request(_request(bridge, "owned", language="python", channel="chan"), bridge, runtime)
    _wait_until(lambda: sessiond.ACTIVE_EVALS.get("owned", {}).get("running") is True)
    (bridge / "eval_owned.cancel.json").write_text(json.dumps({"host_owner_id": "foreign", "reason": "interrupted"}))
    sessiond.service_cancel_requests(bridge)
    ack = json.loads((bridge / "eval_owned.cancel.ack.json").read_text())
    assert ack["state"] == "owner_mismatch"
    assert not sessiond.ACTIVE_EVALS["owned"]["cancel"].is_set()
    release.set()
    _wait_until(lambda: (bridge / "eval_owned.res.json").exists())


def test_request_for_replaced_daemon_generation_is_not_executed(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    request = _request(bridge, "old-generation", language="python", channel="chan", code="side_effect")
    payload = json.loads(request.read_text())
    payload["daemon_generation"] = "replaced-generation"
    request.write_text(json.dumps(payload))
    calls: list[str] = []
    monkeypatch.setattr(sessiond, "execute_python", lambda code, *_a, **_k: calls.append(code) or code)

    sessiond.process_eval_request(request, bridge, runtime)

    response = json.loads((bridge / "eval_old-generation.res.json").read_text())
    assert response["reason"] == "daemon_restarted"
    assert calls == []
    assert not request.with_suffix(request.suffix + ".processing").exists()


def test_stale_processing_is_terminalized_not_replayed(tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    claimed = bridge / "eval_stale.req.json.processing"
    claimed.write_text('{"code":"side_effect()"}')
    sessiond.recover_stale_claims(bridge)
    response = json.loads((bridge / "eval_stale.res.json").read_text())
    assert response["reason"] == "daemon_restarted"
    assert "not replayed" in response["output"]
    assert not claimed.exists()


def test_bash_timeout_resets_to_reusable_channel(tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = Path(sessiond.__file__).resolve().parent

    timed = sessiond.execute_bash(
        "sleep 99", "shell", bridge, "token", runtime, timeout_sec=0.05,
    )
    assert timed.startswith("--- TIMEOUT ---")
    assert "shell" not in sessiond.BASH_REPLS

    after = sessiond.execute_bash(
        "echo after", "shell", bridge, "token", runtime, timeout_sec=1,
    )
    assert "after" in after


@pytest.mark.parametrize("language", ["python", "bash"])
@pytest.mark.parametrize("cancel_reason", ["timeout", "interrupted"])
def test_cancel_teardown_failure_is_truthful_and_blocks_successor(
    monkeypatch, tmp_path, language, cancel_reason,
):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    request_id = f"{language}-{cancel_reason}"
    channel = "unsafe"
    key = f"{language}:{channel}"
    release = threading.Event()

    def blocked_eval(*_args, cancel_check=None, **_kwargs):
        while not (cancel_check and cancel_check()):
            time.sleep(0.005)
        release.wait(2)
        return "should not become terminal success"

    monkeypatch.setattr(
        sessiond,
        "execute_python" if language == "python" else "execute_bash",
        blocked_eval,
    )
    monkeypatch.setattr(
        sessiond,
        "_kill_python_worker" if language == "python" else "_terminate_bash_channel",
        lambda *_a, **_k: (
            sessiond._mark_channel_teardown_failed(key, "permission denied") or False
        ),
    )

    sessiond.process_eval_request(
        _request(bridge, request_id, language=language, channel=channel, code="side_effect"),
        bridge,
        runtime,
    )
    _wait_until(lambda: sessiond.ACTIVE_EVALS.get(request_id, {}).get("running") is True)
    (bridge / f"eval_{request_id}.cancel.json").write_text(json.dumps({
        "host_owner_id": "host-a", "reason": cancel_reason,
    }))

    sessiond.service_cancel_requests(bridge)

    response = json.loads((bridge / f"eval_{request_id}.res.json").read_text())
    assert response["ok"] is False
    assert response["reason"] == "containment_failure"
    assert response["output"].startswith("--- CONTAINMENT FAILURE ---")
    assert "channel was reset" not in response["output"]
    status = sessiond._daemon_status_payload()
    assert status["channel_state"][key]["state"] == "reap_failed"
    assert status["channel_state"][key]["reap_reason"] == "teardown_failed"
    assert status["channel_state"][key]["reap_error"] == "permission denied"

    successor = _request(
        bridge, f"next-{request_id}", language=language, channel=channel, code="successor",
    )
    sessiond.process_eval_request(successor, bridge, runtime)
    successor_response = json.loads(
        (bridge / f"eval_next-{request_id}.res.json").read_text()
    )
    assert successor_response["reason"] == "containment_failure"
    assert "quarantined" in successor_response["output"]

    release.set()
    _wait_until(lambda: request_id not in sessiond.ACTIVE_EVALS)


@pytest.mark.parametrize("language", ["python", "bash"])
def test_execute_timeout_teardown_failure_reports_containment(monkeypatch, tmp_path, language):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    channel = "timeout-unsafe"
    key = f"{language}:{channel}"

    if language == "python":
        class Conn:
            def send(self, _payload):
                pass
            def poll(self, _timeout):
                return False
        proc = SimpleNamespace(is_alive=lambda: True)
        monkeypatch.setattr(sessiond, "_get_python_worker", lambda _channel: (proc, Conn()))
        monkeypatch.setattr(
            sessiond,
            "_kill_python_worker",
            lambda *_a, **_k: (
                sessiond._mark_channel_teardown_failed(key, "still alive") or False
            ),
        )
        output = sessiond.execute_python(
            "side_effect", channel, bridge, "token", runtime, timeout_sec=0.01,
        )
    else:
        class Stdin:
            def write(self, _value):
                pass
            def flush(self):
                pass
        proc = SimpleNamespace(stdin=Stdin(), stdout=object())
        monkeypatch.setattr(sessiond, "_bash_proc", lambda *_a, **_k: proc)
        monkeypatch.setattr(sessiond.select, "select", lambda *_a, **_k: ([], [], []))
        monkeypatch.setattr(
            sessiond,
            "_terminate_bash_channel",
            lambda *_a, **_k: (
                sessiond._mark_channel_teardown_failed(key, "still alive") or False
            ),
        )
        output = sessiond.execute_bash(
            "side_effect", channel, bridge, "token", runtime, timeout_sec=0.01,
        )

    assert output.startswith("--- CONTAINMENT FAILURE ---")
    assert "may still be running side effects" in output
    assert sessiond.CHANNEL_ACTIVITY[key]["reap_reason"] == "teardown_failed"
    assert sessiond.CHANNEL_ACTIVITY[key]["reap_error"] == "still alive"


@pytest.mark.parametrize("language", ["python", "bash"])
def test_execute_cancel_teardown_failure_reports_containment(monkeypatch, tmp_path, language):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    channel = "cancel-unsafe"
    key = f"{language}:{channel}"

    if language == "python":
        conn = SimpleNamespace(send=lambda _payload: None, poll=lambda _timeout: False)
        proc = SimpleNamespace(is_alive=lambda: True)
        monkeypatch.setattr(sessiond, "_get_python_worker", lambda _channel: (proc, conn))
        monkeypatch.setattr(
            sessiond,
            "_kill_python_worker",
            lambda *_a, **_k: (
                sessiond._mark_channel_teardown_failed(key, "still alive") or False
            ),
        )
        output = sessiond.execute_python(
            "side_effect", channel, bridge, "token", runtime,
            cancel_check=lambda: True,
        )
    else:
        stdin = SimpleNamespace(write=lambda _value: None, flush=lambda: None)
        proc = SimpleNamespace(stdin=stdin, stdout=object())
        monkeypatch.setattr(sessiond, "_bash_proc", lambda *_a, **_k: proc)
        monkeypatch.setattr(
            sessiond,
            "_terminate_bash_channel",
            lambda *_a, **_k: (
                sessiond._mark_channel_teardown_failed(key, "still alive") or False
            ),
        )
        output = sessiond.execute_bash(
            "side_effect", channel, bridge, "token", runtime,
            cancel_check=lambda: True,
        )

    assert output.startswith("--- CONTAINMENT FAILURE ---")
    assert "cancelled" in output
    assert sessiond.CHANNEL_ACTIVITY[key]["reap_error"] == "still alive"


@pytest.mark.parametrize("language", ["python", "bash"])
def test_real_eval_timeout_teardown_failure_leaves_channel_quarantined_until_verified_cleanup(
    monkeypatch, tmp_path, language,
):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    channel = f"real-{language}-timeout"
    key = f"{language}:{channel}"
    real_teardown = sessiond._kill_and_verify_process_group
    monkeypatch.setattr(
        sessiond,
        "_kill_and_verify_process_group",
        lambda *_a, **_k: (False, "injected verification failure"),
    )

    if language == "python":
        output = sessiond.execute_python(
            "import time; time.sleep(10)", channel, bridge, "token", runtime,
            timeout_sec=0.05,
        )
        assert sessiond.PY_WORKERS[channel][0].is_alive()
        with pytest.raises(RuntimeError, match="quarantined"):
            sessiond._get_python_worker(channel)
    else:
        output = sessiond.execute_bash(
            "sleep 10", channel, bridge, "token", runtime, timeout_sec=0.05,
        )
        pgid = sessiond.CHANNEL_PROCESS_META[key]["pgid"]
        assert sessiond._process_group_has_live_members(pgid)
        with pytest.raises(RuntimeError, match="quarantined"):
            sessiond._bash_proc(channel, bridge, "token", runtime)

    assert output.startswith("--- CONTAINMENT FAILURE ---")
    assert sessiond.CHANNEL_ACTIVITY[key]["reap_reason"] == "teardown_failed"
    payload = sessiond._daemon_status_payload()
    assert payload["channel_state"][key]["state"] == "reap_failed"

    # Account for the intentionally live real process: only a later verified
    # containment attempt clears quarantine and permits channel replacement.
    monkeypatch.setattr(sessiond, "_kill_and_verify_process_group", real_teardown)
    if language == "python":
        assert sessiond._kill_python_worker(channel)
    else:
        assert sessiond._terminate_bash_channel(channel)
    assert key not in sessiond.CHANNEL_PROCESS_META
    assert key not in sessiond.CHANNEL_ACTIVITY
