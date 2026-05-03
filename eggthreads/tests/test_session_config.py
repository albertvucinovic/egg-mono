from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_session_config_defaults_disabled(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    cfg = ts.get_thread_session_config(db, tid)
    assert cfg.enabled is False
    assert cfg.provider == "docker"
    assert cfg.session_id is None
    assert cfg.share_repl is False


def test_enable_thread_session_appends_config_and_stable_id(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    sid = ts.enable_thread_session(db, tid, image="custom-image", share_with_children_default=True, share_repl=True)
    cfg = ts.get_thread_session_config(db, tid)

    assert cfg.enabled is True
    assert cfg.session_id == sid
    assert sid.startswith("sess_")
    assert cfg.image == "custom-image"
    assert cfg.share_with_children_default is True
    assert cfg.share_repl is True
    assert cfg.owner_thread_id == tid

    sid2 = ts.enable_thread_session(db, tid, image="custom-image")
    assert sid2 == sid


def test_session_config_inherits_to_runtime_child(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, share="private")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")

    cfg = ts.get_thread_session_config(db, runtime)
    assert cfg.enabled is True
    assert cfg.session_id == sid
    assert cfg.source == f"event:{parent}"


def test_repl_channel_defaults_per_runtime_thread():
    assert ts.repl_channel_name("thread-A", "default") != ts.repl_channel_name("thread-B", "default")
    assert ts.repl_channel_name("thread-A", "default", share_repl=True) == "default"


def test_reset_thread_session_rotates_session_id_and_preserves_policy(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    old_sid = ts.enable_thread_session(db, tid, provider="memory", share_repl=True)

    new_sid = ts.reset_thread_session(db, tid, reason="test")

    assert new_sid != old_sid
    cfg = ts.get_thread_session_config(db, tid)
    assert cfg.enabled is True
    assert cfg.provider == "memory"
    assert cfg.share_repl is True
    assert cfg.session_id == new_sid


def test_stop_thread_session_clears_memory_repl_state(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.enable_thread_session(db, tid, provider="memory")

    assert "5" in ts.execute_python_repl(db, tid, "x = 5\nx")
    runtime = ts.find_runtime_thread(db, tid, language="python")
    assert runtime is not None
    st = ts.stop_thread_session(db, runtime.runtime_thread_id, reason="test")
    assert st.status == "stopped"
    out = ts.execute_python_repl(db, tid, "x")
    assert "NameError" in out


def test_child_can_share_specific_parent_session(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    parent_sid = ts.enable_thread_session(db, parent)
    child = ts.create_child_thread(db, parent, name="child")

    ts.set_thread_session_config(
        db,
        child,
        enabled=True,
        share="session",
        session_id=parent_sid,
        owner_thread_id=parent,
        reason="test-share",
    )

    cfg = ts.get_thread_session_config(db, child)
    assert cfg.enabled is True
    assert cfg.share == "session"
    assert cfg.session_id == parent_sid
    assert cfg.owner_thread_id == parent


def test_session_lifecycle_event(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    sid = ts.enable_thread_session(db, tid)

    ts.append_session_lifecycle_event(
        db,
        tid,
        action="started",
        session_id=sid,
        payload={"container_name": "egg-rlm-test"},
    )

    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["action"] == "started"
    assert payload["session_id"] == sid
    assert payload["container_name"] == "egg-rlm-test"


def test_docker_session_status_skeleton_when_available(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    sid = ts.enable_thread_session(db, tid, provider="docker", image="egg-rlm-session")
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: True)
    start_calls = []
    monkeypatch.setattr(ts.eggthreads.session, "_start_docker_container", lambda *a, **k: start_calls.append((a, k)))

    status = ts.get_thread_session_status(db, tid)
    assert status.enabled is True
    assert status.provider == "docker"
    assert status.status == "available"
    assert status.session_id == sid
    assert status.container_name is not None
    assert status.container_name.startswith("egg-rlm-")

    status2 = ts.get_or_start_docker_session(db, tid)
    assert status2.container_name == status.container_name
    assert start_calls
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["action"] in ("docker_started", "docker_skeleton_ready")
    assert payload["container_name"] == status.container_name


def test_docker_session_mount_dir_uses_thread_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    workdir = tmp_path / "thread-work"
    ts.set_thread_working_directory(db, tid, str(workdir))
    sid = ts.enable_thread_session(db, tid, provider="docker")
    cfg = ts.get_thread_session_config(db, tid)

    assert ts.docker_session_mount_dir(db, tid, cfg) == workdir.resolve()
    assert sid


def test_start_docker_container_masks_egg_and_outputs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    sid = ts.enable_thread_session(db, tid, provider="docker", image="python:3.12-slim")
    cfg = ts.get_thread_session_config(db, tid)
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    calls = []
    monkeypatch.setattr(ts.eggthreads.session, "_docker_inspect_running", lambda name: None)
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: True)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(ts.eggthreads.session.subprocess, "run", fake_run)

    ts.eggthreads.session._start_docker_container(db, tid, cfg, "egg-test", bridge, runtime)

    argv = calls[-1]
    joined = "\n".join(argv)
    assert f"{tmp_path.resolve()}:/workspace" in joined
    assert ":/workspace/.egg:ro" in joined
    assert ":/workspace/.egg_outputs:ro" in joined
    assert f"{tmp_path.resolve()}/.egg_outputs/{tid}:/workspace/.egg_outputs/{tid}:ro" in joined
    assert "--user" in argv
    assert f"egg.db_hash={ts.docker_session_db_hash(db)}" in joined
    assert sid


def test_start_docker_container_applies_sandbox_mount_policy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.set_thread_sandbox_config(
        db,
        tid,
        enabled=True,
        settings={
            "provider": "docker",
            "network": {"allowedDomains": []},
            "filesystem": {
                "allowWrite": ["writes"],
                "denyRead": ["secret"],
                "denyWrite": ["readonly"],
            },
        },
        reason="test",
    )
    cfg = ts.get_thread_session_config(db, tid)
    # Explicitly enable the REPL session after sandbox setup so this test only
    # inspects Docker argv construction.
    ts.enable_thread_session(db, tid, provider="docker", image="python:3.12-slim")
    cfg = ts.get_thread_session_config(db, tid)
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    calls = []
    monkeypatch.setattr(ts.eggthreads.session, "_docker_inspect_running", lambda name: None)
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: True)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(ts.eggthreads.session.subprocess, "run", fake_run)

    ts.eggthreads.session._start_docker_container(db, tid, cfg, "egg-test", bridge, runtime)

    argv = calls[-1]
    joined = "\n".join(argv)
    assert "--network\nnone" in joined
    assert f"{tmp_path.resolve()}:/workspace:ro" in joined
    assert f"{(tmp_path / 'writes').resolve()}:/workspace/writes" in joined
    assert ":/workspace/secret:ro" in joined
    assert ":/workspace/readonly:ro" in joined
    assert "--cap-drop\nALL" in joined


def test_start_docker_container_mounts_workspace_rw_when_allowwrite_omitted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.set_thread_sandbox_config(
        db,
        tid,
        enabled=True,
        settings={
            "provider": "docker",
            "network": "none",
            "filesystem": {"denyRead": ["secret"]},
        },
        reason="test",
    )
    ts.enable_thread_session(db, tid, provider="docker", image="python:3.12-slim")
    cfg = ts.get_thread_session_config(db, tid)
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    calls = []
    monkeypatch.setattr(ts.eggthreads.session, "_docker_inspect_running", lambda name: None)
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: True)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(ts.eggthreads.session.subprocess, "run", fake_run)

    ts.eggthreads.session._start_docker_container(db, tid, cfg, "egg-test", bridge, runtime)

    joined = "\n".join(calls[-1])
    assert f"{tmp_path.resolve()}:/workspace" in joined
    assert f"{tmp_path.resolve()}:/workspace:ro" not in joined
    assert ":/workspace/secret:ro" in joined


def test_tool_output_stash_is_thread_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    from eggthreads.runner import stash_tool_output_and_build_preview

    preview, saved = stash_tool_output_and_build_preview(
        db,
        tid,
        "tc_test",
        "\n".join(f"line {i}" for i in range(100)),
        max_lines=1,
        max_chars=20,
    )

    assert saved
    assert Path(saved).parent == tmp_path / ".egg_outputs" / tid
    assert f".egg_outputs/{tid}/" in preview


def test_docker_session_status_unavailable(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.enable_thread_session(db, tid, provider="docker")
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: False)

    status = ts.get_thread_session_status(db, tid)
    assert status.status == "unavailable"
    assert status.container_name is not None

    ts.get_or_start_docker_session(db, tid)
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["action"] == "docker_unavailable"


def test_session_dockerfile_and_build_script_exist():
    repo_root = Path(__file__).resolve().parents[1]
    sandbox_dockerfile = repo_root / "docker" / "Dockerfile"
    dockerfile = repo_root / "docker" / "Dockerfile.session"
    script = repo_root / "docker" / "create-session-image.sh"

    sandbox_text = sandbox_dockerfile.read_text(encoding="utf-8")
    assert "elan toolchain install leanprover/lean4:v4.29.1" in sandbox_text
    assert "COPY bin/applypatch" in sandbox_text

    assert dockerfile.exists()
    text = dockerfile.read_text(encoding="utf-8")
    assert "FROM ${BASE_IMAGE}" in text
    assert "egg-bridge" in text
    assert "sessiond.py" in text
    assert script.exists()
    script_text = script.read_text(encoding="utf-8")
    assert "Dockerfile.session" in script_text
    assert "--build-arg \"BASE_IMAGE=$BASE_IMAGE\"" in script_text
