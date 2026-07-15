from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts
import pytest


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_session_provider_registry_populated_by_plugin(tmp_path):
    registry = ts.create_session_provider_registry()

    assert registry.names() == ["memory", "docker"]
    assert registry.get("memory") is not None
    assert registry.get("missing") is None


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


def test_docker_session_start_returns_verified_daemon_health(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    sid = ts.enable_thread_session(db, tid, provider="docker", image="egg-rlm-session")
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: True)
    monkeypatch.setattr(
        ts.eggthreads.session,
        "_docker_container_state",
        lambda _name: ts.eggthreads.session._DockerContainerState(False, False, "missing"),
    )
    start_calls = []
    monkeypatch.setattr(
        ts.eggthreads.session,
        "_start_docker_container",
        lambda *a, **k: start_calls.append((a, k)) or True,
    )
    health = {
        "daemon_generation": "generation-a",
        "heartbeat_at": 2.0,
        "last_activity_at": 1.0,
        "active_requests": [],
        "channel_state": {},
    }
    monkeypatch.setattr(ts.eggthreads.session, "_wait_for_docker_daemon", lambda _bridge: (health, ""))
    monkeypatch.setattr(ts.eggthreads.session, "_docker_daemon_status", lambda _bridge: (health, ""))
    states = iter([
        ts.eggthreads.session._DockerContainerState(False, False, "missing"),
        ts.eggthreads.session._DockerContainerState(False, False, "missing"),
        ts.eggthreads.session._DockerContainerState(True, True, "running"),
    ])
    monkeypatch.setattr(ts.eggthreads.session, "_docker_container_state", lambda _name: next(states))
    monkeypatch.setattr(ts.eggthreads.session, "_docker_existing_resource_limits", lambda _name: ({}, ""))

    status = ts.get_thread_session_status(db, tid)
    assert status.enabled is True
    assert status.provider == "docker"
    assert status.status == "missing"
    assert status.session_id == sid
    assert status.container_name is not None
    assert status.container_name.startswith("egg-rlm-")

    status2 = ts.get_or_start_docker_session(db, tid)
    assert status2.container_name == status.container_name
    assert status2.status == "ready"
    assert status2.daemon_generation == "generation-a"
    assert start_calls
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["action"] == "docker_started"
    assert payload["container_name"] == status.container_name
    assert payload["previous_status"] == "missing"


def test_docker_session_identity_uses_sqlites_canonical_database_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    relative_db = ts.ThreadsDB(Path("same.sqlite"))
    relative_db.init_schema()
    absolute_db = ts.ThreadsDB((tmp_path / "same.sqlite").resolve())
    symlink = tmp_path / "same-link.sqlite"
    symlink.symlink_to(tmp_path / "same.sqlite")
    symlink_db = ts.ThreadsDB(symlink)

    session_id = "sess_same"
    expected_hash = ts.docker_session_db_hash(relative_db)
    expected_name = ts.docker_session_container_name(relative_db, session_id)

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    assert ts.docker_session_db_hash(relative_db) == expected_hash
    assert ts.docker_session_db_hash(absolute_db) == expected_hash
    assert ts.docker_session_db_hash(symlink_db) == expected_hash
    assert ts.docker_session_container_name(absolute_db, session_id) == expected_name
    assert ts.docker_session_container_name(symlink_db, session_id) == expected_name


def test_reconcile_docker_session_removes_duplicate_sharing_bridge_and_runtime(monkeypatch, tmp_path):
    session = ts.eggthreads.session
    canonical = "egg-rlm-canonical-sess-test"
    legacy = "egg-rlm-legacy-sess-test"
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    calls = []

    monkeypatch.setattr(session, "_docker_session_container_names", lambda _sid: [legacy, canonical])
    monkeypatch.setattr(
        session,
        "_docker_bind_mount_source",
        lambda _name, destination: str(bridge.resolve()) if destination == "/egg-bridge" else str(runtime.resolve()),
    )
    monkeypatch.setattr(session, "_docker_inspect_running", lambda name: True if name in {legacy, canonical} else None)
    monkeypatch.setattr(session, "_docker_container_created_at", lambda _name: 1.0)

    def fake_run(argv, **_kwargs):
        calls.append(argv)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    session._reconcile_docker_session_containers(canonical, "sess_test", bridge, runtime)

    assert calls == [["docker", "rm", "-f", legacy]]


def test_reconcile_docker_session_ignores_same_session_with_other_runtime(monkeypatch, tmp_path):
    session = ts.eggthreads.session
    canonical = "egg-rlm-canonical-sess-test"
    unrelated = "egg-rlm-other-db-sess-test"
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    other_runtime = tmp_path / "other-runtime"
    bridge.mkdir()
    runtime.mkdir()
    other_runtime.mkdir()

    monkeypatch.setattr(session, "_docker_session_container_names", lambda _sid: [unrelated])
    monkeypatch.setattr(
        session,
        "_docker_bind_mount_source",
        lambda _name, destination: (
            str(bridge.resolve()) if destination == "/egg-bridge" else str(other_runtime.resolve())
        ),
    )
    monkeypatch.setattr(session, "_docker_inspect_running", lambda name: True if name == unrelated else None)
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    session._reconcile_docker_session_containers(canonical, "sess_test", bridge, runtime)

    assert calls == []


def test_reconcile_docker_session_reports_duplicate_removal_failure(monkeypatch, tmp_path):
    session = ts.eggthreads.session
    canonical = "egg-rlm-canonical-sess-test"
    legacy = "egg-rlm-legacy-sess-test"
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()

    monkeypatch.setattr(session, "_docker_session_container_names", lambda _sid: [canonical, legacy])
    monkeypatch.setattr(
        session,
        "_docker_bind_mount_source",
        lambda _name, destination: str(bridge.resolve()) if destination == "/egg-bridge" else str(runtime.resolve()),
    )
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)

    class FailedRemoval:
        returncode = 1
        stdout = ""
        stderr = "container busy"

    monkeypatch.setattr(session.subprocess, "run", lambda *_args, **_kwargs: FailedRemoval())

    with pytest.raises(RuntimeError, match="container busy"):
        session._reconcile_docker_session_containers(canonical, "sess_test", bridge, runtime)


def test_reconcile_docker_session_renames_sole_legacy_container(monkeypatch, tmp_path):
    session = ts.eggthreads.session
    canonical = "egg-rlm-canonical-sess-test"
    legacy = "egg-rlm-legacy-sess-test"
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    calls = []

    monkeypatch.setattr(session, "_docker_session_container_names", lambda _sid: [legacy])
    monkeypatch.setattr(
        session,
        "_docker_bind_mount_source",
        lambda _name, destination: str(bridge.resolve()) if destination == "/egg-bridge" else str(runtime.resolve()),
    )
    monkeypatch.setattr(session, "_docker_inspect_running", lambda name: True if name == legacy else None)
    monkeypatch.setattr(session, "_docker_container_created_at", lambda _name: 1.0)

    def fake_run(argv, **_kwargs):
        calls.append(argv)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    session._reconcile_docker_session_containers(canonical, "sess_test", bridge, runtime)

    assert calls == [["docker", "rename", legacy, canonical]]


def test_start_docker_container_reconciles_even_when_target_is_already_running(monkeypatch, tmp_path):
    session = ts.eggthreads.session
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="reconcile-running")
    ts.enable_thread_session(db, thread_id, provider="docker", image="python:3.12-slim")
    cfg = ts.get_thread_session_config(db, thread_id)
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    calls = []

    monkeypatch.setattr(
        session,
        "_reconcile_docker_session_containers",
        lambda *args: calls.append(args),
    )
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)
    monkeypatch.setattr(session, "_docker_existing_mount_policy", lambda _name: session._DOCKER_MOUNT_POLICY)
    monkeypatch.setattr(
        session,
        "_docker_existing_sandbox_policy_hash",
        lambda _name: session._docker_session_policy_hash(db, thread_id, cfg),
    )
    monkeypatch.setattr(session, "_docker_existing_resource_limits", lambda _name: ({}, ""))

    restarted = session._start_docker_container(
        db,
        thread_id,
        cfg,
        "egg-rlm-canonical-sess-test",
        bridge,
        runtime,
    )

    assert restarted is False
    assert calls == [("egg-rlm-canonical-sess-test", cfg.session_id, bridge, runtime)]


def test_reconcile_docker_session_rejects_unowned_canonical_name(monkeypatch, tmp_path):
    session = ts.eggthreads.session
    bridge = tmp_path / "bridge"
    runtime = tmp_path / "runtime"
    bridge.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(session, "_docker_session_container_names", lambda _sid: [])
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)

    with pytest.raises(RuntimeError, match="does not own this Egg session"):
        session._reconcile_docker_session_containers(
            "egg-rlm-canonical-sess-test",
            "sess_test",
            bridge,
            runtime,
        )


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


def test_start_docker_container_masks_egg_without_outputs(monkeypatch, tmp_path):
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
    mounts = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]
    egg_mounts = [spec for spec in mounts if spec.endswith(":/workspace/.egg:ro")]
    assert len(egg_mounts) == 1
    assert not egg_mounts[0].startswith(str(tmp_path.resolve() / ".egg") + ":")
    assert ".egg/rlm_sessions" in egg_mounts[0]
    assert not any(".egg_outputs" in spec for spec in mounts)
    assert not any(".egg/egg_outputs" in spec for spec in mounts)
    assert "--user" in argv
    assert f"egg.db_hash={ts.docker_session_db_hash(db)}" in joined
    assert sid


def test_start_docker_container_does_not_mount_runtime_output_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.create_child_thread(db, parent, name="@runtime:python")
    ts.append_runtime_config(db, parent, runtime, language="python")
    ts.enable_thread_session(db, runtime, provider="docker", image="python:3.12-slim")
    cfg = ts.get_thread_session_config(db, runtime)
    bridge = tmp_path / "bridge"
    runtime_dir = tmp_path / "runtime"
    bridge.mkdir()
    runtime_dir.mkdir()
    calls = []
    monkeypatch.setattr(ts.eggthreads.session, "_docker_inspect_running", lambda name: None)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(ts.eggthreads.session.subprocess, "run", fake_run)

    ts.eggthreads.session._start_docker_container(db, runtime, cfg, "egg-test", bridge, runtime_dir)

    mounts = [calls[-1][i + 1] for i, arg in enumerate(calls[-1][:-1]) if arg == "-v"]
    assert not any(".egg_outputs" in spec for spec in mounts)
    assert not any(".egg/egg_outputs" in spec for spec in mounts)


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
    assert Path(saved).parent == tmp_path / ".egg" / "egg_outputs" / tid
    assert f"Artifact id: {Path(saved).name}" in preview
    assert "read_long_tool_output(" in preview
    assert ".egg_outputs" not in preview


def test_tool_output_stash_uses_flat_thread_artifact_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    from eggthreads.runner import stash_tool_output_and_build_preview

    preview, saved = stash_tool_output_and_build_preview(
        db,
        grandchild,
        "tc_test",
        "x" * 100,
        max_chars=1,
    )

    expected_dir = tmp_path / ".egg" / "egg_outputs" / grandchild
    assert saved
    assert Path(saved).parent == expected_dir
    assert f"Artifact id: {Path(saved).name}" in preview
    assert ".egg_outputs" not in preview


def test_docker_session_status_unavailable(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.enable_thread_session(db, tid, provider="docker")
    monkeypatch.setattr(ts.eggthreads.session, "docker_session_available", lambda: False)

    status = ts.get_thread_session_status(db, tid)
    assert status.status == "unhealthy"
    assert status.reason == "docker_unavailable"
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
