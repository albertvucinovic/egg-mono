from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import eggthreads as ts
import eggthreads.session as session
from eggthreads.command_catalog import CommandContext, create_default_command_registry


def _db(tmp_path: Path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="root")
    return db, thread_id


def _state(*, running: bool | None, exists: bool | None = True):
    return session._DockerContainerState(exists, running, "running" if running else "exited")


def _container(
    db,
    session_id: str,
    name: str,
    *,
    running: bool | None = False,
    db_hash: str | None = None,
    bridge: str | None = None,
    runtime: str | None = None,
    created_at: float = 1.0,
):
    root = session._bridge_root() / session_id
    return {
        "name": name,
        "kind_label": "rlm-session",
        "db_hash_label": db_hash if db_hash is not None else ts.docker_session_db_hash(db),
        "session_id": session_id,
        "bridge_source": bridge if bridge is not None else str((root / "bridge").resolve()),
        "runtime_source": runtime if runtime is not None else str((root / "runtime").resolve()),
        "state": _state(running=running),
        "created_at": created_at,
    }


def _old_tree(monkeypatch, tmp_path: Path, session_id: str, *, age: float = 7200) -> Path:
    monkeypatch.chdir(tmp_path)
    root = session._bridge_root() / session_id
    (root / "bridge").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "runtime" / "sessiond.py").write_text("# owned runtime")
    (root / "masks" / "egg").mkdir(parents=True)
    (root / "activity.lock").write_text("")
    old = time.time() - age
    for current, dirs, files in os.walk(root, topdown=False):
        for name in files:
            os.utime(Path(current) / name, (old, old))
        for name in dirs:
            os.utime(Path(current) / name, (old, old))
    os.utime(root, (old, old))
    return root


def _safe_artifact_dependencies(monkeypatch):
    monkeypatch.setattr(session, "_docker_container_names_using_path", lambda _path: ([], ""))


def _revalidate_from_inventory(monkeypatch, inventory):
    by_name = {item["name"]: item for item in inventory}
    monkeypatch.setattr(
        session, "_docker_existing_label",
        lambda name, label: by_name[name].get({
            "egg.kind": "kind_label",
            "egg.db_hash": "db_hash_label",
            "egg.session_id": "session_id",
        }[label]),
    )
    monkeypatch.setattr(
        session, "_docker_bind_mount_source",
        lambda name, destination: by_name[name][
            "bridge_source" if destination == "/egg-bridge" else "runtime_source"
        ],
    )
    monkeypatch.setattr(session, "_docker_container_state", lambda name: by_name[name]["state"])
    monkeypatch.setattr(session, "_docker_container_created_at", lambda name: by_name[name]["created_at"])


def test_cleanup_is_dry_run_by_default_and_apply_is_bounded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_stale"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "egg-rlm-legacy-sess-stale"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    calls = []
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    dry = ts.cleanup_docker_sessions(db)
    assert {item["name"]: (item["action"], item["reason"]) for item in dry} == {
        canonical: ("skipped", "canonical_preserved"),
        legacy: ("would_remove", "stopped_legacy_duplicate"),
    }
    assert calls == []

    applied = ts.cleanup_docker_sessions(db, dry_run=False)
    assert {item["name"]: item["action"] for item in applied} == {
        canonical: "skipped", legacy: "removed",
    }
    assert calls == [["docker", "rm", legacy]]


def test_container_cleanup_protects_running_uncertain_unrelated_and_unlabeled(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_owned"
    canonical = ts.docker_session_container_name(db, sid)
    inventory = [
        _container(db, sid, canonical, running=True),
        _container(db, sid, "legacy-running", running=True),
        _container(db, sid, "legacy-uncertain", running=None),
        _container(db, sid, "legacy-bad-mount", runtime=str(tmp_path / "other")),
        _container(db, "sess_foreign", "foreign", db_hash="other-db"),
        {**_container(db, sid, "unlabeled"), "kind_label": None},
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    by_name = {item["name"]: item for item in reports}
    assert by_name[canonical]["reason"] == "canonical_preserved"
    assert by_name["legacy-running"]["reason"] == "running_container"
    assert by_name["legacy-uncertain"]["reason"] == "container_state_uncertain"
    assert by_name["legacy-bad-mount"]["reason"] == "database_or_mount_identity_unverified"
    assert by_name["foreign"]["reason"] == "database_or_mount_identity_unverified"
    assert by_name["unlabeled"]["reason"] == "ownership_label_invalid"
    assert calls == []


def test_legacy_db_label_is_accepted_only_for_current_reference_and_exact_mounts(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    db, thread_id = _db(tmp_path)
    sid = ts.enable_thread_session(db, thread_id, provider="docker")
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-path-hash"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False, db_hash="old-path-hash"),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))

    reports = ts.cleanup_docker_sessions(db)
    assert {item["name"]: item["action"] for item in reports} == {
        canonical: "skipped", legacy: "would_remove",
    }


def test_sole_legacy_container_is_preserved_when_canonical_is_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_sole_legacy"
    legacy = "legacy-only"
    inventory = [_container(db, sid, legacy, running=False)]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    report = ts.cleanup_docker_sessions(db, dry_run=False)[0]
    assert report["action"] == "skipped"
    assert report["reason"] == "canonical_missing_or_uncertain"
    assert calls == []


def test_docker_remove_failure_is_reported_and_apply_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_stale"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-failure"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "permission denied"),
    )
    failed = ts.cleanup_docker_sessions(db, dry_run=False)
    assert next(item for item in failed if item["name"] == legacy) == {
        "kind": "container", "name": legacy, "session_id": sid,
        "action": "error", "reason": "docker_remove_failed", "error": "permission denied",
    }

    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([inventory[0]], ""))
    repeated = ts.cleanup_docker_sessions(db, dry_run=False)
    assert all(item["name"] != legacy for item in repeated)


def test_container_apply_revalidates_identity_state_reference_and_activity_lock(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_revalidate"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-revalidate"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    monkeypatch.setattr(
        session, "_docker_duplicate_cleanup_authority",
        lambda *_args: (False, "running_container", None),
    )
    report = next(
        item for item in ts.cleanup_docker_sessions(db, dry_run=False)
        if item["name"] == legacy
    )
    assert report["reason"] == "running_container"
    assert calls == []

    @contextmanager
    def busy_guard(*_args, **_kwargs):
        yield False

    monkeypatch.setattr(session, "_session_activity_guard", busy_guard)
    report = next(
        item for item in ts.cleanup_docker_sessions(db, dry_run=False)
        if item["name"] == legacy
    )
    assert report["reason"] == "host_activity"
    assert calls == []


def test_stale_artifacts_dry_run_apply_and_idempotence(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_stale_artifacts"
    root = _old_tree(monkeypatch, tmp_path, sid)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)

    dry = ts.cleanup_docker_sessions(db)
    report = next(item for item in dry if item.get("session_id") == sid)
    assert report["action"] == "would_remove"
    assert root.exists()

    applied = ts.cleanup_docker_sessions(db, dry_run=False)
    report = next(item for item in applied if item.get("session_id") == sid)
    assert report["action"] == "removed"
    assert not root.exists()
    assert ts.cleanup_docker_sessions(db, dry_run=False) == []


def test_artifacts_protect_reference_recent_heartbeat_and_active_control(monkeypatch, tmp_path):
    db, thread_id = _db(tmp_path)
    referenced_sid = ts.enable_thread_session(db, thread_id, provider="docker")
    referenced = _old_tree(monkeypatch, tmp_path, referenced_sid)
    recent_sid = "sess_recent"
    recent = _old_tree(monkeypatch, tmp_path, recent_sid, age=5)
    heartbeat_sid = "sess_heartbeat"
    heartbeat = _old_tree(monkeypatch, tmp_path, heartbeat_sid)
    now = time.time()
    (heartbeat / "bridge" / "sessiond_status.json").write_text(json.dumps({"heartbeat_at": now}))
    os.utime(heartbeat / "bridge" / "sessiond_status.json", (now - 7200, now - 7200))
    os.utime(heartbeat / "bridge", (now - 7200, now - 7200))
    os.utime(heartbeat, (now - 7200, now - 7200))
    active_sid = "sess_active"
    active = _old_tree(monkeypatch, tmp_path, active_sid)
    (active / "bridge" / "eval_live.req.json.processing").write_text("{}")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)

    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    by_sid = {item.get("session_id"): item for item in reports}
    assert by_sid[referenced_sid]["reason"] == "session_referenced"
    assert by_sid[recent_sid]["reason"] == "artifact_too_recent"
    assert by_sid[heartbeat_sid]["reason"] == "heartbeat_protected"
    assert by_sid[active_sid]["reason"] == "active_eval_protected"
    assert referenced.exists() and recent.exists() and heartbeat.exists() and active.exists()


def test_artifact_symlink_unexpected_entry_mount_owner_and_activity_lock_fail_closed(
    monkeypatch, tmp_path,
):
    db, _thread_id = _db(tmp_path)
    symlink_root = _old_tree(monkeypatch, tmp_path, "sess_symlink")
    outside = tmp_path / "outside"; outside.write_text("keep")
    (symlink_root / "bridge" / "escape").symlink_to(outside)
    unexpected_root = _old_tree(monkeypatch, tmp_path, "sess_unexpected")
    (unexpected_root / "project-output.txt").write_text("keep")
    mount_root = _old_tree(monkeypatch, tmp_path, "sess_mounted")
    locked_root = _old_tree(monkeypatch, tmp_path, "sess_locked")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    monkeypatch.setattr(
        session,
        "_docker_container_names_using_path",
        lambda path: (["unknown-owner"], "") if path == mount_root else ([], ""),
    )
    real_guard = session._session_activity_guard

    @contextmanager
    def guard(db_value, sid, *, blocking=True):
        if sid == "sess_locked":
            yield False
        else:
            with real_guard(db_value, sid, blocking=blocking) as acquired:
                yield acquired

    monkeypatch.setattr(session, "_session_activity_guard", guard)

    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    by_sid = {item.get("session_id"): item for item in reports}
    assert by_sid["sess_symlink"]["reason"] == "artifact_path_unsafe"
    assert by_sid["sess_unexpected"]["reason"] == "artifact_path_unsafe"
    assert by_sid["sess_mounted"]["reason"] == "container_mount_present"
    assert by_sid["sess_locked"]["reason"] == "host_activity"
    assert outside.read_text() == "keep"
    assert all(root.exists() for root in (symlink_root, unexpected_root, mount_root, locked_root))


def test_artifact_apply_revalidates_recent_activity_before_delete(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_revalidate_artifacts"
    root = _old_tree(monkeypatch, tmp_path, sid)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    calls = []
    real_protection = session._session_artifact_protection_error

    def changing_protection(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return real_protection(*args, **kwargs)
        return "active_eval_protected", "request arrived during cleanup"

    monkeypatch.setattr(session, "_session_artifact_protection_error", changing_protection)

    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    report = next(item for item in reports if item.get("session_id") == sid)
    assert report["action"] == "skipped"
    assert report["reason"] == "active_eval_protected"
    assert root.exists()


def test_docker_discovery_or_reference_ambiguity_blocks_artifact_cleanup(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    root = _old_tree(monkeypatch, tmp_path, "sess_ambiguous")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], "daemon unavailable"))
    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    artifact = next(item for item in reports if item.get("kind") == "artifact")
    assert artifact["reason"] == "docker_state_uncertain"
    assert root.exists()

    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    monkeypatch.setattr(session, "_current_session_reference_map", lambda _db: (None, "bad event"))
    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    artifact = next(item for item in reports if item.get("kind") == "artifact")
    assert artifact["reason"] == "reference_state_uncertain"
    assert root.exists()


def test_shared_session_cleanup_command_is_dry_run_unless_apply(monkeypatch, tmp_path):
    db, thread_id = _db(tmp_path)
    registry = create_default_command_registry()
    calls = []
    reports = [{
        "kind": "container", "name": "legacy", "action": "would_remove",
        "reason": "stopped_legacy_duplicate",
    }]

    def fake_cleanup(_db, **kwargs):
        calls.append(kwargs)
        return reports

    monkeypatch.setattr(ts, "cleanup_thread_sessions", fake_cleanup)
    logs = []
    blocks = []
    context = CommandContext(
        db=db,
        current_thread=thread_id,
        log_system=logs.append,
        console_print_block=lambda title, text, **_kwargs: blocks.append((title, text)),
    )

    registry.execute("sessionCleanup", context, "")
    registry.execute("sessionCleanup", context, "apply older_than=2h")

    assert calls[0]["dry_run"] is True
    assert calls[1]["dry_run"] is False
    assert calls[1]["older_than_sec"] == 7200
    assert blocks[0][0] == "Session Cleanup"
    assert "would_remove" in blocks[0][1]
    assert any("mode=dry-run" in value for value in logs)
    assert any("mode=apply" in value for value in logs)

    invalid = registry.execute("sessionCleanup", context, "apply older_than=off")
    assert invalid.clear_input is False
    assert any("positive duration" in value for value in logs)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "1h"])
def test_programmatic_cleanup_rejects_invalid_duration(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    with pytest.raises(ValueError, match="positive finite"):
        ts.cleanup_docker_sessions(db, older_than_sec=value)
