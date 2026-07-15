from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import eggthreads as ts
import eggthreads.session as session
from eggthreads.builtin_plugins.session import format_session_status


def _configured(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="root")
    ts.enable_thread_session(db, thread_id, provider="docker")
    cfg = ts.get_thread_session_config(db, thread_id)
    return db, thread_id, cfg


def _capture_run(monkeypatch):
    calls = []
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: None)
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: None)
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "id", ""),
    )
    return calls


@pytest.mark.parametrize("value", [None, "", "off", "OFF", "none", "unlimited"])
def test_limits_disabled_preserve_legacy_hash_and_command(monkeypatch, tmp_path, value):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.delenv("EGG_RLM_SESSION_MEMORY", raising=False)
    monkeypatch.delenv("EGG_RLM_SESSION_PIDS_LIMIT", raising=False)
    legacy_hash = session._docker_session_policy_hash(db, thread_id, cfg)
    if value is not None:
        monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", value)
        monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", value)

    assert session._docker_session_policy_hash(db, thread_id, cfg) == legacy_hash
    calls = _capture_run(monkeypatch)
    bridge = session._session_bridge_dir(cfg.session_id)
    runtime = session._session_runtime_dir(cfg.session_id)
    session._start_docker_container(db, thread_id, cfg, "new", bridge, runtime)
    command = calls[-1]
    assert "--memory" not in command
    assert "--pids-limit" not in command
    assert not any(str(value).startswith("egg.session_memory_bytes=") for value in command)
    assert not any(str(value).startswith("egg.session_pids_limit=") for value in command)


def test_unset_limits_do_not_restart_existing_container(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.delenv("EGG_RLM_SESSION_MEMORY", raising=False)
    monkeypatch.delenv("EGG_RLM_SESSION_PIDS_LIMIT", raising=False)
    expected = session._docker_session_policy_hash(db, thread_id, cfg)
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: None)
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)
    monkeypatch.setattr(session, "_docker_existing_mount_policy", lambda _name: session._DOCKER_MOUNT_POLICY)
    monkeypatch.setattr(session, "_docker_existing_sandbox_policy_hash", lambda _name: expected)
    monkeypatch.setattr(session, "_docker_existing_channel_reaper_version", lambda _name: None)
    monkeypatch.setattr(session, "_docker_existing_resource_limits", lambda _name: ({}, ""))
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    assert session._start_docker_container(
        db, thread_id, cfg, "existing", session._session_bridge_dir(cfg.session_id),
        session._session_runtime_dir(cfg.session_id),
    ) is False
    assert calls == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("33554432", 32 * 1024 * 1024),
        ("32m", 32 * 1024 * 1024),
        ("32MiB", 32 * 1024 * 1024),
        ("1g", 1024 ** 3),
        (str(session._DOCKER_MEMORY_MAX_BYTES), session._DOCKER_MEMORY_MAX_BYTES),
    ],
)
def test_memory_limit_normalizes(monkeypatch, raw, expected):
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", raw)
    limits = session._configured_docker_session_limits()
    assert limits.memory_bytes == expected
    assert limits.memory_swap_bytes == expected


@pytest.mark.parametrize(
    "raw",
    [
        True, "31m", "33554433", "0", "-1", "+32m", "32.5m", "32x",
        str(session._DOCKER_MEMORY_MAX_BYTES + 1),
        str(session._DOCKER_MEMORY_MAX_BYTES + session._DOCKER_MEMORY_ALIGNMENT_BYTES),
    ],
)
def test_memory_limit_invalid_or_boundary(monkeypatch, raw):
    if isinstance(raw, str):
        monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", raw)
        call = session._configured_docker_session_limits
    else:
        call = lambda: session._configured_docker_memory_bytes(raw)
    with pytest.raises(ValueError, match="EGG_RLM_SESSION_MEMORY"):
        call()


@pytest.mark.parametrize(("raw", "expected"), [("4", 4), ("00042", 42), ("4194304", 4194304)])
def test_pids_limit_normalizes(monkeypatch, raw, expected):
    monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", raw)
    assert session._configured_docker_session_limits().pids_limit == expected


@pytest.mark.parametrize("raw", [True, "0", "1", "2", "3", "-1", "+4", "4.0", "4194305", "NaN"])
def test_pids_limit_invalid_or_boundary(monkeypatch, raw):
    if isinstance(raw, str):
        monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", raw)
        call = session._configured_docker_session_limits
    else:
        call = lambda: session._configured_docker_pids_limit(raw)
    with pytest.raises(ValueError, match="EGG_RLM_SESSION_PIDS_LIMIT"):
        call()


def test_valid_limits_change_hash_and_add_docker_args_and_labels(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.delenv("EGG_RLM_SESSION_MEMORY", raising=False)
    monkeypatch.delenv("EGG_RLM_SESSION_PIDS_LIMIT", raising=False)
    legacy_hash = session._docker_session_policy_hash(db, thread_id, cfg)
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "512m")
    monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", "256")
    limited_hash = session._docker_session_policy_hash(db, thread_id, cfg)
    assert limited_hash != legacy_hash

    calls = _capture_run(monkeypatch)
    session._start_docker_container(
        db, thread_id, cfg, "limited", session._session_bridge_dir(cfg.session_id),
        session._session_runtime_dir(cfg.session_id),
    )
    command = calls[-1]
    memory_bytes = 512 * 1024 * 1024
    assert command[command.index("--memory") + 1] == f"{memory_bytes}b"
    assert command[command.index("--memory-swap") + 1] == f"{memory_bytes}b"
    assert command[command.index("--pids-limit") + 1] == "256"
    assert f"egg.session_memory_bytes={memory_bytes}" in command
    assert f"egg.session_memory_swap_bytes={memory_bytes}" in command
    assert "egg.session_pids_limit=256" in command
    assert f"egg.sandbox_policy_hash={limited_hash}" in command


def test_swap_intent_changes_resource_policy_hash(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    memory_bytes = 64 * 1024 * 1024

    no_swap_hash = session._docker_session_policy_hash(
        db, thread_id, cfg,
        session._DockerSessionLimits(
            memory_bytes=memory_bytes,
            memory_swap_bytes=memory_bytes,
        ),
    )
    implicit_swap_hash = session._docker_session_policy_hash(
        db, thread_id, cfg,
        session._DockerSessionLimits(memory_bytes=memory_bytes),
    )

    assert no_swap_hash != implicit_swap_hash


def test_invalid_limit_fails_before_reconcile_or_docker_mutation(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "bad")
    calls = []
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: calls.append("reconcile"))
    monkeypatch.setattr(session.subprocess, "run", lambda *_a, **_k: calls.append("docker"))

    with pytest.raises(ValueError, match="EGG_RLM_SESSION_MEMORY"):
        session._start_docker_container(
            db, thread_id, cfg, "existing", session._session_bridge_dir(cfg.session_id),
            session._session_runtime_dir(cfg.session_id),
        )
    assert calls == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("EGG_RLM_SESSION_MEMORY", "33554433"),
        ("EGG_RLM_SESSION_MEMORY", str(session._DOCKER_MEMORY_MAX_BYTES + 1)),
        ("EGG_RLM_SESSION_PIDS_LIMIT", "3"),
    ],
)
def test_boundary_invalid_limit_never_removes_existing_container(
    monkeypatch, tmp_path, variable, value,
):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, value)
    calls = []
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: calls.append("reconcile"))
    monkeypatch.setattr(session.subprocess, "run", lambda *_a, **_k: calls.append("docker"))

    with pytest.raises(ValueError, match=variable):
        session._start_docker_container(
            db, thread_id, cfg, "existing", session._session_bridge_dir(cfg.session_id),
            session._session_runtime_dir(cfg.session_id),
        )

    assert calls == []


def test_limit_policy_change_recreates_but_unchanged_preserves(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "256m")
    monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", "64")
    expected = session._docker_session_policy_hash(db, thread_id, cfg)
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: None)
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)
    monkeypatch.setattr(session, "_docker_existing_mount_policy", lambda _name: session._DOCKER_MOUNT_POLICY)
    current_hash = [expected]
    monkeypatch.setattr(session, "_docker_existing_sandbox_policy_hash", lambda _name: current_hash[0])
    monkeypatch.setattr(session, "_docker_existing_channel_reaper_version", lambda _name: None)
    monkeypatch.setattr(
        session, "_docker_existing_resource_limits",
        lambda _name: ({"memory_bytes": 256 * 1024 * 1024, "memory_swap_bytes": 256 * 1024 * 1024, "pids_limit": 64}, ""),
    )
    calls = []
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "id", ""),
    )
    bridge = session._session_bridge_dir(cfg.session_id)
    runtime = session._session_runtime_dir(cfg.session_id)

    assert session._start_docker_container(db, thread_id, cfg, "existing", bridge, runtime) is False
    assert calls == []

    current_hash[0] = "old-unlimited-policy"
    assert session._start_docker_container(db, thread_id, cfg, "existing", bridge, runtime) is True
    assert calls[0] == ["docker", "rm", "-f", "existing"]
    assert calls[1][:3] == ["docker", "run", "-d"]
    assert not any(call[:2] == ["docker", "restart"] for call in calls)


def test_effective_limit_mismatch_recreates_even_when_policy_label_matches(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "256m")
    expected = session._docker_session_policy_hash(db, thread_id, cfg)
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: None)
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)
    monkeypatch.setattr(session, "_docker_existing_mount_policy", lambda _name: session._DOCKER_MOUNT_POLICY)
    monkeypatch.setattr(session, "_docker_existing_sandbox_policy_hash", lambda _name: expected)
    monkeypatch.setattr(
        session, "_docker_existing_resource_limits",
        lambda _name: ({"memory_bytes": 256 * 1024 * 1024, "memory_swap_bytes": 256 * 1024 * 1024}, "HostConfig.Memory mismatch"),
    )
    calls = []
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "id", ""),
    )

    assert session._start_docker_container(
        db, thread_id, cfg, "existing", session._session_bridge_dir(cfg.session_id),
        session._session_runtime_dir(cfg.session_id),
    ) is True
    assert calls[0] == ["docker", "rm", "-f", "existing"]
    assert calls[1][:3] == ["docker", "run", "-d"]


def test_uncertain_effective_limit_inspection_does_not_delete_container(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", "64")
    monkeypatch.setattr(session, "_reconcile_docker_session_containers", lambda *_a: None)
    monkeypatch.setattr(session, "_docker_inspect_running", lambda _name: True)
    monkeypatch.setattr(session, "_docker_existing_mount_policy", lambda _name: session._DOCKER_MOUNT_POLICY)
    monkeypatch.setattr(
        session, "_docker_existing_sandbox_policy_hash",
        lambda _name: session._docker_session_policy_hash(db, thread_id, cfg),
    )
    monkeypatch.setattr(
        session, "_docker_existing_resource_limits",
        lambda _name: (None, "Docker inspect was unavailable"),
    )
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **_kwargs: calls.append(argv))

    with pytest.raises(RuntimeError, match="Docker inspect was unavailable"):
        session._start_docker_container(
            db, thread_id, cfg, "existing", session._session_bridge_dir(cfg.session_id),
            session._session_runtime_dir(cfg.session_id),
        )
    assert calls == []


def test_policy_recreation_waits_for_heartbeat_and_next_eval_refreshes(
    monkeypatch, tmp_path,
):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    runtime_dir = session._session_runtime_dir(cfg.session_id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_hash = "runtime-hash"
    container_name = session.docker_session_container_name(db, cfg.session_id)
    configured_limits = {
        "memory_bytes": 64 * 1024 * 1024,
        "memory_swap_bytes": 64 * 1024 * 1024,
    }
    stale_limits = {
        "memory_bytes": 32 * 1024 * 1024,
        "memory_swap_bytes": 32 * 1024 * 1024,
    }
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "64m")
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    monkeypatch.setattr(
        session, "_docker_existing_resource_limits", lambda _name: (stale_limits, ""),
    )
    monkeypatch.setattr(session, "_write_session_storage_owner", lambda *_a: None)
    monkeypatch.setattr(session, "_write_runtime_files", lambda _path: None)
    monkeypatch.setattr(session, "_start_docker_container", lambda *_a, **_k: True)
    daemon = {
        "daemon_generation": "generation-after-recreate",
        "heartbeat_at": 2.0,
        "last_activity_at": 1.0,
        "active_requests": [],
        "channel_state": {},
    }
    waits = []
    monkeypatch.setattr(
        session, "_wait_for_docker_daemon",
        lambda bridge: waits.append(bridge) or (daemon, ""),
    )

    recreated = session._get_or_start_docker_session_locked(db, thread_id, cfg)

    assert recreated.status == "ready"
    assert recreated.container_name == container_name
    assert recreated.resource_limits == configured_limits
    assert waits == [session._session_bridge_dir(cfg.session_id)]

    monkeypatch.setattr(
        session, "_get_or_start_docker_session_locked", lambda *_a: recreated,
    )
    monkeypatch.setattr(session, "_session_runtime_dir", lambda _sid: runtime_dir)
    monkeypatch.setattr(session, "_python_repl_runtime_code_hash", lambda _path: runtime_hash)
    calls = []
    monkeypatch.setattr(
        session,
        "_run_docker_python_eval_request",
        lambda _db, _thread, _bridge, payload, _timeout, _cancel=None:
            calls.append(payload) or session._PYTHON_REFRESH_SUCCESS_OUTPUT,
    )

    result = session._execute_python_docker_captured(
        db, thread_id, cfg, "print('next-eval-ok')",
        repl_name="default", eval_token="token", timeout_sec=5,
    )

    assert result == session._PYTHON_REFRESH_SUCCESS_OUTPUT
    assert len(calls) == 2
    assert "repl_refresh.py" in calls[0]["code"]
    assert calls[1]["code"] == "print('next-eval-ok')"


def test_memory_provider_is_unaffected_by_docker_limit_environment(monkeypatch, tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="root")
    ts.enable_thread_session(db, thread_id, provider="memory")
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "invalid-for-docker")
    monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", "invalid-for-docker")

    status = ts.get_thread_session_status(db, thread_id)
    assert status.provider == "memory"
    assert status.status == "available"
    assert status.resource_limits == {}


def test_status_validates_and_displays_effective_limits(monkeypatch, tmp_path):
    db, thread_id, cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.setenv("EGG_RLM_SESSION_MEMORY", "64m")
    monkeypatch.setenv("EGG_RLM_SESSION_PIDS_LIMIT", "32")
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    monkeypatch.setattr(session, "_docker_container_state", lambda _name: session._DockerContainerState(True, False, "exited"))
    monkeypatch.setattr(
        session, "_docker_existing_resource_limits",
        lambda _name: ({"memory_bytes": 64 * 1024 * 1024, "memory_swap_bytes": 64 * 1024 * 1024, "pids_limit": 32}, ""),
    )
    status = ts.get_thread_session_status(db, thread_id)
    assert status.status == "stopped"
    assert status.resource_limits == {"memory_bytes": 64 * 1024 * 1024, "memory_swap_bytes": 64 * 1024 * 1024, "pids_limit": 32}
    rendered = format_session_status(thread_id, db=db)
    assert f"Memory limit: {64 * 1024 * 1024} bytes" in rendered
    assert f"Memory + swap limit: {64 * 1024 * 1024} bytes (swap disabled)" in rendered
    assert "PID limit: 32" in rendered

    monkeypatch.setattr(session, "_docker_existing_resource_limits", lambda _name: ({}, ""))
    mismatch = ts.get_thread_session_status(db, thread_id)
    assert mismatch.status == "unhealthy"
    assert mismatch.reason == "resource_limit_mismatch"

    monkeypatch.setattr(
        session, "_docker_existing_resource_limits",
        lambda _name: (None, "Could not inspect Docker session resource limits: timeout"),
    )
    uncertain = ts.get_thread_session_status(db, thread_id)
    assert uncertain.status == "unhealthy"
    assert uncertain.reason == "resource_limit_mismatch"
    assert "timeout" in uncertain.message


def test_status_rejects_stale_enabled_labels_when_limits_are_now_disabled(monkeypatch, tmp_path):
    db, thread_id, _cfg = _configured(tmp_path, monkeypatch)
    monkeypatch.delenv("EGG_RLM_SESSION_MEMORY", raising=False)
    monkeypatch.delenv("EGG_RLM_SESSION_PIDS_LIMIT", raising=False)
    monkeypatch.setattr(session, "docker_session_available", lambda: True)
    monkeypatch.setattr(
        session, "_docker_container_state",
        lambda _name: session._DockerContainerState(True, True, "running"),
    )
    monkeypatch.setattr(
        session, "_docker_existing_resource_limits",
        lambda _name: ({"memory_bytes": 64 * 1024 * 1024, "memory_swap_bytes": 64 * 1024 * 1024}, ""),
    )

    status = ts.get_thread_session_status(db, thread_id)

    assert status.status == "unhealthy"
    assert status.reason == "resource_limit_mismatch"
    assert status.resource_limits == {}


@pytest.mark.parametrize(
    ("labels", "host_config", "expected", "error_fragment"),
    [
        (
            {
                "egg.session_memory_bytes": "67108864",
                "egg.session_memory_swap_bytes": "67108864",
                "egg.session_pids_limit": "32",
            },
            {"Memory": 67108864, "MemorySwap": 67108864, "PidsLimit": 32},
            {"memory_bytes": 67108864, "memory_swap_bytes": 67108864, "pids_limit": 32},
            "",
        ),
        (
            {
                "egg.session_memory_bytes": "67108864",
                "egg.session_memory_swap_bytes": "67108864",
            },
            {"Memory": 67108864, "MemorySwap": 134217728, "PidsLimit": 0},
            {"memory_bytes": 67108864, "memory_swap_bytes": 67108864},
            "HostConfig.MemorySwap",
        ),
        (
            {"egg.session_pids_limit": "not-an-integer"},
            {"Memory": 0, "MemorySwap": 0, "PidsLimit": 32},
            {},
            "label egg.session_pids_limit is invalid",
        ),
        (
            {},
            {"Memory": 67108864, "MemorySwap": 67108864, "PidsLimit": 0},
            {"memory_bytes": 67108864},
            "has no matching Egg resource-limit label",
        ),
    ],
)
def test_existing_limit_inspection_validates_labels_against_host_config(
    monkeypatch, labels, host_config, expected, error_fragment,
):
    payload = [{"Config": {"Labels": labels}, "HostConfig": host_config}]
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
    )

    limits, error = session._docker_existing_resource_limits("container")

    assert limits == expected
    assert error_fragment in error


def test_existing_limit_inspection_failure_is_not_treated_as_unlimited(monkeypatch):
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "daemon unavailable"),
    )

    limits, error = session._docker_existing_resource_limits("container")

    assert limits is None
    assert "daemon unavailable" in error
