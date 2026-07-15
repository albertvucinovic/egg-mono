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
        "id": f"id-{name}",
        "kind_label": "rlm-session",
        "db_hash_label": db_hash if db_hash is not None else ts.docker_session_db_hash(db),
        "session_id": session_id,
        "bridge_source": bridge if bridge is not None else str((root / "bridge").resolve()),
        "runtime_source": runtime if runtime is not None else str((root / "runtime").resolve()),
        "state": _state(running=running),
        "created_at": created_at,
    }


def _old_tree(
    monkeypatch, tmp_path: Path, session_id: str, *, age: float = 7200,
    marked: bool = True, db_path: Path | None = None,
) -> Path:
    monkeypatch.chdir(tmp_path)
    root = session._bridge_root() / session_id
    (root / "bridge").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "runtime" / "sessiond.py").write_text("# owned runtime")
    (root / "masks" / "egg").mkdir(parents=True)
    (root / "activity.lock").write_text("")
    if marked:
        owner_db = ts.ThreadsDB(db_path or tmp_path / "threads.sqlite")
        session._write_session_storage_owner(owner_db, session_id)
    old = time.time() - age
    for current, dirs, files in os.walk(root, topdown=False):
        for name in files:
            os.utime(Path(current) / name, (old, old))
        for name in dirs:
            os.utime(Path(current) / name, (old, old))
    os.utime(root, (old, old))
    return root


def _safe_artifact_dependencies(monkeypatch):
    monkeypatch.setattr(session, "_docker_all_bind_mounts", lambda: ([], ""))


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
    monkeypatch.setattr(session, "_docker_existing_id", lambda name: by_name[name]["id"])
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
    assert calls == [["docker", "rm", f"id-{legacy}"]]


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
        "_docker_all_bind_mounts",
        lambda: ([{
            "name": "unknown-owner",
            "source": str((mount_root / "bridge").resolve()),
            "destination": "/egg-bridge",
        }], ""),
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


def test_storage_owner_metadata_is_atomic_and_normal_start_writes_it(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, thread_id = _db(tmp_path)
    sid = ts.enable_thread_session(db, thread_id, provider="docker")
    cfg = ts.get_thread_session_config(db, thread_id)
    monkeypatch.setattr(session, "_session_status_for_config", lambda *_a: ts.SessionStatus(
        True, "docker", sid, "missing", container_name=ts.docker_session_container_name(db, sid),
    ))
    monkeypatch.setattr(session, "docker_session_mount_dir", lambda *_a: tmp_path)
    monkeypatch.setattr(session, "_write_runtime_files", lambda _path: None)
    monkeypatch.setattr(session, "_start_docker_container", lambda *_a, **_k: False)
    session._get_or_start_docker_session_locked(db, thread_id, cfg)

    root = session._bridge_root() / sid
    metadata = json.loads((root / session._SESSION_STORAGE_METADATA).read_text())
    assert metadata == session._session_storage_owner_payload(db, sid)
    info = os.lstat(root / session._SESSION_STORAGE_METADATA)
    assert info.st_mode & 0o777 == 0o600
    initial_identity = (info.st_dev, info.st_ino)
    # Reattachment validates rather than replacing ownership metadata.
    session._write_session_storage_owner(db, sid)
    after = os.lstat(root / session._SESSION_STORAGE_METADATA)
    assert (after.st_dev, after.st_ino) == initial_identity


def test_storage_owner_scopes_cleanup_between_databases_in_shared_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db_a = ts.ThreadsDB(tmp_path / "a.sqlite"); db_a.init_schema()
    db_b = ts.ThreadsDB(tmp_path / "b.sqlite"); db_b.init_schema()
    sid = "sess_db_a"
    root = _old_tree(monkeypatch, tmp_path, sid, db_path=tmp_path / "a.sqlite")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)

    report_b = next(item for item in ts.cleanup_docker_sessions(db_b, dry_run=False) if item.get("session_id") == sid)
    assert report_b["reason"] == "ownership_mismatch"
    assert root.exists()

    report_a = next(item for item in ts.cleanup_docker_sessions(db_a, dry_run=False) if item.get("session_id") == sid)
    assert report_a["action"] == "removed"
    assert not root.exists()


def test_unmarked_legacy_tree_without_proof_is_ownership_unknown(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_legacy_unknown"
    root = _old_tree(monkeypatch, tmp_path, sid, marked=False)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)

    report = next(item for item in ts.cleanup_docker_sessions(db) if item.get("session_id") == sid)
    assert report["action"] == "skipped"
    assert report["reason"] == "ownership_unknown"
    assert root.exists()


def test_descendant_bind_mount_blocks_artifact_cleanup(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_descendant_mount"
    root = _old_tree(monkeypatch, tmp_path, sid)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    monkeypatch.setattr(session, "_docker_all_bind_mounts", lambda: ([{
        "name": "any-container",
        "source": str((root / "runtime").resolve()),
        "destination": "/runtime",
    }], ""))

    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["reason"] == "container_mount_present"
    assert report["containers"] == ["any-container"]
    assert root.exists()


def test_stopped_canonical_or_duplicate_remove_error_keeps_artifact_tree(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_associated"
    root = _old_tree(monkeypatch, tmp_path, sid)
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-associated"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    monkeypatch.setattr(session, "_docker_all_bind_mounts", lambda: ([], ""))
    monkeypatch.setattr(
        session.subprocess, "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "remove denied"),
    )

    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    artifact = next(item for item in reports if item.get("kind") == "artifact")
    assert artifact["reason"] == "associated_container_preserved"
    assert root.exists()
    duplicate = next(item for item in reports if item.get("name") == legacy)
    assert duplicate["reason"] == "docker_remove_failed"


@pytest.mark.parametrize("link_kind", ["egg", "rlm_sessions"])
def test_symlinked_authority_root_is_rejected(monkeypatch, tmp_path, link_kind):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()
    if link_kind == "egg":
        (tmp_path / ".egg").symlink_to(outside, target_is_directory=True)
        (outside / "rlm_sessions").mkdir()
    else:
        (tmp_path / ".egg").mkdir()
        (tmp_path / ".egg" / "rlm_sessions").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))

    reports = ts.cleanup_docker_sessions(db, dry_run=False)
    doctor = next(item for item in reports if item.get("kind") == "doctor")
    assert doctor["reason"] == "artifact_root_unsafe"
    assert outside.exists()


def test_apply_time_authority_root_replacement_is_rejected(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_root_swap"
    root = _old_tree(monkeypatch, tmp_path, sid)
    original_authority = session._bridge_root_authority()
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    checks = []

    def changed(_expected):
        checks.append(1)
        return False

    monkeypatch.setattr(session, "_bridge_root_authority_matches", changed)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["reason"] in {"artifact_root_changed", "artifact_path_unsafe"}
    assert checks == [1]
    assert root.exists()
    assert original_authority[0] is not None


def test_duplicate_apply_preserves_legacy_if_canonical_disappears(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_canonical_race"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-canonical-race"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    real_state = session._docker_container_state
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda name: session._DockerContainerState(False, False, "missing")
        if name == canonical else real_state(name),
    )
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("name") == legacy)
    assert report["reason"] == "canonical_state_uncertain"
    assert calls == []


def test_duplicate_apply_rejects_name_id_replacement_race(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_id_race"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-id-race"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    monkeypatch.setattr(
        session, "_docker_existing_id",
        lambda name: "replacement-id" if name == legacy else f"id-{name}",
    )
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("name") == legacy)
    assert report["reason"] == "container_identity_changed"
    assert calls == []


def test_duplicate_apply_rejects_canonical_id_replacement_race(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_canonical_id_race"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-canonical-id-race"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    monkeypatch.setattr(
        session, "_docker_existing_id",
        lambda name: "replacement-canonical" if name == canonical else f"id-{name}",
    )
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("name") == legacy)
    assert report["reason"] == "canonical_identity_changed"
    assert calls == []


def test_unmarked_legacy_tree_with_associated_container_proof_is_diagnostic_only(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_legacy_proven"
    root = _old_tree(monkeypatch, tmp_path, sid, marked=False)
    canonical = ts.docker_session_container_name(db, sid)
    inventory = [_container(db, sid, canonical, running=False)]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    monkeypatch.setattr(session, "_docker_all_bind_mounts", lambda: ([{
        "name": canonical,
        "source": str((root / "bridge").resolve()),
        "destination": "/egg-bridge",
    }], ""))

    report = next(item for item in ts.cleanup_docker_sessions(db) if item.get("kind") == "artifact")
    assert report["reason"] == "associated_container_preserved"
    assert root.exists()


def test_concurrent_owner_first_claim_has_one_stable_winner(monkeypatch, tmp_path):
    import threading

    monkeypatch.chdir(tmp_path)
    paths = [tmp_path / "claim-a.sqlite", tmp_path / "claim-b.sqlite"]
    for path in paths:
        db = ts.ThreadsDB(path); db.init_schema()
    sid = "sess_claim_race"
    barrier = threading.Barrier(2)
    outcomes = []

    def claim(path):
        db = ts.ThreadsDB(path)
        barrier.wait(timeout=2)
        try:
            session._write_session_storage_owner(db, sid)
            outcomes.append((path, "won"))
        except RuntimeError:
            outcomes.append((path, "lost"))

    threads = [threading.Thread(target=claim, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result for _db_value, result in outcomes) == ["lost", "won"]

    winner_path = next(path for path, result in outcomes if result == "won")
    winner = ts.ThreadsDB(winner_path)
    marker = session._bridge_root() / sid / session._SESSION_STORAGE_METADATA
    assert json.loads(marker.read_text()) == session._session_storage_owner_payload(winner, sid)
    identity = (os.lstat(marker).st_dev, os.lstat(marker).st_ino)
    session._write_session_storage_owner(winner, sid)
    assert (os.lstat(marker).st_dev, os.lstat(marker).st_ino) == identity


def test_owner_marker_symlink_is_rejected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_marker_symlink"
    root = session._bridge_root() / sid
    root.mkdir(parents=True)
    outside = tmp_path / "outside-owner.json"; outside.write_text("{}")
    (root / session._SESSION_STORAGE_METADATA).symlink_to(outside)

    with pytest.raises(RuntimeError, match="metadata is unreadable"):
        session._write_session_storage_owner(db, sid)
    assert outside.read_text() == "{}"


def test_structured_docker_mount_inventory_handles_escaped_paths(monkeypatch):
    names = subprocess.CompletedProcess(["docker"], 0, "one\n", "")
    source = "/tmp/a\tb\nwith|delimiters"
    destination = "/inside\tpath"
    inspect = subprocess.CompletedProcess(
        ["docker"], 0,
        json.dumps([{"Mounts": [
            {"Type": "bind", "Source": source, "Destination": destination},
            {"Type": "volume", "Source": "volume-name", "Destination": "/volume"},
        ]}]) + "\n",
        "",
    )
    responses = iter([names, inspect])
    monkeypatch.setattr(session.subprocess, "run", lambda *_a, **_k: next(responses))

    mounts, error = session._docker_all_bind_mounts()

    assert error == ""
    assert mounts == [{
        "name": "one",
        "source": str(Path(source).resolve()),
        "destination": destination,
    }]


@pytest.mark.parametrize("payload", ["not-json", "{}", '[{"Mounts": {}}]', '[{"Mounts": [null]}]'])
def test_structured_docker_mount_inventory_malformed_fails_closed(monkeypatch, payload):
    responses = iter([
        subprocess.CompletedProcess(["docker"], 0, "one\n", ""),
        subprocess.CompletedProcess(["docker"], 0, payload, ""),
    ])
    monkeypatch.setattr(session.subprocess, "run", lambda *_a, **_k: next(responses))

    mounts, error = session._docker_all_bind_mounts()

    assert mounts is None
    assert error


def test_docker_rm_uses_captured_id_if_name_is_reassigned_before_command(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_rm_id"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-rm-id"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    original_id = inventory[1]["id"]
    calls = []

    def fake_run(argv, **_kwargs):
        # The mutable name now resolves to a replacement only after authority
        # checked the captured old ID. Removal must still target old ID.
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(session.subprocess, "run", fake_run)

    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("name") == legacy)
    assert report["action"] == "removed"
    assert calls == [["docker", "rm", original_id]]


def _replace_path_with_directory(path: Path, replacement: Path) -> Path:
    moved = path.with_name(path.name + "-original")
    path.rename(moved)
    replacement.rename(path)
    return moved


def test_same_path_authority_root_replacement_preserves_both_trees(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_root_inode_swap"
    root = _old_tree(monkeypatch, tmp_path, sid)
    authority_root = session._bridge_root()
    replacement_parent = tmp_path / "replacement-root"
    replacement_target = replacement_parent / "external.txt"
    replacement_parent.mkdir(); replacement_target.write_text("keep")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    real_match = session._bridge_root_authority_matches
    swapped = []

    def swap_then_match(expected):
        if not swapped:
            swapped.append(_replace_path_with_directory(authority_root, replacement_parent))
        return real_match(expected)

    monkeypatch.setattr(session, "_bridge_root_authority_matches", swap_then_match)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["reason"] in {"artifact_root_changed", "artifact_path_unsafe"}
    assert (authority_root / "external.txt").read_text() == "keep"
    assert (swapped[0] / sid).exists()
    assert root != authority_root / sid or (swapped[0] / sid).exists()


def test_same_path_candidate_replacement_preserves_replacement_and_original(
    monkeypatch, tmp_path,
):
    db, _thread_id = _db(tmp_path)
    sid = "sess_candidate_inode_swap"
    root = _old_tree(monkeypatch, tmp_path, sid)
    replacement = tmp_path / "candidate-replacement"
    replacement.mkdir(); (replacement / "external.txt").write_text("keep")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    real_pin = session._pin_session_candidate
    pins = []
    moved = []

    def pin_and_swap(authority, value):
        result = real_pin(authority, value)
        pins.append(result)
        if len(pins) == 2:
            moved.append(_replace_path_with_directory(root, replacement))
        return result

    monkeypatch.setattr(session, "_pin_session_candidate", pin_and_swap)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["reason"] in {"artifact_root_changed", "artifact_path_unsafe"}
    assert (root / "external.txt").read_text() == "keep"
    assert moved[0].exists()


def test_late_candidate_symlink_swap_preserves_external_tree(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_late_symlink"
    root = _old_tree(monkeypatch, tmp_path, sid)
    outside = tmp_path / "outside-late"; outside.mkdir()
    external = outside / "external.txt"; external.write_text("keep")
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    real_pin = session._pin_session_candidate
    moved = []
    calls = []

    def pin_and_symlink(authority, value):
        calls.append(1)
        result = real_pin(authority, value)
        if len(calls) == 2:
            moved_path = root.with_name(root.name + "-original")
            root.rename(moved_path)
            root.symlink_to(outside, target_is_directory=True)
            moved.append(moved_path)
        return result

    monkeypatch.setattr(session, "_pin_session_candidate", pin_and_symlink)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["reason"] in {"artifact_root_changed", "artifact_path_unsafe"}
    assert external.read_text() == "keep"
    assert moved[0].exists()


def test_duplicate_missing_id_at_apply_is_idempotent_disappearance(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    db, _thread_id = _db(tmp_path)
    sid = "sess_disappeared"
    canonical = ts.docker_session_container_name(db, sid)
    legacy = "legacy-disappeared"
    inventory = [
        _container(db, sid, canonical, running=False),
        _container(db, sid, legacy, running=False),
    ]
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: (inventory, ""))
    _revalidate_from_inventory(monkeypatch, inventory)
    monkeypatch.setattr(
        session, "_docker_existing_id",
        lambda name: None if name == legacy else f"id-{name}",
    )
    original_state = session._docker_container_state
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda name: session._DockerContainerState(False, False, "missing")
        if name == legacy else original_state(name),
    )
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("name") == legacy)
    assert report["action"] == "removed"
    assert report["reason"] == "already_removed"
    assert calls == []


def test_claim_waits_until_quarantine_cleanup_finishes_and_uses_fresh_tree(
    monkeypatch, tmp_path,
):
    import threading

    monkeypatch.chdir(tmp_path)
    db_a_path = tmp_path / "lock-a.sqlite"
    db_a = ts.ThreadsDB(db_a_path); db_a.init_schema()
    db_b_path = tmp_path / "lock-b.sqlite"
    db_b = ts.ThreadsDB(db_b_path); db_b.init_schema()
    sid = "sess_storage_lock"
    root = _old_tree(monkeypatch, tmp_path, sid, db_path=tmp_path / "lock-a.sqlite")
    old_file = root / "runtime" / "old.txt"; old_file.write_text("old")
    old = time.time() - 7200
    for path in [old_file, root / "runtime", root]:
        os.utime(path, (old, old))
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    quarantine_ready = threading.Event()
    allow_remove = threading.Event()
    real_remove = session._remove_quarantined_session_tree

    def paused_remove(authority, quarantine):
        quarantine_ready.set()
        assert allow_remove.wait(3)
        real_remove(authority, quarantine)

    monkeypatch.setattr(session, "_remove_quarantined_session_tree", paused_remove)
    cleanup_result = []
    def run_cleanup():
        cleanup_result.extend(ts.cleanup_docker_sessions(
            ts.ThreadsDB(db_a_path), dry_run=False,
        ))
    cleanup = threading.Thread(
        target=run_cleanup,
    )
    cleanup.start()
    assert quarantine_ready.wait(3)
    assert not root.exists()

    claim_result = []
    claim = threading.Thread(
        target=lambda: (
            session._write_session_storage_owner(ts.ThreadsDB(db_b_path), sid),
            claim_result.append("done"),
        ),
    )
    claim.start()
    time.sleep(0.05)
    assert claim.is_alive()
    assert not root.exists()

    allow_remove.set()
    cleanup.join(3); claim.join(3)
    assert not cleanup.is_alive() and not claim.is_alive()
    assert claim_result == ["done"]
    assert root.is_dir()
    assert not (root / "runtime" / "old.txt").exists()
    assert json.loads((root / session._SESSION_STORAGE_METADATA).read_text()) == session._session_storage_owner_payload(
        ts.ThreadsDB(db_b_path), sid,
    )
    assert any(item.get("action") == "removed" for item in cleanup_result)


def test_replacement_created_after_quarantine_is_never_deleted(monkeypatch, tmp_path):
    db, _thread_id = _db(tmp_path)
    sid = "sess_post_quarantine_replacement"
    root = _old_tree(monkeypatch, tmp_path, sid)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    real_remove = session._remove_quarantined_session_tree
    replacement = []

    def replace_then_remove(authority, quarantine):
        root.mkdir(parents=True)
        marker = root / "replacement.txt"; marker.write_text("keep")
        replacement.append(marker)
        real_remove(authority, quarantine)

    monkeypatch.setattr(session, "_remove_quarantined_session_tree", replace_then_remove)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["action"] == "removed"
    assert replacement[0].read_text() == "keep"


def test_quarantine_identity_mismatch_restores_original_and_deletes_nothing(
    monkeypatch, tmp_path,
):
    db, _thread_id = _db(tmp_path)
    sid = "sess_quarantine_mismatch"
    root = _old_tree(monkeypatch, tmp_path, sid)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    # More deterministic: wrap rename helper by replacing os.stat only for the
    # generated quarantine name after rename.
    real_stat = session.os.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".egg-quarantine-"):
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(session.os, "stat", fake_stat)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["action"] == "error"
    assert "quarantine identity mismatch" in report["error"]
    assert root.exists()
    assert (root / session._SESSION_STORAGE_METADATA).exists()
    assert not list(session._bridge_root().glob(f".egg-quarantine-{sid}-*"))


def test_quarantine_delete_failure_leaves_recoverable_quarantine_not_replacement(
    monkeypatch, tmp_path,
):
    db, _thread_id = _db(tmp_path)
    sid = "sess_quarantine_failure"
    root = _old_tree(monkeypatch, tmp_path, sid)
    monkeypatch.setattr(session, "_docker_owned_session_inventory", lambda: ([], ""))
    _safe_artifact_dependencies(monkeypatch)
    quarantines = []

    def fail_remove(_authority, quarantine):
        quarantines.append(quarantine.lexical)
        raise RuntimeError("injected quarantine deletion failure")

    monkeypatch.setattr(session, "_remove_quarantined_session_tree", fail_remove)
    report = next(item for item in ts.cleanup_docker_sessions(db, dry_run=False) if item.get("session_id") == sid)
    assert report["action"] == "error"
    assert not root.exists()
    assert quarantines and quarantines[0].is_dir()
    assert (quarantines[0] / session._SESSION_STORAGE_METADATA).exists()
