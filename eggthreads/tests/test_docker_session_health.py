from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import eggthreads as ts
import eggthreads.session as session
from eggthreads.builtin_plugins.session import format_session_status
from eggthreads.session_runtime import sessiond


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _configured_docker_session(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")
    session_id = ts.enable_thread_session(db, thread_id, provider="docker")
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    return db, thread_id, session_id


def _write_health(session_id: str, **overrides) -> Path:
    bridge = session._session_bridge_dir(session_id)
    now = time.time()
    payload = {
        "protocol_version": 2,
        "daemon_generation": "generation-a",
        "started_at": now - 1,
        "heartbeat_at": now,
        "last_activity_at": now - 0.5,
        "active_requests": [],
        "channel_state": {"python:default": {"state": "ready"}},
        **overrides,
    }
    (bridge / "sessiond_generation.json").write_text(json.dumps({
        "protocol_version": 2,
        "daemon_generation": payload["daemon_generation"],
        "started_at": payload["started_at"],
    }))
    (bridge / "sessiond_status.json").write_text(json.dumps(payload))
    return bridge


def _latest_lifecycle(db: ts.ThreadsDB, thread_id: str) -> dict:
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' "
        "ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_daemon_status_snapshot_reports_generation_requests_channels_and_activity(tmp_path, monkeypatch):
    status_path = tmp_path / "sessiond_status.json"
    now = time.time()
    monkeypatch.setattr(sessiond, "STATUS_PATH", status_path)
    monkeypatch.setattr(sessiond, "LAST_ACTIVITY_AT", now - 10)
    monkeypatch.setattr(sessiond, "PY_WORKERS", {"idle": (SimpleNamespace(), None)})
    monkeypatch.setattr(sessiond, "BASH_REPLS", {})
    sessiond.ACTIVE_EVALS["req-a"] = {
        "payload": {"language": "python", "channel": "work", "created_at": now - 2},
        "running": True,
        "cancel_reason": None,
    }
    sessiond.CHANNEL_QUEUES["python:work"] = ["req-a"]

    try:
        sessiond.write_daemon_status(activity=True)
        payload = json.loads(status_path.read_text())
        assert payload["daemon_generation"] == sessiond.DAEMON_GENERATION
        assert payload["active_requests"] == [{
            "request_id": "req-a",
            "language": "python",
            "channel": "work",
            "state": "running",
            "created_at": now - 2,
            "cancel_reason": None,
        }]
        assert payload["channel_state"]["python:work"]["state"] == "busy"
        assert payload["channel_state"]["python:idle"]["state"] == "ready"
        assert payload["last_activity_at"] >= now
    finally:
        sessiond.ACTIVE_EVALS.clear()
        sessiond.CHANNEL_QUEUES.clear()


def test_docker_status_reports_missing_ready_busy_and_unhealthy(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)

    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(False, False, "missing"),
    )
    assert ts.get_thread_session_status(db, thread_id).status == "missing"

    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    _write_health(session_id)
    ready = ts.get_thread_session_status(db, thread_id)
    assert ready.status == "ready"
    assert ready.daemon_generation == "generation-a"
    assert ready.channel_state["python:default"]["state"] == "ready"

    _write_health(
        session_id,
        active_requests=[{
            "request_id": "req-b", "language": "bash", "channel": "shell", "state": "running",
        }],
        channel_state={
            "bash:shell": {
                "state": "busy", "running_request_id": "req-b", "queued_request_ids": [],
            },
        },
    )
    busy = ts.get_thread_session_status(db, thread_id)
    assert busy.status == "busy"
    assert busy.active_requests[0]["request_id"] == "req-b"
    rendered = format_session_status(thread_id, db=db)
    assert "Status: busy" in rendered
    assert "Daemon generation: generation-a" in rendered
    assert "Active requests: 1" in rendered

    stale_at = time.time() - session._DOCKER_HEARTBEAT_STALE_SEC - 1
    _write_health(session_id, heartbeat_at=stale_at, last_activity_at=stale_at - 1)
    unhealthy = ts.get_thread_session_status(db, thread_id)
    assert unhealthy.status == "unhealthy"
    assert "stale" in unhealthy.message


def test_docker_status_rejects_generation_mismatch(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    bridge = _write_health(session_id, daemon_generation="status-generation")
    (bridge / "sessiond_generation.json").write_text(json.dumps({
        "daemon_generation": "announced-generation",
        "started_at": time.time(),
    }))

    status = ts.get_thread_session_status(db, thread_id)

    assert status.status == "unhealthy"
    assert "generation" in status.message
    assert status.daemon_generation == "status-generation"


def test_docker_status_rejects_malformed_authority_fields(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    now = time.time()
    malformed = [
        {"heartbeat_at": True},
        {"heartbeat_at": float("nan")},
        {"heartbeat_at": 0},
        {"last_activity_at": True},
        {"last_activity_at": float("inf")},
        {"last_activity_at": -1},
        {"active_requests": ["not-an-object"]},
        {"active_requests": [{}]},
        {"active_requests": [{
            "request_id": "req", "language": "python", "channel": "c", "state": "done",
        }]},
        {"channel_state": {"python:c": "ready"}},
        {"channel_state": {"python:c": {"state": "unknown"}}},
        {"channel_state": {"python:c": {"state": "busy", "queued_request_ids": "req"}}},
    ]

    for override in malformed:
        _write_health(
            session_id,
            **{"heartbeat_at": now, "last_activity_at": now - 1, **override},
        )
        status = ts.get_thread_session_status(db, thread_id)
        assert status.status == "unhealthy", override


def test_docker_status_rejects_cross_field_request_channel_contradictions(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    request = {
        "request_id": "req-a", "language": "python", "channel": "work", "state": "running",
    }
    contradictions = [
        ([request], {}),
        ([], {"python:work": {"state": "busy", "running_request_id": "req-a", "queued_request_ids": []}}),
        ([request], {"python:work": {"state": "busy", "running_request_id": None, "queued_request_ids": ["req-a"]}}),
        ([request], {"bash:work": {"state": "busy", "running_request_id": "req-a", "queued_request_ids": []}}),
        ([request], {"python:other": {"state": "busy", "running_request_id": "req-a", "queued_request_ids": []}}),
        ([request], {"python:work": {"state": "ready", "running_request_id": "req-a"}}),
    ]
    for active, channels in contradictions:
        _write_health(session_id, active_requests=active, channel_state=channels)
        assert ts.get_thread_session_status(db, thread_id).status == "unhealthy"

    queued = {
        "request_id": "req-q", "language": "python", "channel": "work", "state": "queued",
    }
    _write_health(
        session_id,
        active_requests=[request, queued],
        channel_state={
            "python:work": {
                "state": "busy",
                "running_request_id": "req-a",
                "queued_request_ids": ["req-q"],
            },
        },
    )
    valid = ts.get_thread_session_status(db, thread_id)
    assert valid.status == "busy"
    assert {item["request_id"] for item in valid.active_requests} == {"req-a", "req-q"}


def test_docker_status_rejects_malformed_generation_authority(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    bridge = _write_health(session_id)
    generation_path = bridge / "sessiond_generation.json"
    for raw in ("[]", "{}", "not json"):
        generation_path.write_text(raw)
        status = ts.get_thread_session_status(db, thread_id)
        assert status.status == "unhealthy"
        assert "generation" in status.message


def test_docker_status_reports_stopped_from_observed_container_state(tmp_path, monkeypatch):
    db, thread_id, _session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, False, "exited"),
    )

    status = ts.get_thread_session_status(db, thread_id)

    assert status.status == "stopped"
    assert "exited" in status.message



def test_unhealthy_running_session_is_restarted_before_use(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    _write_health(
        session_id,
        heartbeat_at=time.time() - session._DOCKER_HEARTBEAT_STALE_SEC - 1,
    )
    starts = []

    def restart(*args):
        starts.append(args)
        _write_health(session_id, daemon_generation="generation-b")
        return True

    monkeypatch.setattr(session, "_start_docker_container", restart)

    status = ts.get_or_start_docker_session(db, thread_id)

    assert status.status == "ready"
    assert status.reason == "daemon_unhealthy"
    assert starts and starts[0][-1] is True
    event = _latest_lifecycle(db, thread_id)
    assert event["action"] == "docker_restarted"
    assert event["previous_status"] == "unhealthy"
    assert event["reason"] == "daemon_unhealthy"


def test_start_clears_stale_daemon_records_before_docker_run(tmp_path, monkeypatch):
    db, thread_id, session_id = _configured_docker_session(tmp_path, monkeypatch)
    cfg = ts.get_thread_session_config(db, thread_id)
    bridge = _write_health(session_id, daemon_generation="stale")
    runtime = session._session_runtime_dir(session_id)
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: None)
    observed = []

    def fake_run(argv, **_kwargs):
        if argv[1] == "run":
            observed.append((
                (bridge / "sessiond_generation.json").exists(),
                (bridge / "sessiond_status.json").exists(),
            ))
        return subprocess.CompletedProcess(argv, 0, stdout="container", stderr="")

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    assert session._start_docker_container(
        db,
        thread_id,
        cfg,
        session.docker_session_container_name(db, session_id),
        bridge,
        runtime,
    ) is True
    assert observed == [(False, False)]


def test_docker_stop_verifies_state_and_records_reason(tmp_path, monkeypatch):
    db, thread_id, _session_id = _configured_docker_session(tmp_path, monkeypatch)
    states = iter([
        session._DockerContainerState(True, True, "running"),
        session._DockerContainerState(True, False, "exited"),
    ])
    monkeypatch.setattr(session, "_docker_container_state", lambda _name: next(states))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="container\n", stderr="")

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    status = ts.stop_thread_session(db, thread_id, reason="timeout")

    assert status.status == "stopped"
    assert [call[0][1] for call in calls] == ["stop"]
    event = _latest_lifecycle(db, thread_id)
    assert event["action"] == "stopped"
    assert event["reason"] == "timeout"
    assert event["verified_stopped"] is True
    assert event["kill_fallback"] is False


def test_docker_stop_uses_bounded_kill_fallback_after_failed_stop(tmp_path, monkeypatch):
    db, thread_id, _session_id = _configured_docker_session(tmp_path, monkeypatch)
    states = iter([
        session._DockerContainerState(True, True, "running"),
        session._DockerContainerState(True, True, "running"),
        session._DockerContainerState(True, False, "exited"),
    ])
    monkeypatch.setattr(session, "_docker_container_state", lambda _name: next(states))
    monkeypatch.setattr(session, "_DOCKER_STOP_VERIFY_SEC", 0)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        code = 1 if argv[1] == "stop" else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="stop failed" if code else "")

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    status = ts.stop_thread_session(db, thread_id, reason="cancelled")

    assert status.status == "stopped"
    assert [call[0][1] for call in calls] == ["stop", "kill"]
    assert calls[0][1]["timeout"] == session._DOCKER_STOP_TIMEOUT_SEC
    assert calls[1][1]["timeout"] == session._DOCKER_KILL_TIMEOUT_SEC
    event = _latest_lifecycle(db, thread_id)
    assert event["kill_fallback"] is True
    assert event["stop_error"] == "stop failed"
    assert event["reason"] == "cancelled"


def test_docker_stop_timeout_still_attempts_bounded_kill(tmp_path, monkeypatch):
    db, thread_id, _session_id = _configured_docker_session(tmp_path, monkeypatch)
    states = iter([
        session._DockerContainerState(True, True, "running"),
        session._DockerContainerState(True, True, "running"),
        session._DockerContainerState(True, False, "exited"),
    ])
    monkeypatch.setattr(session, "_docker_container_state", lambda _name: next(states))
    monkeypatch.setattr(session, "_DOCKER_STOP_VERIFY_SEC", 0)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "stop":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    status = ts.stop_thread_session(db, thread_id, reason="timeout")

    assert status.status == "stopped"
    assert [call[0][1] for call in calls] == ["stop", "kill"]
    event = _latest_lifecycle(db, thread_id)
    assert event["stop_error"] == f"docker stop timed out after {session._DOCKER_STOP_TIMEOUT_SEC:g}s"
    assert event["kill_fallback"] is True


def test_docker_stop_does_not_claim_success_while_container_still_runs(tmp_path, monkeypatch):
    db, thread_id, _session_id = _configured_docker_session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session,
        "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    monkeypatch.setattr(session, "_DOCKER_STOP_VERIFY_SEC", 0)
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"{argv[1]} failed"),
    )

    status = ts.stop_thread_session(db, thread_id, reason="restart")

    assert status.status == "unhealthy"
    event = _latest_lifecycle(db, thread_id)
    assert event["action"] == "stop_error"
    assert event["verified_stopped"] is False
    assert event["reason"] == "restart"
    assert "kill failed" in event["error"]
