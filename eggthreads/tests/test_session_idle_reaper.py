from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import eggthreads as ts
import eggthreads.session as session


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _auto_session(db: ts.ThreadsDB, *, share_with_children: bool = False) -> tuple[str, str]:
    thread_id = ts.create_root_thread(db, name="runtime")
    session_id = ts.set_thread_session_config(
        db,
        thread_id,
        enabled=True,
        provider="docker",
        share="private",
        owner_thread_id=thread_id,
        share_with_children_default=share_with_children,
        reason="auto:python_repl:python",
    )
    return thread_id, session_id


def _status(
    cfg: session.SessionConfig,
    *,
    state: str = "ready",
    last_activity: float | None = 0.0,
    active_requests=(),
) -> session.SessionStatus:
    return session.SessionStatus(
        True,
        "docker",
        cfg.session_id,
        state,
        container_name="egg-test",
        active_requests=tuple(active_requests),
        last_activity=last_activity,
        heartbeat_at=time.time(),
        daemon_generation="generation-a",
    )


def test_idle_policy_is_disabled_for_unset_invalid_and_nonpositive_values(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    _auto_session(db)
    calls = []
    monkeypatch.delenv("EGG_RLM_AUTO_SESSION_IDLE_TIMEOUT", raising=False)
    monkeypatch.setattr(session, "get_thread_session_status", lambda *_a: calls.append("status"))

    assert ts.reap_idle_auto_docker_sessions(db) == []
    for raw in ("", "off", "invalid", "0", "-1s", "nan", "inf"):
        assert ts.auto_session_idle_timeout_sec(raw) is None
        assert ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=raw) == []
    assert calls == []


def test_idle_policy_parses_duration_and_respects_threshold(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    thread_id, _session_id = _auto_session(db)
    cfg = ts.get_thread_session_config(db, thread_id)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(cfg, last_activity=951.0),
    )
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    assert ts.auto_session_idle_timeout_sec("2m") == 120.0
    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec="50s", now=1000.0)

    assert result[0]["reason"] == "below_idle_threshold"
    assert result[0]["idle_for_sec"] == 49.0
    assert stops == []


def test_last_activity_not_container_creation_age_authorizes_reclamation(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    thread_id, _session_id = _auto_session(db)
    cfg = ts.get_thread_session_config(db, thread_id)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(cfg, last_activity=1.0),
    )
    monkeypatch.setattr(
        session,
        "_docker_container_created_at",
        lambda *_a: (_ for _ in ()).throw(AssertionError("creation age must not be read")),
    )
    monkeypatch.setattr(
        session,
        "stop_thread_session",
        lambda *_a, **_k: session.SessionStatus(True, "docker", cfg.session_id, "stopped"),
    )

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert result[0]["reclaimed"] is True
    assert result[0]["last_activity_at"] == 1.0


def test_only_auto_created_private_session_is_eligible(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    auto_thread, auto_session_id = _auto_session(db)
    explicit_thread = ts.create_root_thread(db, name="explicit")
    explicit_session_id = ts.enable_thread_session(
        db, explicit_thread, provider="docker", reason="/sessionOn"
    )
    shared_default_thread, _shared_default_id = _auto_session(db, share_with_children=True)
    stopped = []

    def fake_status(_db, thread_id):
        return _status(ts.get_thread_session_config(db, thread_id), last_activity=1.0)

    def fake_stop(_db, thread_id, *, reason):
        stopped.append((thread_id, reason))
        cfg = ts.get_thread_session_config(db, thread_id)
        return session.SessionStatus(True, "docker", cfg.session_id, "stopped")

    monkeypatch.setattr(session, "get_thread_session_status", fake_status)
    monkeypatch.setattr(session, "stop_thread_session", fake_stop)

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert [item["session_id"] for item in result] == [auto_session_id]
    assert stopped == [(auto_thread, "idle_reap:10s")]
    assert explicit_session_id not in {item["session_id"] for item in result}
    assert shared_default_thread not in {item["thread_id"] for item in result}


def test_active_eval_and_host_activity_lock_protect_session(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    thread_id, session_id = _auto_session(db)
    cfg = ts.get_thread_session_config(db, thread_id)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(
            cfg,
            state="ready",
            last_activity=1.0,
            active_requests=({"request_id": "eval-a", "state": "running"},),
        ),
    )
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    active = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)
    assert active[0]["reason"] == "active_requests"
    assert stops == []

    entered = threading.Event()
    release = threading.Event()

    def hold_activity():
        with session._session_activity_guard(session_id):
            entered.set()
            release.wait(2)

    holder = threading.Thread(target=hold_activity)
    holder.start()
    assert entered.wait(1)
    try:
        locked = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)
        assert locked[0]["reason"] == "host_activity"
    finally:
        release.set()
        holder.join(2)


def test_live_explicit_shared_reference_pins_auto_session(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    auto_thread, session_id = _auto_session(db)
    shared_thread = ts.create_root_thread(db, name="shared")
    ts.set_thread_session_config(
        db,
        shared_thread,
        enabled=True,
        provider="docker",
        share="session",
        session_id=session_id,
        owner_thread_id=auto_thread,
        reason="spawn_agent share_session",
    )
    cfg = ts.get_thread_session_config(db, auto_thread)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(cfg, last_activity=1.0),
    )
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert result[0]["reason"] == "shared_references"
    assert set(result[0]["reference_thread_ids"]) == {auto_thread, shared_thread}
    assert stops == []


def test_missing_stopped_busy_and_unhealthy_status_fail_closed(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    thread_id, _session_id = _auto_session(db)
    cfg = ts.get_thread_session_config(db, thread_id)
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    for state in ("missing", "stopped", "busy", "unhealthy"):
        monkeypatch.setattr(
            session,
            "get_thread_session_status",
            lambda *_a, state=state: _status(cfg, state=state, last_activity=1.0),
        )
        result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)
        assert result[0]["reason"] == f"session_{state}"
    assert stops == []


def test_inherited_shared_reference_from_share_default_pins_candidate(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    auto_thread, session_id = _auto_session(db)
    child = ts.create_child_thread(db, auto_thread, name="child")
    payload = session._local_session_config_payload(db, auto_thread)
    payload["share_with_children_default"] = True
    db.conn.execute(
        "UPDATE events SET payload_json=? WHERE thread_id=? AND type='session.config'",
        (json.dumps(payload), auto_thread),
    )
    # Exercise reference resolution directly: eligibility itself would pin an
    # auto config once sharing-by-default is enabled.
    refs, error = session._session_reference_thread_ids(db, session_id)
    assert error == ""
    assert set(refs) == {auto_thread, child}


def test_inherited_runtime_reference_pins_auto_session(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    auto_thread, session_id = _auto_session(db)
    runtime_thread = ts.create_child_thread(db, auto_thread, name="runtime")
    db.append_event(
        event_id="runtime-kind",
        thread_id=runtime_thread,
        type_="runtime.thread",
        payload={"language": "python", "name": "default"},
    )
    cfg = ts.get_thread_session_config(db, auto_thread)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(cfg, last_activity=1.0),
    )
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert result[0]["reason"] == "shared_references"
    assert set(result[0]["reference_thread_ids"]) == {auto_thread, runtime_thread}
    assert session_id == cfg.session_id
    assert stops == []


def test_reference_scan_failure_fails_closed(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    _auto_thread, _session_id = _auto_session(db)
    monkeypatch.setattr(
        session,
        "_strict_effective_session_reference",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("bad config")),
    )
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert result[0]["reason"] == "reference_scan_failed"
    assert "bad config" in result[0]["error"]
    assert stops == []


def test_stale_missing_and_unhealthy_phase4_status_never_authorize_stop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, session_id = _auto_session(db)
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    monkeypatch.setattr(
        session,
        "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    bridge = session._session_bridge_dir(session_id)
    (bridge / "sessiond_generation.json").write_text(json.dumps({"daemon_generation": "generation-a"}))
    (bridge / "sessiond_status.json").write_text(json.dumps({
        "daemon_generation": "generation-a",
        "heartbeat_at": time.time() - session._DOCKER_HEARTBEAT_STALE_SEC - 1,
        "last_activity_at": 1.0,
        "active_requests": [],
        "channel_state": {},
    }))
    stops = []
    monkeypatch.setattr(session, "stop_thread_session", lambda *_a, **_k: stops.append(True))

    stale = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)
    assert stale[0]["reason"] == "session_unhealthy"

    (bridge / "sessiond_status.json").unlink()
    missing = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)
    assert missing[0]["reason"] == "session_unhealthy"
    assert stops == []


def test_successful_verified_stop_is_reported_and_records_idle_reason(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, _session_id = _auto_session(db)
    cfg = ts.get_thread_session_config(db, thread_id)
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(cfg, last_activity=1.0),
    )
    states = iter([
        session._DockerContainerState(True, True, "running"),
        session._DockerContainerState(True, False, "exited"),
    ])
    monkeypatch.setattr(session, "_docker_container_state", lambda _name: next(states))
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout="container", stderr=""),
    )

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert result[0]["reclaimed"] is True
    assert result[0]["status"] == "stopped"
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' "
        "ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    lifecycle = json.loads(row[0])
    assert lifecycle["action"] == "stopped"
    assert lifecycle["reason"] == "idle_reap:10s"
    assert lifecycle["verified_stopped"] is True


def test_failed_unverified_stop_is_reported_not_reclaimed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, _session_id = _auto_session(db)
    cfg = ts.get_thread_session_config(db, thread_id)
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    monkeypatch.setattr(
        session,
        "get_thread_session_status",
        lambda *_a: _status(cfg, last_activity=1.0),
    )
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

    result = ts.reap_idle_auto_docker_sessions(db, idle_timeout_sec=10, now=100.0)

    assert result[0]["reclaimed"] is False
    assert result[0]["status"] == "unhealthy"
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' "
        "ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    lifecycle = json.loads(row[0])
    assert lifecycle["action"] == "stop_error"
    assert lifecycle["reason"] == "idle_reap:10s"
    assert lifecycle["verified_stopped"] is False


def test_background_reaper_is_disabled_for_memory_db_and_duplicate_file_pass(monkeypatch, tmp_path):
    monkeypatch.setenv("EGG_RLM_AUTO_SESSION_IDLE_TIMEOUT", "1h")
    memory_db = ts.ThreadsDB(":memory:")
    memory_db.init_schema()
    assert ts.start_idle_auto_docker_reaper(memory_db) is False

    db = _make_db(tmp_path)
    started = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            started.append((target, name, daemon))

        def start(self):
            pass

    monkeypatch.setattr(session.threading, "Thread", FakeThread)
    try:
        assert ts.start_idle_auto_docker_reaper(db) is True
        assert ts.start_idle_auto_docker_reaper(db) is False
        assert len(started) == 1
        assert started[0][2] is True
    finally:
        session._IDLE_REAPER_DATABASES.clear()
