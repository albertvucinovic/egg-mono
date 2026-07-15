from __future__ import annotations

"""Runtime-thread/session helpers for explicit RLM.

This module intentionally starts with the event-sourced *runtime thread*
layer before adding Docker/REPL providers.  A runtime thread is a real child
thread used as the execution/audit container for programmatic REPL tool calls.
"""

import json
import math
import os
import subprocess
import tempfile
import threading
import uuid
import time
import hashlib
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .db import ThreadsDB


@dataclass(frozen=True)
class RuntimeThreadConfig:
    """Configuration/linkage for a runtime child thread."""

    parent_thread_id: str
    runtime_thread_id: str
    language: str = "python"
    name: str = "default"
    session_id: Optional[str] = None
    source_event_seq: Optional[int] = None


@dataclass(frozen=True)
class SessionConfig:
    """Effective persistent session configuration for a runtime/thread."""

    enabled: bool = False
    provider: str = "docker"
    image: str = "egg-rlm-session"
    share: str = "private"
    session_id: Optional[str] = None
    owner_thread_id: Optional[str] = None
    workspace: str = "/workspace"
    network: str = "none"
    share_with_children_default: bool = False
    share_repl: bool = False
    source: str = "default"
    raw: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SessionStatus:
    """Lightweight status for a resolved session provider."""

    enabled: bool
    provider: str
    session_id: Optional[str]
    status: str
    message: str = ""
    container_name: Optional[str] = None
    share_repl: bool = False
    daemon_generation: Optional[str] = None
    active_requests: tuple[Dict[str, Any], ...] = ()
    channel_state: Dict[str, Any] = field(default_factory=dict)
    last_activity: Optional[float] = None
    heartbeat_at: Optional[float] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class _DockerContainerState:
    """Observed Docker state; ``exists=None`` means inspection failed."""

    exists: Optional[bool]
    running: Optional[bool]
    status: str = "unknown"
    error: str = ""


@dataclass(frozen=True)
class DockerSessionHandle:
    session_id: str
    container_name: str
    bridge_dir: str
    runtime_dir: str
    mount_dir: str
    workspace: str


@runtime_checkable
class SessionProvider(Protocol):
    """Persistent execution-session provider interface."""

    name: str

    def available(self) -> bool:
        ...

    def status(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig) -> SessionStatus:
        ...

    def start(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig) -> SessionStatus:
        ...

    def eval(
        self,
        db: ThreadsDB,
        runtime_thread_id: str,
        cfg: SessionConfig,
        *,
        language: str,
        code: str,
        repl_channel: str,
        eval_token: Optional[str],
        timeout_sec: Optional[float],
        cancel_check: Any = None,
    ) -> str:
        ...

    def stop(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> SessionStatus:
        ...

    def reset(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> str:
        ...

    def cleanup(self, db: ThreadsDB, **kwargs: Any) -> List[Dict[str, Any]]:
        ...


_DOCKER_MOUNT_POLICY = "thread-workdir-mask-egg-sandbox-v2"
_SESSION_ACTIVITY_LOCKS: Dict[str, threading.RLock] = {}
_SESSION_ACTIVITY_LOCK_USERS: Dict[str, int] = {}
_SESSION_ACTIVITY_LOCKS_GUARD = threading.Lock()
_SESSION_ACTIVITY_LOCAL = threading.local()


def _session_activity_lock(session_id: str) -> threading.RLock:
    with _SESSION_ACTIVITY_LOCKS_GUARD:
        lock = _SESSION_ACTIVITY_LOCKS.setdefault(session_id, threading.RLock())
        _SESSION_ACTIVITY_LOCK_USERS[session_id] = _SESSION_ACTIVITY_LOCK_USERS.get(session_id, 0) + 1
        return lock


def _discard_session_activity_lock_user(session_id: str, lock: threading.RLock) -> None:
    with _SESSION_ACTIVITY_LOCKS_GUARD:
        users = _SESSION_ACTIVITY_LOCK_USERS.get(session_id, 1) - 1
        if users <= 0:
            _SESSION_ACTIVITY_LOCK_USERS.pop(session_id, None)
            if _SESSION_ACTIVITY_LOCKS.get(session_id) is lock:
                _SESSION_ACTIVITY_LOCKS.pop(session_id, None)
        else:
            _SESSION_ACTIVITY_LOCK_USERS[session_id] = users


def _release_session_activity_lock(session_id: str, lock: threading.RLock) -> None:
    lock.release()
    _discard_session_activity_lock_user(session_id, lock)


def _session_activity_coordination_path(session_id: str) -> Path:
    identity = f"{_bridge_root()}\0{session_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / "egg-session-activity" / f"{digest}.lock"


@contextmanager
def _session_activity_guard(session_id: str, *, blocking: bool = True):
    """Serialize Docker eval/reference activity with idle reclamation."""

    session_id = str(session_id or "").strip()
    if not session_id:
        yield False
        return
    held = getattr(_SESSION_ACTIVITY_LOCAL, "held", None)
    if held is None:
        held = {}
        _SESSION_ACTIVITY_LOCAL.held = held
    if held.get(session_id, 0):
        held[session_id] += 1
        try:
            yield True
        finally:
            held[session_id] -= 1
        return

    thread_lock = _session_activity_lock(session_id)
    acquired = thread_lock.acquire(blocking=blocking)
    if not acquired:
        _discard_session_activity_lock_user(session_id, thread_lock)
        yield False
        return
    held[session_id] = 1
    lock_file = None
    file_locked = False
    try:
        lock_path = _session_activity_coordination_path(session_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+")
        try:
            import fcntl

            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(lock_file.fileno(), operation)
            file_locked = True
        except BlockingIOError:
            yield False
            return
        except (ImportError, OSError):
            if not blocking:
                # Automatic reclamation fails closed without cross-process
                # exclusion. Foreground Docker activity can still proceed.
                yield False
                return
        yield True
    finally:
        if lock_file is not None:
            if file_locked:
                try:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            lock_file.close()
        held.pop(session_id, None)
        _release_session_activity_lock(session_id, thread_lock)


@contextmanager
def _session_activity_guards(session_ids: List[str]):
    """Acquire multiple Docker-session guards in stable order."""

    ordered = sorted({str(value).strip() for value in session_ids if str(value).strip()})
    with ExitStack() as stack:
        for session_id in ordered:
            acquired = stack.enter_context(_session_activity_guard(session_id))
            if not acquired:
                raise RuntimeError(f"Could not acquire Docker session activity guard: {session_id}")
        yield


def _session_config_identity(cfg: SessionConfig) -> tuple[Any, ...]:
    return (
        cfg.enabled,
        cfg.provider,
        cfg.session_id,
        cfg.source,
        json.dumps(cfg.raw or {}, sort_keys=True, separators=(",", ":"), default=str),
    )


def _docker_guard_ids_for_transition(
    old_cfg: SessionConfig,
    *,
    enabled: bool,
    provider: str,
    session_id: Optional[str],
) -> List[str]:
    ids: List[str] = []
    if old_cfg.enabled and old_cfg.provider == "docker" and old_cfg.session_id:
        ids.append(old_cfg.session_id)
    if enabled and _clean_runtime_part(provider, "docker") == "docker" and session_id:
        ids.append(session_id)
    return ids


def _clean_runtime_part(value: Any, default: str) -> str:
    if isinstance(value, str):
        value = value.strip()
        if value:
            return value
    return default


def _parent_id(db: ThreadsDB, thread_id: str) -> Optional[str]:
    try:
        row = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row[0] if row and isinstance(row[0], str) and row[0] else None
    except Exception:
        return None


def _nearest_session_payload(db: ThreadsDB, thread_id: str) -> Optional[tuple[str, Dict[str, Any]]]:
    tid: Optional[str] = thread_id
    seen: set[str] = set()
    while tid and tid not in seen:
        seen.add(tid)
        try:
            row = db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='session.config' "
                "ORDER BY event_seq DESC LIMIT 1",
                (tid,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                return tid, payload
        tid = _parent_id(db, tid)
    return None


def _latest_session_lifecycle(
    db: ThreadsDB,
    thread_id: str,
    session_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Return the latest lifecycle payload for one session on a thread."""

    if not session_id:
        return None
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' "
            "ORDER BY event_seq DESC",
            (thread_id,),
        )
    except Exception:
        return None
    for (payload_json,) in cur.fetchall():
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("session_id") == session_id:
            return payload
    return None


def _latest_session_lifecycle_action(db: ThreadsDB, thread_id: str, session_id: Optional[str]) -> Optional[str]:
    """Return latest lifecycle action for a specific session id on a thread."""

    payload = _latest_session_lifecycle(db, thread_id, session_id)
    action = payload.get("action") if payload is not None else None
    return str(action) if action is not None else None


def _is_runtime_thread(db: ThreadsDB, thread_id: str) -> bool:
    try:
        row = db.conn.execute(
            "SELECT 1 FROM events WHERE thread_id=? AND type='runtime.thread' ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _session_id_for_thread(thread_id: str) -> str:
    """Return a stable default session id for a thread/runtime."""

    safe = ''.join(ch for ch in str(thread_id) if ch.isalnum())
    return f"sess_{safe}" if safe else f"sess_{os.urandom(5).hex()}"


def docker_session_container_name(db: ThreadsDB, session_id: str) -> str:
    """Return deterministic Docker container name for a session id."""

    db_hash = docker_session_db_hash(db)
    safe_session = ''.join(ch.lower() if ch.isalnum() else '-' for ch in str(session_id))
    return f"egg-rlm-{db_hash}-{safe_session[:48]}"


def docker_session_db_hash(db: ThreadsDB) -> str:
    """Return the stable physical-DB hash used in session labels and names.

    ``ThreadsDB.path`` may be relative in one Egg process and absolute in
    another (notably the CLI and EggW).  Hashing that spelling creates two
    containers for one logical session; both then consume requests from the
    same bridge directory.  Prefer SQLite's canonical main-database path so
    every process agrees on the container identity.
    """

    import hashlib

    canonical = next(
        (
            str(Path(row[2]).expanduser().resolve())
            for row in db.conn.execute("PRAGMA database_list")
            if str(row[1]) == "main" and row[2]
        ),
        "",
    )
    if not canonical:
        raw = str(getattr(db, "path", "") or "")
        canonical = raw if raw == ":memory:" else str(Path(raw).expanduser().resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def docker_session_available() -> bool:
    """Return True if Docker CLI/daemon appear available."""

    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def _bridge_root() -> Path:
    return (Path.cwd() / ".egg" / "rlm_sessions").resolve()


def _session_bridge_dir(session_id: str) -> Path:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in session_id)
    path = _bridge_root() / safe / "bridge"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_runtime_dir(session_id: str) -> Path:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in session_id)
    path = _bridge_root() / safe / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_mask_dir(session_id: str, name: str) -> Path:
    safe_session = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in session_id)
    safe_name = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in name)
    path = _bridge_root() / safe_session / "masks" / safe_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _session_mount_thread_id(db: ThreadsDB, runtime_thread_id: str, cfg: SessionConfig) -> str:
    """Choose which thread's working directory defines a session mount."""

    if cfg.share == "session" and cfg.owner_thread_id:
        # A shared Docker session has fixed container mounts. Reuse the
        # owner's working-directory scope so child runtimes do not silently
        # widen an already-running container by requesting a broader mount.
        return cfg.owner_thread_id
    return runtime_thread_id


def docker_session_mount_dir(db: ThreadsDB, runtime_thread_id: str, cfg: SessionConfig) -> Path:
    """Return the host directory mounted as the session workspace."""

    try:
        from .api import _ensure_thread_working_directory

        mount_tid = _session_mount_thread_id(db, runtime_thread_id, cfg)
        return _ensure_thread_working_directory(db, mount_tid).resolve()
    except Exception:
        return Path.cwd().resolve()


def _sandbox_path_values(settings: Dict[str, Any], key: str) -> List[str]:
    from .sandbox import _sandbox_filesystem_values

    return _sandbox_filesystem_values(settings, key)


def _resolve_sandbox_path(value: str, mount_dir: Path) -> Optional[Path]:
    from .sandbox import _resolve_sandbox_policy_path

    return _resolve_sandbox_policy_path(value, mount_dir)


def _container_workspace_path(host_path: Path, mount_dir: Path, workspace: str) -> Optional[str]:
    try:
        rel = host_path.resolve().relative_to(mount_dir.resolve())
    except Exception:
        return None
    container = Path(workspace.rstrip("/") or "/") / rel
    return str(container)


def _is_mount_equal_or_under(path: Path, root: Path) -> bool:
    try:
        p = path.resolve()
        r = root.resolve()
        return p == r or _is_relative_to(p, r)
    except Exception:
        return False


def _sandbox_network_to_docker(network: Any, fallback: str) -> str:
    """Map Egg sandbox network settings to Docker's coarse network modes."""

    if isinstance(network, str) and network.strip():
        return network.strip()
    if isinstance(network, dict):
        allowed = network.get("allowedDomains")
        denied = network.get("deniedDomains")
        if isinstance(allowed, list):
            # Domain-level filtering is not expressible with plain Docker.  Use
            # coarse semantics: empty allowlist denies all network; a non-empty
            # allowlist needs network access and relies on external DNS/proxy
            # controls if stricter domain filtering is required.
            return "none" if len(allowed) == 0 else (fallback or "bridge")
        if isinstance(denied, list) and denied:
            # Docker cannot deny specific domains. Keep the fallback mode.
            return fallback or "none"
    return fallback or "none"


def _docker_repl_mount_args_from_sandbox(
    *,
    mount_dir: Path,
    workspace: str,
    sandbox_settings: Dict[str, Any],
    skip_denied_paths: Optional[List[Path]] = None,
) -> List[str]:
    """Translate filesystem sandbox policy into Docker REPL mount flags.

    The persistent REPL container cannot be wrapped per eval, so its direct
    file I/O boundary must be represented in container mounts.  The workspace
    is read-write by default; an explicit ``filesystem.allowWrite`` list narrows
    writes by mounting the workspace read-only and overlaying the allowed paths
    read-write.  denyRead/denyWrite paths are masked with read-only empty
    directories where possible.
    """

    mount_dir = mount_dir.resolve()
    workspace = workspace or "/workspace"
    fs = sandbox_settings.get("filesystem") if isinstance(sandbox_settings, dict) else None
    explicit_allow_write = isinstance(fs, dict) and "allowWrite" in fs

    args: List[str] = ["-v", f"{mount_dir}:{workspace}:ro"]

    allow_paths: List[Path] = []
    if not explicit_allow_write:
        allow_paths.append(mount_dir)
    else:
        for raw in _sandbox_path_values(sandbox_settings, "allowWrite"):
            p = _resolve_sandbox_path(raw, mount_dir)
            if p is not None and _is_mount_equal_or_under(p, mount_dir):
                allow_paths.append(p)

    # If allowWrite includes the workspace root, the policy grants writes to
    # the whole thread working directory.  Keep the single root rw mount and
    # rely on deny masks below for protected paths.
    root_rw = any(p.resolve() == mount_dir for p in allow_paths)
    if root_rw:
        args = ["-v", f"{mount_dir}:{workspace}"]
    else:
        for p in sorted({p.resolve() for p in allow_paths}, key=lambda item: len(item.parts)):
            # Docker creates missing bind sources as root-owned directories, so
            # create explicit directories to keep ownership predictable.
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            container_path = _container_workspace_path(p, mount_dir, workspace)
            if container_path:
                args.extend(["-v", f"{p}:{container_path}"])

    skip_roots = [p.resolve() for p in (skip_denied_paths or [])]
    denied: List[Path] = []
    for key in ("denyRead", "denyWrite"):
        for raw in _sandbox_path_values(sandbox_settings, key):
            p = _resolve_sandbox_path(raw, mount_dir)
            if p is None or not _is_mount_equal_or_under(p, mount_dir):
                continue
            if any(_is_mount_equal_or_under(p, skip) for skip in skip_roots):
                # These paths are already masked by fixed REPL safety mounts
                # (notably .egg). Avoid duplicate/nested Docker
                # bind mounts, which can fail on some Docker versions.
                continue
            if any(_is_mount_equal_or_under(p, existing) for existing in denied):
                # A broader denied ancestor already hides this path.
                continue
            # If this path is a broader ancestor of existing denied paths, keep
            # only the broader mount.
            denied = [existing for existing in denied if not _is_mount_equal_or_under(existing, p)]
            if p.exists() and p.is_file():
                # Masking files with directory bind mounts is not portable.
                # The containing directory is the safe over-approximation.
                p = p.parent
            if _is_mount_equal_or_under(p, mount_dir):
                denied.append(p)

    for p in sorted({p.resolve() for p in denied}, key=lambda item: len(item.parts)):
        container_path = _container_workspace_path(p, mount_dir, workspace)
        if not container_path:
            continue
        mask = _session_mask_dir("sandbox", hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:16])
        args.extend(["-v", f"{mask}:{container_path}:ro"])

    return args


def _docker_repl_mandatory_mask_args(*, mount_dir: Path, workspace: str, session_id: str) -> List[str]:
    """Return final non-overridable masks for Egg-private workspace paths."""

    workspace = workspace or "/workspace"
    mask_dir = _session_mask_dir(session_id, "egg")
    return ["-v", f"{mask_dir}:{workspace.rstrip('/')}/.egg:ro"]


def _docker_existing_mount_policy(container_name: str) -> Optional[str]:
    return _docker_existing_label(container_name, "egg.mount_policy")


def _docker_existing_sandbox_policy_hash(container_name: str) -> Optional[str]:
    return _docker_existing_label(container_name, "egg.sandbox_policy_hash")


def _docker_existing_label(container_name: str, label: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{ index .Config.Labels " + json.dumps(str(label)) + " }}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def _docker_session_policy_hash(db: ThreadsDB, runtime_thread_id: str, cfg: SessionConfig) -> str:
    """Return a stable hash of the security-relevant Docker session policy."""

    body: Dict[str, Any] = {
        "mount_policy": _DOCKER_MOUNT_POLICY,
        "image": cfg.image,
        "workspace": cfg.workspace,
        "network": cfg.network,
        "mount_dir": str(docker_session_mount_dir(db, runtime_thread_id, cfg).resolve()),
    }
    try:
        from .sandbox import get_thread_sandbox_config

        sb = get_thread_sandbox_config(db, runtime_thread_id)
        body["sandbox"] = {
            "enabled": bool(sb.enabled),
            "provider": sb.provider,
            "settings": dict(sb.settings or {}),
        }
    except Exception as e:
        body["sandbox_error"] = f"{type(e).__name__}: {e}"
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:24]


def _docker_container_created_at(container_name: str) -> Optional[float]:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.Created}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        from datetime import datetime

        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None


def list_docker_session_containers(db: ThreadsDB) -> List[Dict[str, Any]]:
    """List Docker containers belonging to this Egg database."""

    db_hash = docker_session_db_hash(db)
    try:
        proc = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=egg.kind=rlm-session",
                "--filter", f"label=egg.db_hash={db_hash}",
                "--format", "{{.Names}}\t{{.Status}}\t{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out: List[Dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        name = parts[0]
        status = parts[1] if len(parts) > 1 else ""
        cid = parts[2] if len(parts) > 2 else ""
        out.append({
            "name": name,
            "status": status,
            "id": cid,
            "running": _docker_inspect_running(name) is True,
            "created_at": _docker_container_created_at(name),
            "mount_policy": _docker_existing_mount_policy(name),
        })
    return out


def cleanup_docker_sessions(
    db: ThreadsDB,
    *,
    stopped_only: bool = True,
    older_than_sec: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Remove Docker RLM session containers for this database.

    Selection is label-based (`egg.kind=rlm-session` and this DB hash) so it
    does not touch unrelated containers.  By default only stopped containers
    are removed.
    """

    now = time.time()
    removed: List[Dict[str, Any]] = []
    for info in list_docker_session_containers(db):
        if stopped_only and info.get("running"):
            continue
        created = info.get("created_at")
        if older_than_sec is not None and isinstance(created, (int, float)):
            if now - float(created) < float(older_than_sec):
                continue
        name = str(info.get("name") or "")
        if not name:
            continue
        try:
            proc = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=20)
            ok = proc.returncode == 0
            error = (proc.stderr or proc.stdout or "").strip()
        except Exception as e:
            ok = False
            error = str(e)
        item = dict(info)
        item["removed"] = ok
        if error and not ok:
            item["error"] = error
        removed.append(item)
    return removed


def cleanup_thread_sessions(db: ThreadsDB, provider_name: str = "docker", **kwargs: Any) -> List[Dict[str, Any]]:
    """Run cleanup for the named session provider."""

    provider = get_session_provider(provider_name)
    if provider is None:
        return []
    return provider.cleanup(db, **kwargs)


def repl_channel_name(runtime_thread_id: str, repl_name: str = "default", *, share_repl: bool = False) -> str:
    """Return the provider-level REPL channel name for an eval.

    A Docker/session may be shared between multiple runtime threads, but the
    interpreter channel must *not* be shared by accident.  By default we scope
    the channel by runtime thread id; callers that intentionally want shared
    Python/Bash interpreter state can opt in with ``share_repl=True``.
    """

    name = _clean_runtime_part(repl_name, "default")
    if share_repl:
        return name
    safe_tid = ''.join(ch if ch.isalnum() else '_' for ch in str(runtime_thread_id))
    return f"{safe_tid}:{name}" if safe_tid else name


_PYTHON_REPL_RUNTIME_FILES = (
    "eggtools.py",
    "_eggtools_generated.py",
    "repl_refresh.py",
)
_DOCKER_REFRESHED_PYTHON_RUNTIMES: set[tuple[str, str, str]] = set()


def _invalidate_python_runtime_refresh_cache(runtime_dir: Path) -> None:
    runtime_key = str(runtime_dir)
    _DOCKER_REFRESHED_PYTHON_RUNTIMES.difference_update(
        key for key in _DOCKER_REFRESHED_PYTHON_RUNTIMES if key[0] == runtime_key
    )


def _python_repl_runtime_code_hash(runtime_dir: Path) -> str:
    """Hash the Egg-owned Python code loaded inside persistent REPL workers."""

    digest = hashlib.sha256()
    for name in _PYTHON_REPL_RUNTIME_FILES:
        data = (runtime_dir / name).read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:24]


def _python_repl_runtime_refresh_code(expected_hash: str) -> str:
    """Build a state-preserving refresh eval understood by old session daemons."""

    refresh_path = "/egg-runtime/repl_refresh.py"
    return (
        f"if globals().get('__egg_runtime_code_hash__') != {expected_hash!r}:\n"
        "    exec(compile(__import__('pathlib').Path("
        f"{refresh_path!r}).read_text(encoding='utf-8'), {refresh_path!r}, 'exec'), "
        "{'__name__': '__egg_runtime_refresh__', 'repl_globals': globals(), "
        f"'runtime_dir': '/egg-runtime', 'expected_hash': {expected_hash!r}" + "})\n"
    )


def _write_runtime_files(runtime_dir: Path) -> None:
    from importlib import resources

    for name in ("eggtools.py", "sessiond.py", "eggtool", "repl_refresh.py"):
        try:
            data = resources.files("eggthreads.session_runtime").joinpath(name).read_text(encoding="utf-8")
        except Exception:
            src = Path(__file__).resolve().parent / "session_runtime" / name
            data = src.read_text(encoding="utf-8")
        (runtime_dir / name).write_text(data, encoding="utf-8")
        if name == "eggtool":
            try:
                os.chmod(runtime_dir / name, 0o755)
            except Exception:
                pass
    try:
        from .tools import create_default_tools

        try:
            from .session_runtime.tool_wrappers import generate_tool_wrappers_source
        except Exception:
            import importlib.util

            helper_path = Path(__file__).resolve().parent / "session_runtime" / "tool_wrappers.py"
            spec = importlib.util.spec_from_file_location("eggthreads.session_runtime.tool_wrappers", helper_path)
            if spec is None or spec.loader is None:
                raise
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            generate_tool_wrappers_source = helper.generate_tool_wrappers_source

        specs = [entry["spec"] for entry in create_default_tools()._tools.values()]
        (runtime_dir / "_eggtools_generated.py").write_text(generate_tool_wrappers_source(specs), encoding="utf-8")
    except Exception:
        # The generic eggtools.tool(name, **kwargs) bridge remains available if
        # wrapper generation fails.
        pass


def _docker_container_state(container_name: str) -> _DockerContainerState:
    """Inspect existence/running state without conflating missing with errors."""

    try:
        proc = subprocess.run(
            [
                "docker", "inspect", "-f",
                "{{.State.Status}}\t{{.State.Running}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        return _DockerContainerState(None, None, error=f"{type(e).__name__}: {e}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Docker inspect failed").strip()
        lowered = detail.lower()
        if "no such object" in lowered or "no such container" in lowered:
            return _DockerContainerState(False, False, status="missing")
        return _DockerContainerState(None, None, error=detail)
    parts = (proc.stdout or "").strip().split("\t", 1)
    status = (parts[0] if parts else "unknown").strip().lower() or "unknown"
    running_text = (parts[1] if len(parts) > 1 else "").strip().lower()
    running = running_text == "true" if running_text in {"true", "false"} else None
    return _DockerContainerState(True, running, status=status)


def _docker_inspect_running(container_name: str) -> Optional[bool]:
    state = _docker_container_state(container_name)
    if state.exists is False:
        return None
    return state.running


def _docker_session_container_names(session_id: str) -> List[str]:
    """Return all Docker containers claiming one logical Egg session."""

    try:
        proc = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=egg.kind=rlm-session",
                "--filter", f"label=egg.session_id={session_id}",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _docker_bind_mount_source(container_name: str, destination: str) -> Optional[str]:
    """Return the resolved host source mounted at ``destination``."""

    try:
        proc = subprocess.run(
            [
                "docker", "inspect", "-f",
                "{{range .Mounts}}{{if eq .Destination " + json.dumps(destination) + "}}{{.Source}}{{end}}{{end}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    source = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    return str(Path(source).resolve()) if source else None


def _reconcile_docker_session_containers(
    container_name: str,
    session_id: str,
    bridge_dir: Path,
    runtime_dir: Path,
) -> None:
    """Collapse legacy duplicate daemons without discarding the sole worker.

    Older releases derived the container's DB hash from the literal path, so
    a relative-path CLI and absolute-path EggW could run different daemons for
    the same session.  Sharing ``/egg-bridge`` makes that unsafe: a refresh
    request can reach one daemon and the following user eval another.

    Only containers with the same session label and exact bridge/runtime
    mounts are reconciled.  The canonical container wins when present.  If
    there is only a legacy container, rename it so its live interpreter state
    survives the migration.  Unrelated containers are never removed.
    """

    expected_bridge = str(bridge_dir.resolve())
    expected_runtime = str(runtime_dir.resolve())

    def matching_candidates() -> List[str]:
        return [
            candidate
            for candidate in _docker_session_container_names(session_id)
            if _docker_bind_mount_source(candidate, "/egg-bridge") == expected_bridge
            and _docker_bind_mount_source(candidate, "/egg-runtime") == expected_runtime
        ]

    candidates = matching_candidates()

    target_exists = _docker_inspect_running(container_name) is not None
    if target_exists and container_name not in candidates:
        # Another process may have created/renamed the canonical container
        # between the label query and inspect. Re-query before treating it as
        # an ownership collision.
        candidates = matching_candidates()
        if container_name not in candidates:
            raise RuntimeError(
                f"Docker container name collision for {container_name}: "
                "the existing container does not own this Egg session bridge/runtime."
            )

    survivor = container_name if container_name in candidates else None
    if survivor is None and candidates:
        survivor = min(
            candidates,
            key=lambda name: (
                _docker_inspect_running(name) is not True,
                _docker_container_created_at(name) or float("inf"),
                name,
            ),
        )
        proc = subprocess.run(
            ["docker", "rename", survivor, container_name],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            # A concurrent upgraded process may have won the same migration.
            candidates = matching_candidates()
            if container_name not in candidates:
                error = (proc.stderr or proc.stdout or "Docker rename failed").strip()
                raise RuntimeError(f"Could not migrate legacy Docker session {survivor}: {error}")
        else:
            candidates = [container_name if name == survivor else name for name in candidates]
        survivor = container_name

    failures: List[str] = []
    for candidate in candidates:
        if candidate in {container_name, survivor}:
            continue
        try:
            proc = subprocess.run(
                ["docker", "rm", "-f", candidate],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as e:
            failures.append(f"{candidate}: {type(e).__name__}: {e}")
            continue
        if proc.returncode != 0:
            failures.append(
                f"{candidate}: {(proc.stderr or proc.stdout or 'Docker removal failed').strip()}"
            )
    if failures:
        raise RuntimeError(
            "Could not remove competing Docker session container(s): "
            + "; ".join(failures)
        )


def _clear_docker_daemon_status(bridge_dir: Path) -> None:
    """Remove old generation/heartbeat records before a container process starts."""

    for name in ("sessiond_generation.json", "sessiond_status.json"):
        try:
            (bridge_dir / name).unlink()
        except FileNotFoundError:
            pass


def _start_docker_container(
    db: ThreadsDB,
    runtime_thread_id: str,
    cfg: SessionConfig,
    container_name: str,
    bridge_dir: Path,
    runtime_dir: Path,
    force_restart: bool = False,
) -> bool:
    """Ensure the session container runs; return whether its process restarted."""
    expected_policy_hash = _docker_session_policy_hash(db, runtime_thread_id, cfg)

    if cfg.session_id:
        _reconcile_docker_session_containers(
            container_name,
            cfg.session_id,
            bridge_dir,
            runtime_dir,
        )

    def _remove_existing_container() -> None:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False, timeout=20)

    existing_running = _docker_inspect_running(container_name)
    if existing_running is True:
        policy = _docker_existing_mount_policy(container_name)
        policy_hash = _docker_existing_sandbox_policy_hash(container_name)
        if policy == _DOCKER_MOUNT_POLICY and policy_hash == expected_policy_hash:
            if not force_restart:
                return False
            _clear_docker_daemon_status(bridge_dir)
            subprocess.run(
                ["docker", "restart", container_name],
                capture_output=True,
                check=True,
                timeout=30,
            )
            return True
        _remove_existing_container()
    if existing_running is False:
        policy = _docker_existing_mount_policy(container_name)
        policy_hash = _docker_existing_sandbox_policy_hash(container_name)
        if policy == _DOCKER_MOUNT_POLICY and policy_hash == expected_policy_hash:
            _clear_docker_daemon_status(bridge_dir)
            subprocess.run(["docker", "start", container_name], capture_output=True, check=True, timeout=20)
            return True
        _remove_existing_container()

    workspace = cfg.workspace or "/workspace"
    network = cfg.network or "none"
    mount_dir = docker_session_mount_dir(db, runtime_thread_id, cfg)
    mandatory_mask_args = _docker_repl_mandatory_mask_args(
        mount_dir=mount_dir,
        workspace=workspace,
        session_id=cfg.session_id or container_name,
    )
    sandbox_mount_args = ["-v", f"{mount_dir}:{workspace}"]
    sandbox_effective = False
    try:
        from .sandbox import get_thread_sandbox_config, normalize_provider_settings

        sb = get_thread_sandbox_config(db, runtime_thread_id)
        sandbox_effective = bool(sb.enabled)
        if sandbox_effective:
            # Use the thread sandbox policy for persistent REPL mounts. If the
            # thread's sandbox provider is srt/bwrap, its filesystem/network
            # policy is still useful and translated onto Docker's coarser model.
            settings = normalize_provider_settings("docker", dict(sb.settings or {}))
            # Do not call apply_mandatory_protections("srt", ...) here: this
            # REPL mount layer has a stronger fixed empty mask for .egg below.
            # We also want missing allowWrite to mean workspace rw by
            # default, not the Docker provider default's allowWrite=["."].
            network = _sandbox_network_to_docker(settings.get("network"), network)
            sandbox_mount_args = _docker_repl_mount_args_from_sandbox(
                mount_dir=mount_dir,
                workspace=workspace,
                sandbox_settings=settings,
                skip_denied_paths=[mount_dir / ".egg"],
            )
    except Exception:
        sandbox_effective = False

    cmd = [
        "docker", "run", "-d", "--init",
        "--name", container_name,
        "--cpus", "4",
        "--user", f"{os.getuid()}",
        "--network", network,
        "--label", "egg.kind=rlm-session",
        "--label", f"egg.session_id={cfg.session_id}",
        "--label", f"egg.owner_thread_id={cfg.owner_thread_id or runtime_thread_id}",
        "--label", f"egg.runtime_thread_id={runtime_thread_id}",
        "--label", f"egg.db_hash={docker_session_db_hash(db)}",
        "--label", f"egg.mount_policy={_DOCKER_MOUNT_POLICY}",
        "--label", f"egg.sandbox_policy_hash={expected_policy_hash}",
        "--label", f"egg.sandbox_mounts={'on' if sandbox_effective else 'off'}",
        "-v", f"{bridge_dir}:/egg-bridge",
        "-v", f"{runtime_dir}:/egg-runtime:ro",
        *sandbox_mount_args,
        *mandatory_mask_args,
        "--cap-drop", "ALL",
        "-w", workspace,
        cfg.image,
        "python3", "/egg-runtime/sessiond.py", "--bridge-dir", "/egg-bridge", "--runtime-dir", "/egg-runtime",
    ]
    _clear_docker_daemon_status(bridge_dir)
    subprocess.run(cmd, capture_output=True, check=True, timeout=60)
    return True


def get_thread_session_config(db: ThreadsDB, thread_id: str) -> SessionConfig:
    """Resolve effective session.config for a thread, with ancestor inheritance."""

    found = _nearest_session_payload(db, thread_id)
    if found is None:
        return SessionConfig()

    source_tid, payload = found
    if source_tid != thread_id and not _is_runtime_thread(db, thread_id):
        # Persistent execution sessions are not inherited by ordinary children
        # unless the owner explicitly opted into sharing with future children.
        # Runtime threads are implementation children of their caller and must
        # still inherit the caller's session policy.
        if not bool(payload.get("share_with_children_default", False)):
            return SessionConfig()
    enabled = bool(payload.get("enabled", False))
    provider = _clean_runtime_part(payload.get("provider"), "docker")
    image = _clean_runtime_part(payload.get("image"), "egg-rlm-session")
    share = _clean_runtime_part(payload.get("share"), "private")
    workspace = _clean_runtime_part(payload.get("workspace"), "/workspace")
    network = _clean_runtime_part(payload.get("network"), "none")
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    owner_thread_id = payload.get("owner_thread_id") if isinstance(payload.get("owner_thread_id"), str) else source_tid
    if enabled and not session_id:
        session_id = _session_id_for_thread(owner_thread_id or thread_id)
    return SessionConfig(
        enabled=enabled,
        provider=provider,
        image=image,
        share=share,
        session_id=session_id,
        owner_thread_id=owner_thread_id,
        workspace=workspace,
        network=network,
        share_with_children_default=bool(payload.get("share_with_children_default", False)),
        share_repl=bool(payload.get("share_repl", False)),
        source=f"event:{source_tid}",
        raw=dict(payload),
    )


def _set_thread_session_config_unlocked(
    db: ThreadsDB,
    thread_id: str,
    *,
    enabled: bool,
    provider: str = "docker",
    image: str = "egg-rlm-session",
    share: str = "private",
    session_id: Optional[str] = None,
    owner_thread_id: Optional[str] = None,
    workspace: str = "/workspace",
    network: str = "none",
    share_with_children_default: bool = False,
    share_repl: bool = False,
    reason: str = "user",
) -> str:
    """Append a session.config event and return the effective session_id."""

    sid = session_id or (_session_id_for_thread(owner_thread_id or thread_id) if enabled else None)
    payload: Dict[str, Any] = {
        "enabled": bool(enabled),
        "provider": _clean_runtime_part(provider, "docker"),
        "image": _clean_runtime_part(image, "egg-rlm-session"),
        "share": _clean_runtime_part(share, "private"),
        "workspace": _clean_runtime_part(workspace, "/workspace"),
        "network": _clean_runtime_part(network, "none"),
        "share_with_children_default": bool(share_with_children_default),
        "share_repl": bool(share_repl),
        "reason": reason,
    }
    if sid:
        payload["session_id"] = sid
    if owner_thread_id:
        payload["owner_thread_id"] = owner_thread_id
    else:
        payload["owner_thread_id"] = thread_id
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_="session.config",
        msg_id=None,
        invoke_id=None,
        payload=payload,
    )
    return sid or ""


def set_thread_session_config(
    db: ThreadsDB,
    thread_id: str,
    *,
    enabled: bool,
    provider: str = "docker",
    image: str = "egg-rlm-session",
    share: str = "private",
    session_id: Optional[str] = None,
    owner_thread_id: Optional[str] = None,
    workspace: str = "/workspace",
    network: str = "none",
    share_with_children_default: bool = False,
    share_repl: bool = False,
    reason: str = "user",
) -> str:
    """Append session config while excluding idle reclamation for its session."""

    effective_session_id = session_id
    if enabled and not effective_session_id:
        effective_session_id = _session_id_for_thread(owner_thread_id or thread_id)

    while True:
        old_cfg = get_thread_session_config(db, thread_id)
        guard_ids = _docker_guard_ids_for_transition(
            old_cfg,
            enabled=enabled,
            provider=provider,
            session_id=effective_session_id,
        )
        guard = _session_activity_guards(guard_ids) if guard_ids else nullcontext()
        retry = False
        with guard:
            # Re-resolve under the old/new guards. If another writer won before
            # acquisition, release and retry with its current Docker identity.
            current_cfg = get_thread_session_config(db, thread_id)
            required = _docker_guard_ids_for_transition(
                current_cfg,
                enabled=enabled,
                provider=provider,
                session_id=effective_session_id,
            )
            if set(required) - set(guard_ids):
                retry = True
            else:
                return _set_thread_session_config_unlocked(
                    db,
                    thread_id,
                    enabled=enabled,
                    provider=provider,
                    image=image,
                    share=share,
                    session_id=effective_session_id,
                    owner_thread_id=owner_thread_id,
                    workspace=workspace,
                    network=network,
                    share_with_children_default=share_with_children_default,
                    share_repl=share_repl,
                    reason=reason,
                )
        if retry:
            continue


def enable_thread_session(db: ThreadsDB, thread_id: str, **kwargs: Any) -> str:
    """Enable a persistent session for a thread and return its session_id."""

    return set_thread_session_config(db, thread_id, enabled=True, **kwargs)


def disable_thread_session(db: ThreadsDB, thread_id: str, *, reason: str = "user") -> None:
    """Disable the effective persistent session for a thread."""

    set_thread_session_config(db, thread_id, enabled=False, reason=reason)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _parse_duration_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    mult = 1.0
    if text.endswith("ms"):
        mult = 0.001
        text = text[:-2]
    elif text.endswith("s"):
        text = text[:-1]
    elif text.endswith("m"):
        mult = 60.0
        text = text[:-1]
    elif text.endswith("h"):
        mult = 3600.0
        text = text[:-1]
    elif text.endswith("d"):
        mult = 86400.0
        text = text[:-1]
    try:
        return float(text) * mult
    except Exception:
        return None


def auto_session_idle_timeout_sec(value: Any = None) -> Optional[float]:
    """Resolve the opt-in idle timeout for auto-created Docker sessions."""

    raw = os.environ.get("EGG_RLM_AUTO_SESSION_IDLE_TIMEOUT") if value is None else value
    timeout = _parse_duration_seconds(raw)
    if timeout is None or not math.isfinite(timeout) or timeout <= 0:
        return None
    return float(timeout)


def _local_session_config_payload(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='session.config' "
            "ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _auto_docker_session_candidate(db: ThreadsDB, thread_id: str) -> Optional[SessionConfig]:
    payload = _local_session_config_payload(db, thread_id)
    reason = str((payload or {}).get("reason") or "")
    if not reason.startswith("auto:"):
        return None
    cfg = get_thread_session_config(db, thread_id)
    if (
        not cfg.enabled
        or cfg.provider != "docker"
        or not cfg.session_id
        or cfg.source != f"event:{thread_id}"
        or cfg.share != "private"
        or cfg.share_with_children_default
    ):
        return None
    return cfg


def _strict_effective_session_reference(
    db: ThreadsDB,
    thread_id: str,
) -> tuple[bool, Optional[str]]:
    current: Optional[str] = thread_id
    seen: set[str] = set()
    source_thread_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    while current and current not in seen:
        seen.add(current)
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='session.config' "
            "ORDER BY event_seq DESC LIMIT 1",
            (current,),
        ).fetchone()
        if row is not None:
            loaded = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if not isinstance(loaded, dict):
                raise ValueError(f"session.config for {current} is not a JSON object")
            source_thread_id = current
            payload = loaded
            break
        parent = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=? LIMIT 1",
            (current,),
        ).fetchone()
        current = str(parent[0]) if parent and parent[0] else None
    if payload is None or source_thread_id is None:
        return False, None
    if source_thread_id != thread_id:
        runtime = db.conn.execute(
            "SELECT 1 FROM events WHERE thread_id=? AND type='runtime.thread' LIMIT 1",
            (thread_id,),
        ).fetchone()
        if runtime is None and not bool(payload.get("share_with_children_default", False)):
            return False, None
    if not bool(payload.get("enabled", False)):
        return False, None
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        owner = str(payload.get("owner_thread_id") or source_thread_id or thread_id)
        session_id = _session_id_for_thread(owner)
    return True, session_id


def _session_reference_thread_ids(
    db: ThreadsDB,
    session_id: str,
) -> tuple[Optional[List[str]], str]:
    """Resolve every existing thread's effective config, failing closed."""

    try:
        rows = db.conn.execute("SELECT thread_id FROM threads ORDER BY thread_id").fetchall()
        references: List[str] = []
        for row in rows:
            thread_id = str(row[0])
            enabled, effective_session_id = _strict_effective_session_reference(db, thread_id)
            if enabled and effective_session_id == session_id:
                references.append(thread_id)
        return references, ""
    except Exception as e:
        return None, f"session reference scan failed: {type(e).__name__}: {e}"


def reap_idle_auto_docker_sessions(
    db: ThreadsDB,
    *,
    idle_timeout_sec: Any = None,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Stop safely reclaimable auto-created Docker sessions.

    This is deliberately conservative: only fresh Phase 4 ``ready`` status can
    authorize reclamation. The function never removes containers or channels.
    """

    threshold = auto_session_idle_timeout_sec(idle_timeout_sec)
    if threshold is None:
        return []
    observed_at = time.time() if now is None else float(now)
    try:
        thread_ids = [str(row[0]) for row in db.conn.execute(
            "SELECT thread_id FROM threads ORDER BY thread_id"
        ).fetchall()]
    except Exception:
        return []

    results: List[Dict[str, Any]] = []
    processed_sessions: set[str] = set()
    for thread_id in thread_ids:
        cfg = _auto_docker_session_candidate(db, thread_id)
        if cfg is None or not cfg.session_id or cfg.session_id in processed_sessions:
            continue
        session_id = cfg.session_id
        processed_sessions.add(session_id)
        item: Dict[str, Any] = {
            "thread_id": thread_id,
            "session_id": session_id,
            "idle_timeout_sec": threshold,
            "reclaimed": False,
        }
        with _session_activity_guard(session_id, blocking=False) as acquired:
            if not acquired:
                item.update(status="skipped", reason="host_activity")
                results.append(item)
                continue
            # Re-read eligibility and references inside the same cross-process
            # activity boundary used by config writes and Docker evals.
            current = _auto_docker_session_candidate(db, thread_id)
            if current is None or current.session_id != session_id:
                item.update(status="skipped", reason="configuration_changed")
                results.append(item)
                continue
            references, reference_error = _session_reference_thread_ids(db, session_id)
            if references is None:
                item.update(status="skipped", reason="reference_scan_failed", error=reference_error)
                results.append(item)
                continue
            other_references = [ref for ref in references if ref != thread_id]
            if other_references:
                item.update(
                    status="skipped",
                    reason="shared_references",
                    reference_thread_ids=references,
                )
                results.append(item)
                continue

            final_cfg = _auto_docker_session_candidate(db, thread_id)
            if final_cfg is None or _session_config_identity(final_cfg) != _session_config_identity(current):
                item.update(status="skipped", reason="configuration_changed")
                results.append(item)
                continue
            try:
                status = _session_status_for_config(db, thread_id, current)
            except Exception as e:
                item.update(
                    status="skipped",
                    reason="status_unavailable",
                    error=f"{type(e).__name__}: {e}",
                )
                results.append(item)
                continue
            item["observed_status"] = status.status
            if status.active_requests:
                item.update(status="skipped", reason="active_requests")
                results.append(item)
                continue
            if status.status != "ready":
                item.update(status="skipped", reason=f"session_{status.status}")
                results.append(item)
                continue
            if not isinstance(status.last_activity, (int, float)) or not math.isfinite(float(status.last_activity)):
                item.update(status="skipped", reason="last_activity_unavailable")
                results.append(item)
                continue
            idle_for = max(0.0, observed_at - float(status.last_activity))
            item["idle_for_sec"] = idle_for
            item["last_activity_at"] = float(status.last_activity)
            if idle_for <= threshold:
                item.update(status="skipped", reason="below_idle_threshold")
                results.append(item)
                continue

            final_cfg = _auto_docker_session_candidate(db, thread_id)
            if final_cfg is None or _session_config_identity(final_cfg) != _session_config_identity(current):
                item.update(status="skipped", reason="configuration_changed")
                results.append(item)
                continue
            stop_reason = f"idle_reap:{threshold:g}s"
            try:
                stopped = _stop_captured_session(db, thread_id, current, reason=stop_reason)
            except Exception as e:
                item.update(
                    status="unhealthy",
                    reason=stop_reason,
                    error=f"{type(e).__name__}: {e}",
                )
                results.append(item)
                continue
            item.update(
                status=stopped.status,
                reason=stop_reason,
                reclaimed=stopped.status == "stopped",
                message=stopped.message,
            )
            results.append(item)
    return results


_IDLE_REAPER_THREADS_LOCK = threading.Lock()
_IDLE_REAPER_DATABASES: set[str] = set()
_IDLE_REAPER_CADENCE_SEC = 30.0


def _canonical_database_path(db: ThreadsDB) -> Optional[str]:
    raw_path = str(getattr(db, "path", "") or "")
    if not raw_path or raw_path == ":memory:":
        return None
    try:
        rows = db.conn.execute("PRAGMA database_list").fetchall()
        main_path = next(
            (str(row[2]) for row in rows if str(row[1]) == "main" and row[2]),
            "",
        )
    except Exception:
        main_path = ""
    return str(Path(main_path or raw_path).expanduser().resolve())


def _idle_reaper_coordination_path(db_path: str) -> Path:
    digest = hashlib.sha256(db_path.encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / "egg-session-reaper" / f"{digest}.lock"


def _run_coordinated_idle_reaper_pass(
    db_path: str,
    *,
    now: Optional[float] = None,
) -> bool:
    """Run at most one cross-process pass per canonical DB/cadence."""

    try:
        import fcntl
    except ImportError:
        return False
    try:
        lock_path = _idle_reaper_coordination_path(db_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as coordination:
            try:
                fcntl.flock(coordination.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return False
            current = time.time() if now is None else float(now)
            coordination.seek(0)
            raw_last = coordination.read().strip()
            try:
                last_started = float(raw_last)
            except (TypeError, ValueError):
                last_started = 0.0
            if (
                not math.isfinite(current)
                or current <= 0
                or (math.isfinite(last_started) and current - last_started < _IDLE_REAPER_CADENCE_SEC)
            ):
                return False
            coordination.seek(0)
            coordination.truncate()
            coordination.write(f"{current:.9f}")
            coordination.flush()
            os.fsync(coordination.fileno())
            worker_db = ThreadsDB(db_path)
            try:
                reap_idle_auto_docker_sessions(worker_db, now=current)
            finally:
                worker_db.conn.close()
            return True
    except Exception:
        return False


def start_idle_auto_docker_reaper(db: ThreadsDB) -> bool:
    """Start a bounded, cross-process-coordinated maintenance pass."""

    if auto_session_idle_timeout_sec() is None:
        return False
    db_path = _canonical_database_path(db)
    if db_path is None:
        return False
    with _IDLE_REAPER_THREADS_LOCK:
        if db_path in _IDLE_REAPER_DATABASES:
            return False
        _IDLE_REAPER_DATABASES.add(db_path)

    def run() -> None:
        try:
            _run_coordinated_idle_reaper_pass(db_path)
        finally:
            with _IDLE_REAPER_THREADS_LOCK:
                _IDLE_REAPER_DATABASES.discard(db_path)

    try:
        threading.Thread(
            target=run,
            name=f"egg-session-reaper-{hashlib.sha256(db_path.encode()).hexdigest()[:8]}",
            daemon=True,
        ).start()
    except Exception:
        with _IDLE_REAPER_THREADS_LOCK:
            _IDLE_REAPER_DATABASES.discard(db_path)
        return False
    return True


def ensure_thread_session_for_repl(
    db: ThreadsDB,
    runtime_thread_id: str,
    *,
    language: str,
    reason: str,
) -> SessionConfig:
    """Return an enabled session config, auto-creating one when allowed.

    Explicit ``session.config`` events always win.  If none is enabled, REPL
    tools can auto-create a runtime-local session using environment defaults:

    - ``EGG_RLM_AUTO_SESSION`` (default: on)
    - ``EGG_RLM_SESSION_PROVIDER`` (default: docker)
    - ``EGG_RLM_SESSION_IMAGE`` (default: egg-rlm-session)
    """

    cfg = get_thread_session_config(db, runtime_thread_id)
    if cfg.enabled:
        return cfg
    if not _env_bool("EGG_RLM_AUTO_SESSION", True):
        return cfg
    provider = _clean_runtime_part(os.environ.get("EGG_RLM_SESSION_PROVIDER"), "docker")
    image = _clean_runtime_part(os.environ.get("EGG_RLM_SESSION_IMAGE"), "egg-rlm-session")
    workspace = _clean_runtime_part(os.environ.get("EGG_RLM_SESSION_WORKSPACE"), "/workspace")
    network = _clean_runtime_part(os.environ.get("EGG_RLM_SESSION_NETWORK"), "none")
    set_thread_session_config(
        db,
        runtime_thread_id,
        enabled=True,
        provider=provider,
        image=image,
        share="private",
        owner_thread_id=runtime_thread_id,
        workspace=workspace,
        network=network,
        share_repl=_env_bool("EGG_RLM_SHARE_REPL", False),
        reason=f"auto:{reason}:{language}",
    )
    append_session_lifecycle_event(
        db,
        runtime_thread_id,
        action="auto_created",
        session_id=get_thread_session_config(db, runtime_thread_id).session_id,
        payload={"provider": provider, "image": image, "language": language, "reason": reason},
    )
    return get_thread_session_config(db, runtime_thread_id)


def append_session_lifecycle_event(
    db: ThreadsDB,
    thread_id: str,
    *,
    action: str,
    session_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a session.lifecycle event for audit/debugging."""

    body: Dict[str, Any] = dict(payload or {})
    body["action"] = action
    if session_id:
        body["session_id"] = session_id
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_="session.lifecycle",
        msg_id=None,
        invoke_id=None,
        payload=body,
    )


def _session_status_for_config(
    db: ThreadsDB,
    thread_id: str,
    cfg: SessionConfig,
) -> SessionStatus:
    if not cfg.enabled:
        return SessionStatus(
            False, cfg.provider, cfg.session_id, "disabled",
            "Session is disabled", share_repl=cfg.share_repl,
        )
    provider = get_session_provider(cfg.provider)
    if provider is not None:
        return provider.status(db, thread_id, cfg)
    return SessionStatus(
        True, cfg.provider, cfg.session_id, "unavailable",
        f"Unknown session provider: {cfg.provider}", share_repl=cfg.share_repl,
    )


def _stop_captured_session(
    db: ThreadsDB,
    thread_id: str,
    cfg: SessionConfig,
    *,
    reason: str,
) -> SessionStatus:
    """Stop exactly ``cfg`` without re-resolving another session identity."""

    provider = get_session_provider(cfg.provider)
    if provider is None:
        return SessionStatus(
            True, cfg.provider, cfg.session_id, "unavailable",
            f"Unknown session provider: {cfg.provider}", share_repl=cfg.share_repl,
        )
    if cfg.provider == "docker" and cfg.session_id:
        _invalidate_python_runtime_refresh_cache(_session_runtime_dir(cfg.session_id))
    return provider.stop(db, thread_id, cfg, reason=reason)


def get_thread_session_status(db: ThreadsDB, thread_id: str) -> SessionStatus:
    """Return provider and runtime health for the effective session config."""

    return _session_status_for_config(db, thread_id, get_thread_session_config(db, thread_id))


def get_or_start_docker_session(db: ThreadsDB, thread_id: str) -> SessionStatus:
    """Start, reattach, or repair the configured Docker session."""

    while True:
        cfg = get_thread_session_config(db, thread_id)
        if not cfg.enabled or cfg.provider != "docker" or not cfg.session_id:
            return _session_status_for_config(db, thread_id, cfg)
        with _session_activity_guard(cfg.session_id):
            current = get_thread_session_config(db, thread_id)
            if _session_config_identity(current) != _session_config_identity(cfg):
                continue
            return _get_or_start_docker_session_locked(db, thread_id, cfg)


def _get_or_start_docker_session_locked(
    db: ThreadsDB,
    thread_id: str,
    cfg: SessionConfig,
) -> SessionStatus:
    status = _session_status_for_config(db, thread_id, cfg)
    if status.status == "unhealthy" and status.reason == "docker_unavailable":
        append_session_lifecycle_event(
            db,
            thread_id,
            action="docker_unavailable",
            session_id=cfg.session_id,
            payload={"message": status.message, "reason": status.reason},
        )
        return status
    if status.status not in {"missing", "stopped", "ready", "busy", "unhealthy"} or not status.container_name:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="docker_error",
            session_id=cfg.session_id,
            payload={"message": status.message, "reason": status.reason or status.status},
        )
        return status

    bridge_dir = _session_bridge_dir(cfg.session_id)
    runtime_dir = _session_runtime_dir(cfg.session_id)
    mount_dir = docker_session_mount_dir(db, thread_id, cfg)
    _write_runtime_files(runtime_dir)
    try:
        restarted = _start_docker_container(
            db, thread_id, cfg, status.container_name, bridge_dir, runtime_dir,
            status.status == "unhealthy",
        )
        if restarted:
            _invalidate_python_runtime_refresh_cache(runtime_dir)
            _daemon, health_error = _wait_for_docker_daemon(bridge_dir)
            if health_error:
                raise RuntimeError(health_error)
        action = "reattached" if status.status == "stopped" else ("docker_restarted" if status.status == "unhealthy" else "docker_started")
        if not restarted:
            return _session_status_for_config(db, thread_id, cfg)
    except Exception as e:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="docker_error",
            session_id=cfg.session_id,
            payload={
                "container_name": status.container_name,
                "error": str(e),
                "reason": status.reason or "start_failed",
                "previous_status": status.status,
            },
        )
        return SessionStatus(
            True, cfg.provider, cfg.session_id, "unhealthy", str(e),
            status.container_name, cfg.share_repl,
            reason=status.reason or "start_failed",
        )

    append_session_lifecycle_event(
        db,
        thread_id,
        action=action,
        session_id=cfg.session_id,
        payload={
            "container_name": status.container_name,
            "image": cfg.image,
            "workspace": cfg.workspace,
            "mount_dir": str(mount_dir),
            "mount_policy": _DOCKER_MOUNT_POLICY,
            "bridge_dir": str(bridge_dir),
            "runtime_dir": str(runtime_dir),
            "reason": (status.reason if status.status == "unhealthy" else ("reattach" if status.status == "stopped" else "start")),
            "previous_status": status.status,
            "previous_reason": status.reason,
        },
    )
    if restarted and _daemon is not None:
        return _session_status_from_daemon(
            cfg,
            status.container_name,
            _daemon,
            reason=(status.reason if status.status == "unhealthy" else ("reattach" if status.status == "stopped" else "start")),
        )
    return _session_status_for_config(db, thread_id, cfg)


def get_or_start_docker_session_handle(db: ThreadsDB, thread_id: str) -> DockerSessionHandle:
    cfg = get_thread_session_config(db, thread_id)
    if not cfg.enabled or cfg.provider != "docker" or not cfg.session_id:
        raise RuntimeError("Docker session is not enabled for this thread")
    status = get_or_start_docker_session(db, thread_id)
    if status.status not in ("ready", "busy") or not status.container_name:
        raise RuntimeError(status.message or f"Docker session not available: {status.status}")
    return DockerSessionHandle(
        session_id=cfg.session_id,
        container_name=status.container_name,
        bridge_dir=str(_session_bridge_dir(cfg.session_id)),
        runtime_dir=str(_session_runtime_dir(cfg.session_id)),
        mount_dir=str(docker_session_mount_dir(db, thread_id, cfg)),
        workspace=cfg.workspace,
    )


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.urandom(4).hex()}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _service_tool_requests(
    bridge_dir: Path,
    *,
    host_owner_id: str = "",
    eval_request_id: str = "",
) -> None:
    from .repl_bridge import call_tool

    for req_path in sorted(bridge_dir.glob("tool_*.req.json")):
        claimed = req_path.with_suffix(req_path.suffix + ".host")
        try:
            os.replace(req_path, claimed)
        except Exception:
            continue
        req_id = req_path.name[len("tool_"):-len(".req.json")]
        res_path = bridge_dir / f"tool_{req_id}.res.json"
        try:
            payload = json.loads(claimed.read_text(encoding="utf-8"))
            request_owner = str(payload.get("host_owner_id") or "")
            request_eval = str(payload.get("eval_request_id") or "")
            protocol_version = int(payload.get("protocol_version") or 1)
            if protocol_version >= 2:
                owned = bool(
                    host_owner_id
                    and eval_request_id
                    and request_owner == host_owner_id
                    and request_eval == eval_request_id
                )
            else:
                owned = not (
                    (request_owner and request_owner != host_owner_id)
                    or (request_eval and request_eval != eval_request_id)
                )
            if not owned:
                os.replace(claimed, req_path)
                continue
            result = call_tool(
                str(payload.get("token") or ""),
                str(payload.get("name") or ""),
                payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                timeout_sec=payload.get("timeout_sec"),
            )
            _atomic_write_json(res_path, {"ok": True, "result": result})
        except Exception as e:
            _atomic_write_json(res_path, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        finally:
            try:
                claimed.unlink()
            except Exception:
                pass


_DOCKER_EVAL_POLL_SEC = 0.05
_DOCKER_CANCEL_ACK_SEC = 2.0
_DOCKER_EVAL_PROTOCOL_VERSION = 2
_DOCKER_HOST_OWNER_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
_DOCKER_HEARTBEAT_STALE_SEC = 5.0
_DOCKER_STOP_TIMEOUT_SEC = 20.0
_DOCKER_KILL_TIMEOUT_SEC = 10.0
_DOCKER_STOP_VERIFY_SEC = 2.0


def _docker_daemon_generation(bridge_dir: Path) -> Optional[str]:
    try:
        payload = json.loads((bridge_dir / "sessiond_generation.json").read_text(encoding="utf-8"))
        value = str(payload.get("daemon_generation") or "").strip()
        return value or None
    except Exception:
        return None


def _valid_daemon_timestamp(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _docker_daemon_status(bridge_dir: Path) -> tuple[Optional[Dict[str, Any]], str]:
    """Read and strictly validate the daemon heartbeat/status authority."""

    path = bridge_dir / "sessiond_status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "Docker session daemon heartbeat is missing"
    except Exception as e:
        return None, f"Docker session daemon status is unreadable: {type(e).__name__}: {e}"
    if not isinstance(payload, dict):
        return None, "Docker session daemon status is not a JSON object"
    generation = payload.get("daemon_generation")
    heartbeat = payload.get("heartbeat_at")
    last_activity = payload.get("last_activity_at")
    if not isinstance(generation, str) or not generation.strip():
        return payload, "Docker session daemon generation is invalid"
    if not _valid_daemon_timestamp(heartbeat):
        return payload, "Docker session daemon heartbeat timestamp is invalid"
    if not _valid_daemon_timestamp(last_activity):
        return payload, "Docker session daemon last activity timestamp is invalid"
    if float(last_activity) > float(heartbeat) + _DOCKER_HEARTBEAT_STALE_SEC:
        return payload, "Docker session daemon last activity is ahead of its heartbeat"
    generation_path = bridge_dir / "sessiond_generation.json"
    try:
        generation_record = json.loads(generation_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return payload, "Docker session daemon generation record is missing"
    except Exception as e:
        return payload, f"Docker session daemon generation record is unreadable: {type(e).__name__}: {e}"
    if not isinstance(generation_record, dict):
        return payload, "Docker session daemon generation record is invalid"
    announced_generation = generation_record.get("daemon_generation")
    if not isinstance(announced_generation, str) or not announced_generation.strip():
        return payload, "Docker session daemon generation record is invalid"
    if generation.strip() != announced_generation.strip():
        return payload, "Docker session daemon generation does not match its heartbeat"

    active_requests = payload.get("active_requests")
    if not isinstance(active_requests, list):
        return payload, "Docker session daemon active request status is invalid"
    for request in active_requests:
        if not isinstance(request, dict):
            return payload, "Docker session daemon active request entry is invalid"
        if not isinstance(request.get("request_id"), str) or not request["request_id"].strip():
            return payload, "Docker session daemon active request ID is invalid"
        if request.get("state") not in {"queued", "running"}:
            return payload, "Docker session daemon active request state is invalid"
        if not isinstance(request.get("language"), str) or not request["language"].strip():
            return payload, "Docker session daemon active request language is invalid"
        if not isinstance(request.get("channel"), str) or not request["channel"].strip():
            return payload, "Docker session daemon active request channel is invalid"
        created_at = request.get("created_at")
        if created_at is not None and not _valid_daemon_timestamp(created_at):
            return payload, "Docker session daemon active request timestamp is invalid"
        cancel_reason = request.get("cancel_reason")
        if cancel_reason is not None and not isinstance(cancel_reason, str):
            return payload, "Docker session daemon active request cancellation is invalid"

    channel_state = payload.get("channel_state")
    if not isinstance(channel_state, dict):
        return payload, "Docker session daemon channel status is invalid"
    for channel, details in channel_state.items():
        if not isinstance(channel, str) or not channel.strip() or not isinstance(details, dict):
            return payload, "Docker session daemon channel entry is invalid"
        if details.get("state") not in {"ready", "busy"}:
            return payload, "Docker session daemon channel state is invalid"
        if details.get("state") == "busy":
            running_id = details.get("running_request_id")
            queued_ids = details.get("queued_request_ids")
            if running_id is not None and (not isinstance(running_id, str) or not running_id.strip()):
                return payload, "Docker session daemon running request ID is invalid"
            if not isinstance(queued_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in queued_ids
            ):
                return payload, "Docker session daemon queued request IDs are invalid"

    age = max(0.0, time.time() - float(heartbeat))
    if age > _DOCKER_HEARTBEAT_STALE_SEC:
        return payload, f"Docker session daemon heartbeat is stale ({age:.1f}s old)"
    return payload, ""


def _wait_for_docker_daemon(bridge_dir: Path, timeout_sec: float = 5.0) -> tuple[Optional[Dict[str, Any]], str]:
    deadline = time.monotonic() + timeout_sec
    status, error = _docker_daemon_status(bridge_dir)
    while error and time.monotonic() < deadline:
        time.sleep(_DOCKER_EVAL_POLL_SEC)
        status, error = _docker_daemon_status(bridge_dir)
    return status, error


def _docker_eval_cleanup(bridge_dir: Path, req_id: str) -> None:
    for suffix in ("req.json", "req.json.processing", "cancel.json", "cancel.ack.json", "res.json"):
        try:
            (bridge_dir / f"eval_{req_id}.{suffix}").unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _docker_cancel_eval(
    bridge_dir: Path,
    req_id: str,
    *,
    reason: str,
) -> bool:
    """Request channel cancellation and report whether sessiond acknowledged."""

    cancel_path = bridge_dir / f"eval_{req_id}.cancel.json"
    ack_path = bridge_dir / f"eval_{req_id}.cancel.ack.json"
    _atomic_write_json(cancel_path, {
        "protocol_version": _DOCKER_EVAL_PROTOCOL_VERSION,
        "request_id": req_id,
        "host_owner_id": _DOCKER_HOST_OWNER_ID,
        "reason": reason,
        "requested_at": time.time(),
    })
    deadline = time.monotonic() + _DOCKER_CANCEL_ACK_SEC
    res_path = bridge_dir / f"eval_{req_id}.res.json"
    while time.monotonic() < deadline:
        # A terminal response can win the host timeout/cancellation race before
        # sessiond observes the cancel file. Treat it as responsive.
        if res_path.exists():
            return True
        if ack_path.exists():
            try:
                ack = json.loads(ack_path.read_text(encoding="utf-8"))
                if (
                    str(ack.get("request_id") or "") == req_id
                    and str(ack.get("state") or "") in {"accepted", "already_finished"}
                ):
                    return True
            except Exception:
                pass
            finally:
                try:
                    ack_path.unlink()
                except Exception:
                    pass
        time.sleep(_DOCKER_EVAL_POLL_SEC)
    return False


def _record_docker_eval_lifecycle(
    db: ThreadsDB,
    runtime_thread_id: str,
    *,
    action: str,
    reason: str,
    req_id: str,
    payload: Dict[str, Any],
    daemon_generation: Optional[str],
    container_stopped: bool = False,
) -> None:
    try:
        cfg = get_thread_session_config(db, runtime_thread_id)
        append_session_lifecycle_event(
            db,
            runtime_thread_id,
            action=action,
            session_id=cfg.session_id,
            payload={
                "provider": "docker",
                "reason": reason,
                "request_id": req_id,
                "language": str(payload.get("language") or ""),
                "channel": str(payload.get("channel") or payload.get("repl_name") or "default"),
                "daemon_generation": daemon_generation,
                "container_stopped": container_stopped,
            },
        )
    except Exception:
        # Lifecycle diagnostics must never mask the eval's terminal result.
        pass


def _run_docker_eval_request(
    db: ThreadsDB,
    runtime_thread_id: str,
    bridge_dir: Path,
    payload: Dict[str, Any],
    timeout_sec: Optional[float],
    cancel_check: Any = None,
) -> str:
    req_id = uuid.uuid4().hex
    req_path = bridge_dir / f"eval_{req_id}.req.json"
    res_path = bridge_dir / f"eval_{req_id}.res.json"
    daemon_generation = _docker_daemon_generation(bridge_dir)
    payload = {
        **dict(payload),
        "protocol_version": _DOCKER_EVAL_PROTOCOL_VERSION,
        "id": req_id,
        "request_id": req_id,
        "channel": str(payload.get("channel") or payload.get("repl_name") or "default"),
        "host_owner_id": _DOCKER_HOST_OWNER_ID,
        "daemon_generation": daemon_generation,
        "created_at": time.time(),
        "timeout_sec": timeout_sec,
        "deadline_duration_sec": timeout_sec,
    }
    _atomic_write_json(req_path, payload)
    started = time.monotonic()
    cancel_reason: Optional[str] = None
    try:
        while True:
            _service_tool_requests(
                bridge_dir,
                host_owner_id=_DOCKER_HOST_OWNER_ID,
                eval_request_id=req_id,
            )
            if res_path.exists():
                response = json.loads(res_path.read_text(encoding="utf-8"))
                if str(response.get("request_id") or req_id) != req_id:
                    continue
                response_reason = str(response.get("reason") or "")
                if response_reason in {"timeout", "cancelled", "daemon_restarted"}:
                    _record_docker_eval_lifecycle(
                        db, runtime_thread_id,
                        action="docker_eval_terminal",
                        reason=("interrupted" if response_reason == "cancelled" else response_reason),
                        req_id=req_id,
                        payload=payload,
                        daemon_generation=str(response.get("daemon_generation") or daemon_generation or "") or None,
                    )
                if response.get("ok"):
                    return str(response.get("output") or "")
                return f"Error: Docker REPL failed: {response.get('error') or 'unknown error'}"
            if cancel_check is not None and cancel_check():
                cancel_reason = "cancelled"
            elif timeout_sec is not None and (time.monotonic() - started) >= float(timeout_sec):
                cancel_reason = "timeout"
            if cancel_reason is not None:
                lifecycle_reason = "interrupted" if cancel_reason == "cancelled" else cancel_reason
                acknowledged = _docker_cancel_eval(bridge_dir, req_id, reason=lifecycle_reason)
                terminal_deadline = time.monotonic() + _DOCKER_CANCEL_ACK_SEC
                while acknowledged and time.monotonic() < terminal_deadline:
                    if res_path.exists():
                        response = json.loads(res_path.read_text(encoding="utf-8"))
                        response_reason = str(response.get("reason") or cancel_reason)
                        _record_docker_eval_lifecycle(
                            db, runtime_thread_id,
                            action="docker_eval_terminal",
                            reason=("interrupted" if response_reason == "cancelled" else response_reason),
                            req_id=req_id,
                            payload=payload,
                            daemon_generation=str(response.get("daemon_generation") or daemon_generation or "") or None,
                        )
                        return str(response.get("output") or "--- INTERRUPTED ---\nDocker REPL eval cancelled.")
                    time.sleep(_DOCKER_EVAL_POLL_SEC)
                container_stopped = False
                if not acknowledged:
                    status = stop_thread_session(
                        db,
                        runtime_thread_id,
                        reason=f"docker_eval_cancel_unresponsive:{lifecycle_reason}",
                    )
                    container_stopped = getattr(status, "status", None) == "stopped"
                _record_docker_eval_lifecycle(
                    db, runtime_thread_id,
                    action="docker_eval_terminal",
                    reason=(f"{lifecycle_reason}_session_stopped" if container_stopped else lifecycle_reason),
                    req_id=req_id,
                    payload=payload,
                    daemon_generation=daemon_generation,
                    container_stopped=container_stopped,
                )
                if cancel_reason == "timeout":
                    suffix = " The unresponsive session container was stopped." if container_stopped else ""
                    return f"--- TIMEOUT ---\nDocker REPL eval timed out and its channel was reset.{suffix}"
                suffix = " The unresponsive session container was stopped." if container_stopped else ""
                return f"--- INTERRUPTED ---\nDocker REPL eval was cancelled and its channel was reset.{suffix}"
            time.sleep(_DOCKER_EVAL_POLL_SEC)
    finally:
        _docker_eval_cleanup(bridge_dir, req_id)


def _run_docker_python_eval_request(
    db: ThreadsDB,
    runtime_thread_id: str,
    bridge_dir: Path,
    payload: Dict[str, Any],
    timeout_sec: Optional[float],
    cancel_check: Any = None,
) -> str:
    return _run_docker_eval_request(
        db, runtime_thread_id, bridge_dir, payload, timeout_sec, cancel_check,
    )


def _execute_python_docker(
    db: ThreadsDB,
    runtime_thread_id: str,
    code: str,
    *,
    repl_name: str,
    eval_token: str,
    timeout_sec: Optional[float],
    cancel_check: Any = None,
) -> str:
    while True:
        cfg = get_thread_session_config(db, runtime_thread_id)
        if not cfg.enabled or cfg.provider != "docker" or not cfg.session_id:
            raise RuntimeError("Docker session is not enabled for this thread")
        with _session_activity_guard(cfg.session_id):
            current = get_thread_session_config(db, runtime_thread_id)
            if _session_config_identity(current) != _session_config_identity(cfg):
                continue
            return _execute_python_docker_captured(
                db, runtime_thread_id, cfg, code,
                repl_name=repl_name, eval_token=eval_token,
                timeout_sec=timeout_sec, cancel_check=cancel_check,
            )


def _execute_python_docker_captured(
    db: ThreadsDB,
    runtime_thread_id: str,
    cfg: SessionConfig,
    code: str,
    *,
    repl_name: str,
    eval_token: str,
    timeout_sec: Optional[float],
    cancel_check: Any = None,
) -> str:
    status = _get_or_start_docker_session_locked(db, runtime_thread_id, cfg)
    if status.status not in ("ready", "busy") or not status.container_name:
        raise RuntimeError(status.message or f"Docker session not available: {status.status}")
    handle = DockerSessionHandle(
        session_id=cfg.session_id or "",
        container_name=status.container_name,
        bridge_dir=str(_session_bridge_dir(cfg.session_id or "")),
        runtime_dir=str(_session_runtime_dir(cfg.session_id or "")),
        mount_dir=str(docker_session_mount_dir(db, runtime_thread_id, cfg)),
        workspace=cfg.workspace,
    )
    runtime_hash = _python_repl_runtime_code_hash(Path(handle.runtime_dir))
    bridge_dir = Path(handle.bridge_dir)
    refresh_key = (handle.runtime_dir, repl_name, runtime_hash)
    if refresh_key not in _DOCKER_REFRESHED_PYTHON_RUNTIMES:
        refresh_output = _run_docker_python_eval_request(
            db,
            runtime_thread_id,
            bridge_dir,
            {
                "language": "python",
                "code": _python_repl_runtime_refresh_code(runtime_hash),
                "repl_name": repl_name,
                "token": eval_token,
                "timeout_sec": timeout_sec,
            },
            timeout_sec,
            cancel_check,
        )
        refresh_failed = (
            "Traceback (most recent call last):" in refresh_output
            or refresh_output.startswith(("Error:", "--- TIMEOUT ---"))
        )
        if refresh_failed:
            return "Error: Egg could not refresh the persistent Python REPL runtime code.\n" + refresh_output
        _DOCKER_REFRESHED_PYTHON_RUNTIMES.add(refresh_key)

    payload = {
        "language": "python",
        "code": code,
        "repl_name": repl_name,
        "token": eval_token,
        "timeout_sec": timeout_sec,
    }
    try:
        from .repl_bridge import resolve_eval_context

        eval_ctx = resolve_eval_context(eval_token)
        context = _load_repl_thread_context(str(db.path), eval_ctx.caller_thread_id)
        files = context.get("context_files") if isinstance(context.get("context_files"), dict) else None
        if isinstance(files, dict):
            container_files: Dict[str, str] = {}
            for key, value in files.items():
                mapped = None
                if isinstance(value, str):
                    mapped = _container_workspace_path(Path(value), Path(handle.mount_dir), handle.workspace)
                container_files[str(key)] = mapped or str(value)
            context = dict(context)
            context["context_files"] = container_files
        context_json = json.dumps(context, ensure_ascii=False, sort_keys=True)
        payload["thread_context_json"] = context_json
    except Exception:
        pass
    return _run_docker_python_eval_request(
        db,
        runtime_thread_id,
        bridge_dir,
        payload,
        timeout_sec,
        cancel_check,
    )


def _execute_bash_docker(
    db: ThreadsDB,
    runtime_thread_id: str,
    script: str,
    *,
    repl_name: str,
    eval_token: str,
    timeout_sec: Optional[float],
    cancel_check: Any = None,
) -> str:
    while True:
        cfg = get_thread_session_config(db, runtime_thread_id)
        if not cfg.enabled or cfg.provider != "docker" or not cfg.session_id:
            raise RuntimeError("Docker session is not enabled for this thread")
        with _session_activity_guard(cfg.session_id):
            current = get_thread_session_config(db, runtime_thread_id)
            if _session_config_identity(current) != _session_config_identity(cfg):
                continue
            return _execute_bash_docker_captured(
                db, runtime_thread_id, cfg, script,
                repl_name=repl_name, eval_token=eval_token,
                timeout_sec=timeout_sec, cancel_check=cancel_check,
            )


def _execute_bash_docker_captured(
    db: ThreadsDB,
    runtime_thread_id: str,
    cfg: SessionConfig,
    script: str,
    *,
    repl_name: str,
    eval_token: str,
    timeout_sec: Optional[float],
    cancel_check: Any = None,
) -> str:
    status = _get_or_start_docker_session_locked(db, runtime_thread_id, cfg)
    if status.status not in ("ready", "busy") or not status.container_name:
        raise RuntimeError(status.message or f"Docker session not available: {status.status}")
    return _run_docker_eval_request(
        db,
        runtime_thread_id,
        _session_bridge_dir(cfg.session_id or ""),
        {
            "language": "bash",
            "script": script,
            "repl_name": repl_name,
            "channel": repl_name,
            "token": eval_token,
        },
        timeout_sec,
        cancel_check,
    )


# ---------------------------------------------------------------------------
# In-memory Python provider (test/development only)
# ---------------------------------------------------------------------------

_MEMORY_PYTHON_REPLS: Dict[tuple[str, str], Dict[str, Any]] = {}
_MEMORY_BASH_ENVS: Dict[tuple[str, str], Dict[str, str]] = {}


def _memory_provider_allowed_under_sandbox(db: ThreadsDB, runtime_thread_id: str) -> tuple[bool, str]:
    """Return whether the unsafe host-memory provider may run now."""

    try:
        if str(getattr(db, "path", "")) == ":memory:":
            return True, "in-memory test database"
    except Exception:
        pass
    try:
        raw = os.environ.get("EGG_ALLOW_MEMORY_SESSION_WITH_SANDBOX")
        if raw is not None and str(raw).strip().lower() in ("1", "true", "yes", "on"):
            return True, "explicit unsafe override"
    except Exception:
        pass
    try:
        from .sandbox import get_thread_sandbox_config

        sb = get_thread_sandbox_config(db, runtime_thread_id)
        if bool(sb.enabled):
            return False, (
                "Error: memory REPL sessions execute in the Egg host process, but sandboxing is turned on. "
                "Use provider='docker' or turn sandboxing off if you are sure host execution is intended."
            )
    except Exception as e:
        return False, f"Error: could not verify sandbox policy before using memory REPL provider: {e}"
    return True, "sandboxing disabled"


def _coerce_positive_timeout(value: Any) -> Optional[float]:
    """Return a positive timeout seconds value, otherwise ``None``.

    ``timeout_sec`` is the only public name for REPL eval limits. Invalid or
    non-positive values mean "fall back to the next configured source". A
    caller that wants no timeout should set the runner/global tool timeout to 0
    rather than passing 0 as a per-call override.
    """

    if value is None:
        return None
    try:
        timeout = float(value)
    except Exception:
        return None
    return timeout if timeout > 0 else None



def _safe_repl_context_part(value: Any, default: str = "thread") -> str:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in str(value or ''))
    safe = safe.strip('-_')
    return safe or default


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.urandom(4).hex()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _repl_context_cache_dir(db: ThreadsDB, thread_id: str) -> Path:
    # Keep regenerated transcript caches in the thread working directory so
    # Python REPL code can open/grep/read them.  This remains only a cache; the
    # event DB is the source of truth and hydration rewrites the files.
    try:
        from .api import _ensure_thread_working_directory

        base = _ensure_thread_working_directory(db, thread_id)
    except Exception:
        base = Path(db.path).parent
    return Path(base) / ".egg_thread_context" / _safe_repl_context_part(thread_id)


def _message_text_for_context_file(value: Any) -> str:
    try:
        from .content_parts import content_to_plain_text

        return content_to_plain_text(value)
    except Exception:
        pass
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _format_context_message_for_markdown(message: Dict[str, Any]) -> List[str]:
    role = message.get("role") or "message"
    msg_id = message.get("msg_id") or "(no msg_id)"
    seq = message.get("event_seq")
    title = f"### {role} {msg_id}"
    if seq is not None:
        title += f" (event_seq={seq})"
    lines = [title, ""]
    content = _message_text_for_context_file(message.get("content", ""))
    lines.append(content)
    lines.append("")
    return lines


def _write_repl_thread_context_files(db: ThreadsDB, thread_id: str, context: Dict[str, Any]) -> Dict[str, str]:
    """Write regenerated JSONL/Markdown REPL context caches.

    These files deliberately contain the already-built consumer-facing context,
    not raw event-log data, so the same hidden/no_api and tool-output filtering
    used by ``build_repl_thread_context`` applies to file-backed workflows.
    """

    cache_dir = _repl_context_cache_dir(db, thread_id)
    jsonl_path = cache_dir / "thread_context.jsonl"
    markdown_path = cache_dir / "thread_context.md"

    records: List[Dict[str, Any]] = []
    thread_meta = context.get("thread") if isinstance(context.get("thread"), dict) else {}
    records.append({"type": "thread", **dict(thread_meta)})
    for compaction in context.get("compactions", []) if isinstance(context.get("compactions"), list) else []:
        if isinstance(compaction, dict):
            records.append({"type": "compaction", **compaction})
    for message in context.get("all_messages", []) if isinstance(context.get("all_messages"), list) else []:
        if isinstance(message, dict):
            records.append({"type": "message", **message})
    jsonl_text = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)

    lines: List[str] = ["# Thread context", ""]
    lines.append(f"Thread: `{thread_id}`")
    loaded_seq = thread_meta.get("loaded_through_event_seq") if isinstance(thread_meta, dict) else None
    if loaded_seq is not None:
        lines.append(f"Loaded through event seq: `{loaded_seq}`")
    note = thread_meta.get("visibility_note") if isinstance(thread_meta, dict) else None
    if note:
        lines.extend(["", f"> {note}"])
    how_to_use = context.get("how_to_use")
    if how_to_use:
        lines.extend(["", "## How to use", "", str(how_to_use)])
    compactions = context.get("compactions") if isinstance(context.get("compactions"), list) else []
    if compactions:
        lines.extend(["", "## Compactions", ""])
        for item in compactions:
            if not isinstance(item, dict):
                continue
            current = " current" if item.get("is_current") else ""
            lines.append(
                f"- marker_event_seq={item.get('marker_event_seq')} starts_at={item.get('current_prompt_starts_at_msg_id')}"
                f" selector={item.get('selector_used')} created_by={item.get('created_by')}{current}"
            )
    sections = [
        ("Current prompt messages", context.get("current_prompt_messages")),
        ("Older messages not in prompt", context.get("older_messages_not_in_prompt")),
        ("All usable messages", context.get("all_messages")),
    ]
    for title, messages in sections:
        if not isinstance(messages, list):
            continue
        lines.extend(["", f"## {title}", ""])
        if not messages:
            lines.append("(none)")
            continue
        for message in messages:
            if isinstance(message, dict):
                lines.extend(_format_context_message_for_markdown(message))
    markdown_text = "\n".join(lines).rstrip() + "\n"

    _atomic_write_text(jsonl_path, jsonl_text)
    _atomic_write_text(markdown_path, markdown_text)
    files = {
        "jsonl_path": str(jsonl_path),
        "markdown_path": str(markdown_path),
    }
    context["context_files"] = files
    return files


def _message_search_blob(message: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("msg_id", "role", "name", "tool_call_id"):
        value = message.get(key)
        if value is not None:
            parts.append(str(value))
    parts.append(_message_text_for_context_file(message.get("content", "")))
    return "\n".join(parts).lower()


def _coerce_context_seq(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _load_repl_thread_context(db_path: str, thread_id: str) -> Dict[str, Any]:
    from .api import build_repl_thread_context

    db = ThreadsDB(db_path)
    context = build_repl_thread_context(db, thread_id)
    try:
        _write_repl_thread_context_files(db, thread_id, context)
    except Exception as e:
        # File-backed context is a convenience cache.  Keep REPL hydration
        # usable even if the filesystem is read-only or otherwise unavailable.
        context.setdefault("context_files", {})
        if isinstance(context.get("context_files"), dict):
            context["context_files"].setdefault("error", f"{type(e).__name__}: {e}")
    return context


def _install_repl_thread_context(globs: Dict[str, Any], context: Dict[str, Any], *, db_path: str, thread_id: str) -> None:
    """Install thread-context variables and helpers into a Python REPL namespace."""

    def reload_thread_context() -> Dict[str, Any]:
        fresh = _load_repl_thread_context(db_path, thread_id)
        _install_repl_thread_context(globs, fresh, db_path=db_path, thread_id=thread_id)
        return fresh

    def _current_context() -> Dict[str, Any]:
        current = globs.get("thread_context")
        if isinstance(current, dict):
            return current
        return reload_thread_context()

    def search_thread(query: Any, role: Any = None, in_prompt: Any = None) -> List[Dict[str, Any]]:
        """Search hydrated thread messages by text, optionally filtering role/prompt membership."""

        ctx = _current_context()
        if in_prompt is True:
            messages = ctx.get("current_prompt_messages", [])
        elif in_prompt is False:
            messages = ctx.get("older_messages_not_in_prompt", [])
        else:
            messages = ctx.get("all_messages", [])
        if not isinstance(messages, list):
            messages = []
        query_text = str(query or "").lower()
        role_filter: Optional[set[str]] = None
        if role is not None:
            if isinstance(role, (list, tuple, set)):
                role_filter = {str(item) for item in role}
            else:
                role_filter = {str(role)}
        out: List[Dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if role_filter is not None and str(message.get("role")) not in role_filter:
                continue
            if query_text and query_text not in _message_search_blob(message):
                continue
            out.append(message)
        return out

    def get_message(msg_id: Any) -> Optional[Dict[str, Any]]:
        """Return one hydrated message by exact msg_id, or None."""

        ctx = _current_context()
        by_id = ctx.get("messages_by_id") if isinstance(ctx, dict) else None
        if not isinstance(by_id, dict):
            return None
        return by_id.get(str(msg_id))

    def print_message(msg_id: Any) -> None:
        """Print one hydrated message with a compact header and its exact content."""

        message = get_message(msg_id)
        if message is None:
            print(f"Message not found: {msg_id}")
            return None
        header_parts = [str(message.get("role") or "message")]
        if message.get("msg_id") is not None:
            header_parts.append(str(message.get("msg_id")))
        if message.get("event_seq") is not None:
            header_parts.append(f"event_seq={message.get('event_seq')}")
        print("[" + " ".join(header_parts) + "]")
        print(_message_text_for_context_file(message.get("content", "")))
        return None

    globs["thread_context"] = context
    globs["all_messages"] = context.get("all_messages", [])
    globs["current_prompt_messages"] = context.get("current_prompt_messages", [])
    globs["older_messages_not_in_prompt"] = context.get("older_messages_not_in_prompt", [])
    globs["messages_by_id"] = context.get("messages_by_id", {})
    messages_by_role = context.get("messages_by_role", {}) if isinstance(context.get("messages_by_role"), dict) else {}
    globs["messages_by_role"] = messages_by_role
    globs["system_messages"] = messages_by_role.get("system", [])
    globs["user_messages"] = messages_by_role.get("user", [])
    globs["assistant_messages"] = messages_by_role.get("assistant", [])
    globs["tool_messages"] = messages_by_role.get("tool", [])
    globs["compactions"] = context.get("compactions", [])
    globs["context_files"] = context.get("context_files", {})
    globs["search_thread"] = search_thread
    globs["get_message"] = get_message
    globs["print_message"] = print_message
    globs["reload_thread_context"] = reload_thread_context
    loaded_seq = None
    thread_meta = context.get("thread") if isinstance(context.get("thread"), dict) else {}
    if isinstance(thread_meta, dict):
        loaded_seq = _coerce_context_seq(thread_meta.get("loaded_through_event_seq"))
    globs["_egg_thread_context_meta"] = {
        "db_path": str(db_path),
        "thread_id": str(thread_id),
        "loaded_through_event_seq": loaded_seq,
    }


def _hydrate_python_repl_thread_context(globs: Dict[str, Any], eval_token: Optional[str]) -> None:
    """Refresh the Python REPL's thread_context variables when needed."""

    if not eval_token:
        return
    try:
        from .repl_bridge import resolve_eval_context

        eval_ctx = resolve_eval_context(eval_token)
        db_path = str(eval_ctx.db_path)
        thread_id = str(eval_ctx.caller_thread_id)
        db = ThreadsDB(db_path)
        current_seq = db.max_event_seq(thread_id)
        meta = globs.get("_egg_thread_context_meta") if isinstance(globs.get("_egg_thread_context_meta"), dict) else {}
        existing = globs.get("thread_context")
        loaded_seq = _coerce_context_seq(meta.get("loaded_through_event_seq")) if isinstance(meta, dict) else None
        existing_thread_id = meta.get("thread_id") if isinstance(meta, dict) else None
        existing_db_path = meta.get("db_path") if isinstance(meta, dict) else None
        needs_rebuild = (
            not isinstance(existing, dict)
            or str(existing_thread_id or "") != thread_id
            or str(existing_db_path or "") != db_path
            or loaded_seq != int(current_seq)
        )
        if needs_rebuild:
            context = _load_repl_thread_context(db_path, thread_id)
        else:
            context = existing
            if not isinstance(context.get("context_files"), dict) or not context.get("context_files"):
                try:
                    _write_repl_thread_context_files(db, thread_id, context)
                except Exception:
                    pass
        _install_repl_thread_context(globs, context, db_path=db_path, thread_id=thread_id)
        globs.pop("_egg_thread_context_error", None)
    except Exception as e:
        # Hydration should not make the REPL itself unusable.  Keep a compact
        # diagnostic in the namespace for explicit debugging without printing it
        # into every eval result.
        globs["_egg_thread_context_error"] = f"{type(e).__name__}: {e}"


def _append_runtime_repl_message(
    db: ThreadsDB,
    runtime_thread_id: str,
    role: str,
    content: str,
    *,
    language: str,
    repl_name: str,
    repl_channel: str,
    session_id: Optional[str],
    caller_thread_id: str,
) -> None:
    """Append a hidden/audit message to a runtime thread for REPL evals."""

    try:
        from .api import append_message

        append_message(
            db,
            runtime_thread_id,
            role,
            content,
            extra={
                "no_api": True,
                "keep_user_turn": True,
                "origin": "repl_eval",
                "runtime": True,
                "language": language,
                "repl_name": repl_name,
                "repl_channel": repl_channel,
                "session_id": session_id,
                "caller_thread_id": caller_thread_id,
            },
        )
    except Exception:
        # Runtime audit messages should never make REPL execution fail.
        pass


def _make_eggtools_module(eval_token: str):
    """Create an in-memory eggtools module bound to an eval token."""

    import types
    from . import repl_bridge

    mod = types.ModuleType("eggtools")

    def _tool_timeout(args: Dict[str, Any]) -> Optional[float]:
        timeout = _coerce_positive_timeout(args.get("timeout"))
        if timeout is not None:
            args.setdefault("_egg_tool_timeout_sec", timeout)
            return timeout
        timeout_sec = _coerce_positive_timeout(args.get("timeout_sec"))
        if timeout_sec is not None:
            args.setdefault("_egg_tool_timeout_sec", timeout_sec)
            return timeout_sec
        return None

    def tool(tool_name: str, /, **kwargs: Any) -> str:
        args = dict(kwargs)
        return repl_bridge.call_tool(eval_token, tool_name, args, timeout_sec=_tool_timeout(args))

    def _pop_timeout_arg(args: Dict[str, Any]) -> Optional[float]:
        timeout = _coerce_positive_timeout(args.pop("timeout", None))
        if timeout is not None:
            args.pop("timeout_sec", None)
            return timeout
        return _coerce_positive_timeout(args.pop("timeout_sec", None))

    def _install_generated_wrappers() -> None:
        try:
            from .tools import create_default_tools

            try:
                from .session_runtime.tool_wrappers import generate_tool_wrappers_source
            except Exception:
                import importlib.util

                helper_path = Path(__file__).resolve().parent / "session_runtime" / "tool_wrappers.py"
                spec = importlib.util.spec_from_file_location("eggthreads.session_runtime.tool_wrappers", helper_path)
                if spec is None or spec.loader is None:
                    raise
                helper = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(helper)
                generate_tool_wrappers_source = helper.generate_tool_wrappers_source

            specs = [entry["spec"] for entry in create_default_tools()._tools.values()]
            ns: Dict[str, Any] = {"tool": tool, "Any": Any, "__name__": "eggtools._generated"}
            source = generate_tool_wrappers_source(specs)
            exec(compile(source, "<eggtools-generated>", "exec"), ns, ns)
            for generated_name in ns.get("__all__", []):
                if isinstance(generated_name, str) and generated_name and not generated_name.startswith("_"):
                    # Keep hand-written wrappers for tools that need
                    # presentation hints or argument normalization
                    # (spawn_agent*, wait, bash, ...). Generated wrappers fill
                    # in only tools without bespoke behavior.
                    if not hasattr(mod, generated_name):
                        setattr(mod, generated_name, ns[generated_name])
        except Exception:
            pass

    def spawn_agent(context_text: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["context_text"] = context_text
        args.setdefault("_egg_raw_thread_id_result", True)
        return repl_bridge.call_tool(eval_token, "spawn_agent", args, timeout_sec=timeout_sec)

    def spawn_agent_auto(context_text: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["context_text"] = context_text
        args.setdefault("_egg_raw_thread_id_result", True)
        return repl_bridge.call_tool(eval_token, "spawn_agent_auto", args, timeout_sec=timeout_sec)

    def send_message_to_child(child_thread_id: str, message: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["child_thread_id"] = child_thread_id
        args["message"] = message
        return repl_bridge.call_tool(eval_token, "send_message_to_child", args, timeout_sec=timeout_sec)

    def get_child_status(child_thread_ids: Any = None, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        if child_thread_ids is not None:
            if isinstance(child_thread_ids, (str, int)):
                child_thread_ids = [str(child_thread_ids)]
            if isinstance(child_thread_ids, (list, tuple, set)):
                child_thread_ids = [str(t).splitlines()[-1].strip() for t in child_thread_ids if isinstance(t, (str, int))]
            args["child_thread_ids"] = child_thread_ids
        return repl_bridge.call_tool(eval_token, "get_child_status", args, timeout_sec=timeout_sec)

    def wait(thread_ids: Any, **kwargs: Any) -> str:
        if isinstance(thread_ids, (str, int)):
            thread_ids = [str(thread_ids)]
        if isinstance(thread_ids, (list, tuple, set)):
            thread_ids = [str(t).splitlines()[-1].strip() for t in thread_ids if isinstance(t, (str, int))]
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["thread_ids"] = thread_ids
        return repl_bridge.call_tool(eval_token, "wait", args, timeout_sec=timeout_sec)

    def web_search(query: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["query"] = query
        return repl_bridge.call_tool(eval_token, "web_search", args, timeout_sec=timeout_sec)

    def fetch_url(url: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["url"] = url
        return repl_bridge.call_tool(eval_token, "fetch_url", args, timeout_sec=timeout_sec)

    def skill(name: Optional[str] = None, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        if name is not None:
            args["name"] = name
        return repl_bridge.call_tool(eval_token, "skill", args, timeout_sec=timeout_sec)

    def bash(script: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["script"] = script
        return repl_bridge.call_tool(eval_token, "bash", args, timeout_sec=timeout_sec)

    def python(script: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        timeout_sec = _pop_timeout_arg(args)
        args["script"] = script
        return repl_bridge.call_tool(eval_token, "python", args, timeout_sec=timeout_sec)

    _install_generated_wrappers()

    mod.tool = tool
    mod.spawn_agent = spawn_agent
    mod.spawn_agent_auto = spawn_agent_auto
    mod.send_message_to_child = send_message_to_child
    mod.get_child_status = get_child_status
    mod.wait = wait
    mod.web_search = web_search
    mod.fetch_url = fetch_url
    mod.skill = skill
    mod.bash = bash
    mod.python = python
    return mod


def _execute_python_memory(session_id: str, repl_name: str, code: str, *, eval_token: Optional[str] = None) -> str:
    """Execute Python in a persistent in-process namespace.

    This provider exists to let the RLM bridge/runtime-thread semantics be
    tested before Docker is implemented.  It is intentionally only selected
    when a thread's ``session.config`` explicitly sets ``provider='memory'``.
    """

    import ast
    import contextlib
    import io
    import sys
    import traceback

    key = (session_id, repl_name)
    globs = _MEMORY_PYTHON_REPLS.setdefault(key, {"__name__": "__egg_repl__"})
    _hydrate_python_repl_thread_context(globs, eval_token)
    old_eggtools = sys.modules.get("eggtools")
    if eval_token:
        eggtools_mod = _make_eggtools_module(eval_token)
        sys.modules["eggtools"] = eggtools_mod
        globs["eggtools"] = eggtools_mod
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        tree = ast.parse(code or "", mode="exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                body = tree.body[:-1]
                expr = tree.body[-1].value
                if body:
                    exec(compile(ast.Module(body=body, type_ignores=[]), "<egg-python-repl>", "exec"), globs, globs)
                value = eval(compile(ast.Expression(expr), "<egg-python-repl>", "eval"), globs, globs)
                if value is not None:
                    print(repr(value))
            else:
                exec(compile(tree, "<egg-python-repl>", "exec"), globs, globs)
    except Exception:
        traceback.print_exc(file=stderr)
    finally:
        if eval_token:
            if old_eggtools is not None:
                sys.modules["eggtools"] = old_eggtools
            else:
                sys.modules.pop("eggtools", None)

    out = ""
    stdout_text = stdout.getvalue().strip()
    stderr_text = stderr.getvalue().strip()
    if stdout_text:
        out += f"--- STDOUT ---\n{stdout_text}\n"
    if stderr_text:
        out += f"--- STDERR ---\n{stderr_text}\n"
    return out.strip() or "--- The Python REPL executed successfully and produced no output ---"


def _session_provider_stop_message(cfg: SessionConfig) -> str:
    sid = cfg.session_id or "(none)"
    if cfg.provider == "memory":
        return f"Stopped in-memory session {sid}."
    if cfg.provider == "docker":
        return f"Stopped Docker session {sid}."
    return f"Stopped session {sid}."


def stop_thread_session(db: ThreadsDB, thread_id: str, *, reason: str = "user") -> SessionStatus:
    """Stop the captured effective session, never a later replacement identity."""

    while True:
        cfg = get_thread_session_config(db, thread_id)
        if not cfg.enabled or not cfg.session_id:
            append_session_lifecycle_event(
                db,
                thread_id,
                action="stop_ignored",
                session_id=cfg.session_id,
                payload={"reason": reason, "message": "Session is not enabled"},
            )
            return _session_status_for_config(db, thread_id, cfg)
        guard = (
            _session_activity_guard(cfg.session_id)
            if cfg.provider == "docker"
            else nullcontext(True)
        )
        with guard:
            current = get_thread_session_config(db, thread_id)
            if _session_config_identity(current) != _session_config_identity(cfg):
                continue
            return _stop_captured_session(db, thread_id, cfg, reason=reason)


def _reset_thread_session_core(db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> str:
    """Reset a session's mutable state and assign a fresh session id.

    Reset is event-sourced as a stop/lifecycle event followed by a new
    ``session.config`` event with the same provider/image/share policy but a
    new session id.  This preserves auditability and keeps containers out of
    the authority path.
    """

    if cfg.enabled and cfg.session_id:
        stop_thread_session(db, thread_id, reason=f"reset:{reason}")

    old_session_id = cfg.session_id
    new_session_id = _session_id_for_thread(f"{thread_id}{os.urandom(8).hex()}")
    sid = set_thread_session_config(
        db,
        thread_id,
        enabled=True,
        provider=cfg.provider,
        image=cfg.image,
        share=cfg.share,
        session_id=new_session_id,
        owner_thread_id=cfg.owner_thread_id or thread_id,
        workspace=cfg.workspace,
                network=cfg.network,
        share_with_children_default=cfg.share_with_children_default,
        share_repl=cfg.share_repl,
        reason=f"reset:{reason}",
    )
    append_session_lifecycle_event(
        db,
        thread_id,
        action="reset",
        session_id=sid,
        payload={"provider": cfg.provider, "old_session_id": old_session_id, "reason": reason},
    )
    return sid


def reset_thread_session(db: ThreadsDB, thread_id: str, *, reason: str = "user") -> str:
    """Reset a session's mutable state and assign a fresh session id.

    Reset is event-sourced as a stop/lifecycle event followed by a new
    ``session.config`` event with the same provider/image/share policy but a
    new session id.  This preserves auditability and keeps providers out of
    the authority path.
    """

    cfg = get_thread_session_config(db, thread_id)
    if not cfg.enabled:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="reset_ignored",
            session_id=cfg.session_id,
            payload={"provider": cfg.provider, "reason": reason, "message": "Session is not enabled"},
        )
        return ""
    provider = get_session_provider(cfg.provider)
    if provider is not None:
        return provider.reset(db, thread_id, cfg, reason=reason)
    return _reset_thread_session_core(db, thread_id, cfg, reason=reason)


def _execute_bash_memory(session_id: str, repl_name: str, script: str, *, eval_token: Optional[str] = None, timeout_sec: Optional[float] = None) -> str:
    """Small persistent Bash provider for tests/dev using host bash."""

    env_key = (session_id, repl_name)
    persistent = _MEMORY_BASH_ENVS.setdefault(env_key, {})
    env = os.environ.copy()
    env.update(persistent)
    if eval_token:
        env["EGG_EVAL_TOKEN"] = eval_token
    marker = f"__EGG_ENV_{os.urandom(6).hex()}__"
    wrapped = f"{script or ''}\nprintf '\\n{marker}\\n'\nenv\n"
    try:
        proc = subprocess.run(
            ["bash", "-lc", wrapped],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"--- TIMEOUT ---\nBash REPL timed out after {timeout_sec} seconds"
    stdout = proc.stdout or ""
    user_out, _, env_dump = stdout.partition(f"\n{marker}\n")
    for line in env_dump.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k and "\x00" not in k:
            persistent[k] = v
    out = ""
    if user_out.strip():
        out += f"--- STDOUT ---\n{user_out.strip()}\n"
    if proc.stderr and proc.stderr.strip():
        out += f"--- STDERR ---\n{proc.stderr.strip()}\n"
    return out.strip() or "--- The Bash REPL executed successfully and produced no output ---"


class MemorySessionProvider:
    """In-process test/development session provider."""

    name = "memory"

    def available(self) -> bool:
        return True

    def status(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig) -> SessionStatus:
        allowed, reason = _memory_provider_allowed_under_sandbox(db, thread_id)
        if not allowed:
            return SessionStatus(True, cfg.provider, cfg.session_id, "blocked", reason, share_repl=cfg.share_repl)
        action = _latest_session_lifecycle_action(db, thread_id, cfg.session_id)
        if action == "stopped":
            return SessionStatus(True, cfg.provider, cfg.session_id, "stopped", "In-memory test session provider is stopped", share_repl=cfg.share_repl)
        return SessionStatus(True, cfg.provider, cfg.session_id, "available", "In-memory test session provider", share_repl=cfg.share_repl)

    def start(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig) -> SessionStatus:
        return self.status(db, thread_id, cfg)

    def eval(
        self,
        db: ThreadsDB,
        runtime_thread_id: str,
        cfg: SessionConfig,
        *,
        language: str,
        code: str,
        repl_channel: str,
        eval_token: Optional[str],
        timeout_sec: Optional[float],
        cancel_check: Any = None,
    ) -> str:
        allowed, reason = _memory_provider_allowed_under_sandbox(db, runtime_thread_id)
        if not allowed:
            return reason
        if language == "python":
            return _execute_python_memory(cfg.session_id or "", repl_channel, code, eval_token=eval_token)
        if language == "bash":
            return _execute_bash_memory(cfg.session_id or "", repl_channel, code, eval_token=eval_token, timeout_sec=timeout_sec)
        return f"Error: unknown session language: {language}"

    def stop(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> SessionStatus:
        for key in list(_MEMORY_PYTHON_REPLS.keys()):
            if key[0] == cfg.session_id:
                _MEMORY_PYTHON_REPLS.pop(key, None)
        for key in list(_MEMORY_BASH_ENVS.keys()):
            if key[0] == cfg.session_id:
                _MEMORY_BASH_ENVS.pop(key, None)
        append_session_lifecycle_event(
            db,
            thread_id,
            action="stopped",
            session_id=cfg.session_id,
            payload={"provider": cfg.provider, "reason": reason},
        )
        return SessionStatus(True, cfg.provider, cfg.session_id, "stopped", _session_provider_stop_message(cfg), share_repl=cfg.share_repl)

    def reset(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> str:
        return _reset_thread_session_core(db, thread_id, cfg, reason=reason)

    def cleanup(self, db: ThreadsDB, **kwargs: Any) -> List[Dict[str, Any]]:
        return []


def _docker_wait_until_not_running(container_name: str, timeout_sec: float) -> _DockerContainerState:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    state = _docker_container_state(container_name)
    while state.exists is True and state.running is True and time.monotonic() < deadline:
        time.sleep(0.05)
        state = _docker_container_state(container_name)
    return state


def _docker_command_error(proc: Any, fallback: str) -> str:
    return str(getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or fallback).strip()


def _session_status_from_daemon(
    cfg: SessionConfig,
    container_name: str,
    daemon: Dict[str, Any],
    *,
    reason: Optional[str],
) -> SessionStatus:
    active = tuple(daemon.get("active_requests") or ())
    state = "busy" if active else "ready"
    return SessionStatus(
        True, cfg.provider, cfg.session_id, state,
        f"Docker session daemon is {state}", container_name, cfg.share_repl,
        daemon_generation=str(daemon.get("daemon_generation") or "") or None,
        active_requests=active,
        channel_state=dict(daemon.get("channel_state") or {}),
        last_activity=daemon.get("last_activity_at"),
        heartbeat_at=daemon.get("heartbeat_at"),
        reason=reason,
    )


class DockerSessionProvider:
    """Docker-backed persistent session provider."""

    name = "docker"

    def available(self) -> bool:
        return docker_session_available()

    def status(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig) -> SessionStatus:
        name = docker_session_container_name(db, cfg.session_id or _session_id_for_thread(thread_id))
        if not self.available():
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "unhealthy",
                "Docker daemon is not available", name, cfg.share_repl,
                reason="docker_unavailable",
            )
        container = _docker_container_state(name)
        lifecycle = _latest_session_lifecycle(db, thread_id, cfg.session_id) or {}
        lifecycle_reason = str(lifecycle.get("reason") or "").strip() or None
        if container.exists is None:
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "unhealthy",
                container.error or "Could not inspect Docker session container",
                name, cfg.share_repl, reason=lifecycle_reason or "inspect_failed",
            )
        if container.exists is False:
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "missing",
                "Docker session container has not been created",
                name, cfg.share_repl, reason=lifecycle_reason,
            )
        if container.running is not True:
            message = f"Docker session container is {container.status or 'stopped'}"
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "stopped", message,
                name, cfg.share_repl, reason=lifecycle_reason,
            )

        daemon, health_error = _docker_daemon_status(_session_bridge_dir(cfg.session_id or ""))
        if daemon is None or health_error:
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "unhealthy",
                health_error or "Docker session daemon status is unavailable",
                name, cfg.share_repl,
                daemon_generation=str((daemon or {}).get("daemon_generation") or "") or None,
                active_requests=tuple((daemon or {}).get("active_requests") or ()),
                channel_state=dict((daemon or {}).get("channel_state") or {}),
                last_activity=(daemon or {}).get("last_activity_at"),
                heartbeat_at=(daemon or {}).get("heartbeat_at"),
                reason="daemon_unhealthy",
            )
        return _session_status_from_daemon(
            cfg,
            name,
            daemon,
            reason=lifecycle_reason,
        )

    def start(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig) -> SessionStatus:
        return get_or_start_docker_session(db, thread_id)

    def eval(
        self,
        db: ThreadsDB,
        runtime_thread_id: str,
        cfg: SessionConfig,
        *,
        language: str,
        code: str,
        repl_channel: str,
        eval_token: Optional[str],
        timeout_sec: Optional[float],
        cancel_check: Any = None,
    ) -> str:
        captured = cfg
        while True:
            if not captured.enabled or captured.provider != "docker" or not captured.session_id:
                return "Error: Docker session configuration changed before eval."
            with _session_activity_guard(captured.session_id):
                current = get_thread_session_config(db, runtime_thread_id)
                if _session_config_identity(current) != _session_config_identity(captured):
                    captured = current
                    continue
                if language == "python":
                    return _execute_python_docker_captured(
                        db, runtime_thread_id, captured, code,
                        repl_name=repl_channel, eval_token=eval_token,
                        timeout_sec=timeout_sec, cancel_check=cancel_check,
                    )
                if language == "bash":
                    return _execute_bash_docker_captured(
                        db, runtime_thread_id, captured, code,
                        repl_name=repl_channel, eval_token=eval_token,
                        timeout_sec=timeout_sec, cancel_check=cancel_check,
                    )
                return f"Error: unknown session language: {language}"

    def stop(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> SessionStatus:
        container_name = docker_session_container_name(db, cfg.session_id or _session_id_for_thread(thread_id))
        if not self.available():
            append_session_lifecycle_event(
                db,
                thread_id,
                action="stop_unavailable",
                session_id=cfg.session_id,
                payload={
                    "provider": cfg.provider,
                    "container_name": container_name,
                    "reason": reason,
                    "error": "Docker daemon is not available",
                },
            )
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "unhealthy",
                "Docker daemon is not available", container_name, cfg.share_repl,
                reason=reason,
            )

        initial = _docker_container_state(container_name)
        if initial.exists is False:
            append_session_lifecycle_event(
                db, thread_id, action="stopped", session_id=cfg.session_id,
                payload={
                    "provider": cfg.provider,
                    "container_name": container_name,
                    "reason": reason,
                    "resulting_state": "missing",
                    "verified_stopped": True,
                    "already_absent": True,
                    "kill_fallback": False,
                },
            )
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "stopped",
                "Docker session container is already absent", container_name, cfg.share_repl,
                reason=reason,
            )
        if initial.exists is None:
            error = initial.error or "Could not inspect Docker session container"
            append_session_lifecycle_event(
                db, thread_id, action="stop_error", session_id=cfg.session_id,
                payload={"provider": cfg.provider, "container_name": container_name, "reason": reason, "error": error},
            )
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "unhealthy", error,
                container_name, cfg.share_repl, reason=reason,
            )

        stop_error = ""
        kill_used = False
        if initial.running is True:
            try:
                proc = subprocess.run(
                    ["docker", "stop", container_name], capture_output=True, text=True,
                    check=False, timeout=_DOCKER_STOP_TIMEOUT_SEC,
                )
                if proc.returncode != 0:
                    stop_error = _docker_command_error(proc, f"docker stop exited {proc.returncode}")
            except subprocess.TimeoutExpired:
                stop_error = f"docker stop timed out after {_DOCKER_STOP_TIMEOUT_SEC:g}s"
            except Exception as e:
                stop_error = f"{type(e).__name__}: {e}"

        observed = _docker_wait_until_not_running(container_name, _DOCKER_STOP_VERIFY_SEC)
        if observed.exists is True and observed.running is True:
            kill_used = True
            try:
                proc = subprocess.run(
                    ["docker", "kill", container_name], capture_output=True, text=True,
                    check=False, timeout=_DOCKER_KILL_TIMEOUT_SEC,
                )
                if proc.returncode != 0:
                    kill_error = _docker_command_error(proc, f"docker kill exited {proc.returncode}")
                    stop_error = "; ".join(part for part in (stop_error, kill_error) if part)
            except subprocess.TimeoutExpired:
                stop_error = "; ".join(part for part in (
                    stop_error,
                    f"docker kill timed out after {_DOCKER_KILL_TIMEOUT_SEC:g}s",
                ) if part)
            except Exception as e:
                stop_error = "; ".join(part for part in (stop_error, f"{type(e).__name__}: {e}") if part)
            observed = _docker_wait_until_not_running(container_name, _DOCKER_STOP_VERIFY_SEC)

        verified = observed.exists is False or (observed.exists is True and observed.running is False)
        payload = {
            "provider": cfg.provider,
            "container_name": container_name,
            "reason": reason,
            "stop_error": stop_error or None,
            "kill_fallback": kill_used,
            "resulting_state": observed.status,
            "verified_stopped": verified,
        }
        if verified:
            append_session_lifecycle_event(
                db, thread_id, action="stopped", session_id=cfg.session_id, payload=payload,
            )
            message = _session_provider_stop_message(cfg)
            if kill_used:
                message += " Docker kill fallback was required."
            return SessionStatus(
                True, cfg.provider, cfg.session_id, "stopped", message,
                container_name, cfg.share_repl, reason=reason,
            )

        error = stop_error or observed.error or "Container is still running after docker stop/kill"
        payload["error"] = error
        append_session_lifecycle_event(
            db, thread_id, action="stop_error", session_id=cfg.session_id, payload=payload,
        )
        return SessionStatus(
            True, cfg.provider, cfg.session_id, "unhealthy", error,
            container_name, cfg.share_repl, reason=reason,
        )

    def reset(self, db: ThreadsDB, thread_id: str, cfg: SessionConfig, *, reason: str = "user") -> str:
        return _reset_thread_session_core(db, thread_id, cfg, reason=reason)

    def cleanup(self, db: ThreadsDB, **kwargs: Any) -> List[Dict[str, Any]]:
        return cleanup_docker_sessions(db, **kwargs)


class SessionProviderRegistry:
    """Deterministic registry for persistent session providers."""

    def __init__(self) -> None:
        self._providers: Dict[str, SessionProvider] = {}

    def register(self, provider: SessionProvider) -> None:
        name = str(getattr(provider, "name", "") or "").strip()
        if not name:
            raise ValueError("Session provider name must not be empty")
        if name in self._providers:
            raise ValueError(f"Session provider already registered: {name}")
        self._providers[name] = provider

    def get(self, name: str) -> Optional[SessionProvider]:
        return self._providers.get(name)

    def names(self) -> List[str]:
        return list(self._providers.keys())


def create_session_provider_registry() -> SessionProviderRegistry:
    from .builtin_plugins.session_providers import SessionProvidersPlugin
    from .plugins import ProviderPluginContext, register_plugins

    registry = SessionProviderRegistry()
    register_plugins(ProviderPluginContext(session_provider_registry=registry), [SessionProvidersPlugin()])
    return registry


_SESSION_PROVIDER_REGISTRY = create_session_provider_registry()


def get_session_provider(name: str) -> Optional[SessionProvider]:
    return _SESSION_PROVIDER_REGISTRY.get(name)


def get_session_provider_names() -> List[str]:
    return _SESSION_PROVIDER_REGISTRY.names()


def execute_python_repl(
    db: ThreadsDB,
    caller_thread_id: str,
    code: str,
    *,
    repl_name: str = "default",
    runtime_name: str = "default",
    timeout_sec: Optional[float] = 30.0,
    drive_runtime_tools: bool = False,
    cancel_check: Any = None,
) -> str:
    """Execute Python code in the caller's persistent runtime session.

    MVP behavior:
      * creates/reuses ``@runtime:python`` child thread;
      * requires an enabled ``session.config`` (inherited by the runtime);
      * supports explicit ``provider='memory'`` for tests/development;
      * returns an actionable error for Docker until the Docker provider lands.
    """

    effective_timeout_sec = _coerce_positive_timeout(timeout_sec)

    # Safety invariant: normal REPL tool execution runs as an outer tool call
    # on the caller/application thread.  Programmatic eggtools calls from the
    # REPL are enqueued on the runtime child and should be driven by the active
    # SubtreeScheduler.  Direct-driving is only for isolated unit/headless tests
    # where no scheduler exists.
    if drive_runtime_tools:
        try:
            import asyncio as _asyncio

            _asyncio.get_running_loop()
            return (
                "Error: drive_runtime_tools=True cannot be used while an asyncio event loop is running. "
                "Queue python_repl as a tool call and let SubtreeScheduler drive runtime tool calls."
            )
        except RuntimeError:
            pass

    runtime_thread_id = get_or_create_runtime_thread(
        db,
        caller_thread_id,
        language="python",
        name=runtime_name,
        reason="python_repl",
    )
    cfg = ensure_thread_session_for_repl(db, runtime_thread_id, language="python", reason="python_repl")
    if not cfg.enabled or not cfg.session_id:
        return (
            "Error: persistent session is not enabled for this thread and auto-create is disabled. "
            "Set EGG_RLM_AUTO_SESSION=1 or call enable_thread_session(...)."
        )

    channel = repl_channel_name(runtime_thread_id, repl_name, share_repl=cfg.share_repl)

    if _latest_session_lifecycle_action(db, runtime_thread_id, cfg.session_id) == "stopped":
        append_session_lifecycle_event(
            db,
            runtime_thread_id,
            action="reattached",
            session_id=cfg.session_id,
            payload={"provider": cfg.provider, "reason": "python_repl"},
        )

    append_session_lifecycle_event(
        db,
        runtime_thread_id,
        action="python_eval",
        session_id=cfg.session_id,
        payload={
            "provider": cfg.provider,
            "repl_name": repl_name,
            "repl_channel": channel,
            "caller_thread_id": caller_thread_id,
            "runtime_thread_id": runtime_thread_id,
            "share_repl": cfg.share_repl,
        },
    )
    _append_runtime_repl_message(
        db,
        runtime_thread_id,
        "user",
        code,
        language="python",
        repl_name=repl_name,
        repl_channel=channel,
        session_id=cfg.session_id,
        caller_thread_id=caller_thread_id,
    )

    provider = get_session_provider(cfg.provider)
    if provider is None:
        return f"Error: unknown session provider: {cfg.provider}"

    from .repl_bridge import create_eval_context, dispose_eval_context

    ctx = create_eval_context(
        db,
        caller_thread_id=caller_thread_id,
        runtime_thread_id=runtime_thread_id,
        session_id=cfg.session_id,
        timeout_sec=effective_timeout_sec,
        drive_runtime_tools=drive_runtime_tools,
    )
    try:
        out = provider.eval(
            db,
            runtime_thread_id,
            cfg,
            language="python",
            code=code,
            repl_channel=channel,
            eval_token=ctx.token,
            timeout_sec=effective_timeout_sec,
            cancel_check=cancel_check,
        )
    finally:
        dispose_eval_context(ctx.token)
    _append_runtime_repl_message(
        db,
        runtime_thread_id,
        "tool",
        out,
        language="python",
        repl_name=repl_name,
        repl_channel=channel,
        session_id=cfg.session_id,
        caller_thread_id=caller_thread_id,
    )
    return out


def execute_bash_repl(
    db: ThreadsDB,
    caller_thread_id: str,
    script: str,
    *,
    repl_name: str = "default",
    runtime_name: str = "default",
    timeout_sec: Optional[float] = 30.0,
    drive_runtime_tools: bool = False,
    cancel_check: Any = None,
) -> str:
    """Execute Bash in the caller's persistent runtime session."""

    effective_timeout_sec = _coerce_positive_timeout(timeout_sec)

    if drive_runtime_tools:
        try:
            import asyncio as _asyncio

            _asyncio.get_running_loop()
            return (
                "Error: drive_runtime_tools=True cannot be used while an asyncio event loop is running. "
                "Queue bash_repl as a tool call and let SubtreeScheduler drive runtime tool calls."
            )
        except RuntimeError:
            pass

    runtime_thread_id = get_or_create_runtime_thread(
        db,
        caller_thread_id,
        language="bash",
        name=runtime_name,
        reason="bash_repl",
    )
    cfg = ensure_thread_session_for_repl(db, runtime_thread_id, language="bash", reason="bash_repl")
    if not cfg.enabled or not cfg.session_id:
        return (
            "Error: persistent session is not enabled for this thread and auto-create is disabled. "
            "Set EGG_RLM_AUTO_SESSION=1 or call enable_thread_session(...)."
        )

    channel = repl_channel_name(runtime_thread_id, repl_name, share_repl=cfg.share_repl)

    if _latest_session_lifecycle_action(db, runtime_thread_id, cfg.session_id) == "stopped":
        append_session_lifecycle_event(
            db,
            runtime_thread_id,
            action="reattached",
            session_id=cfg.session_id,
            payload={"provider": cfg.provider, "reason": "bash_repl"},
        )

    append_session_lifecycle_event(
        db,
        runtime_thread_id,
        action="bash_eval",
        session_id=cfg.session_id,
        payload={
            "provider": cfg.provider,
            "repl_name": repl_name,
            "repl_channel": channel,
            "caller_thread_id": caller_thread_id,
            "runtime_thread_id": runtime_thread_id,
            "share_repl": cfg.share_repl,
        },
    )
    _append_runtime_repl_message(
        db,
        runtime_thread_id,
        "user",
        script,
        language="bash",
        repl_name=repl_name,
        repl_channel=channel,
        session_id=cfg.session_id,
        caller_thread_id=caller_thread_id,
    )

    from .repl_bridge import create_eval_context, dispose_eval_context

    ctx = create_eval_context(
        db,
        caller_thread_id=caller_thread_id,
        runtime_thread_id=runtime_thread_id,
        session_id=cfg.session_id,
        timeout_sec=effective_timeout_sec,
        drive_runtime_tools=drive_runtime_tools,
    )
    try:
        provider = get_session_provider(cfg.provider)
        if provider is None:
            return f"Error: unknown session provider: {cfg.provider}"
        out = provider.eval(
            db,
            runtime_thread_id,
            cfg,
            language="bash",
            code=script,
            repl_channel=channel,
            eval_token=ctx.token,
            timeout_sec=effective_timeout_sec,
            cancel_check=cancel_check,
        )
        _append_runtime_repl_message(
            db,
            runtime_thread_id,
            "tool",
            out,
            language="bash",
            repl_name=repl_name,
            repl_channel=channel,
            session_id=cfg.session_id,
            caller_thread_id=caller_thread_id,
        )
        return out
    finally:
        dispose_eval_context(ctx.token)


def runtime_thread_label(*, language: str = "python", name: str = "default") -> str:
    """Return the conventional human-readable name for a runtime thread."""

    lang = _clean_runtime_part(language, "python")
    nm = _clean_runtime_part(name, "default")
    return f"@runtime:{lang}" if nm == "default" else f"@runtime:{lang}:{nm}"


def append_runtime_config(
    db: ThreadsDB,
    parent_thread_id: str,
    runtime_thread_id: str,
    *,
    language: str = "python",
    name: str = "default",
    session_id: Optional[str] = None,
    reason: str = "runtime",
) -> None:
    """Record the parent -> runtime-thread linkage as an event."""

    payload: Dict[str, Any] = {
        "runtime_thread_id": runtime_thread_id,
        "language": _clean_runtime_part(language, "python"),
        "name": _clean_runtime_part(name, "default"),
        "reason": reason,
    }
    if session_id:
        payload["session_id"] = session_id
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=parent_thread_id,
        type_="runtime.config",
        msg_id=None,
        invoke_id=None,
        payload=payload,
    )


def find_runtime_thread(
    db: ThreadsDB,
    parent_thread_id: str,
    *,
    language: str = "python",
    name: str = "default",
) -> Optional[RuntimeThreadConfig]:
    """Return the latest matching runtime thread config for a parent."""

    lang = _clean_runtime_part(language, "python")
    nm = _clean_runtime_part(name, "default")
    try:
        cur = db.conn.execute(
            "SELECT event_seq, payload_json FROM events "
            "WHERE thread_id=? AND type='runtime.config' ORDER BY event_seq DESC",
            (parent_thread_id,),
        )
    except Exception:
        return None

    for event_seq, payload_json in cur.fetchall():
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        if _clean_runtime_part(payload.get("language"), "python") != lang:
            continue
        if _clean_runtime_part(payload.get("name"), "default") != nm:
            continue
        runtime_thread_id = payload.get("runtime_thread_id")
        if not isinstance(runtime_thread_id, str) or not runtime_thread_id.strip():
            continue
        if db.get_thread(runtime_thread_id) is None:
            # Stale event referencing a deleted runtime thread; keep looking.
            continue
        _ensure_runtime_thread_child_link(db, parent_thread_id, runtime_thread_id)
        session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
        return RuntimeThreadConfig(
            parent_thread_id=parent_thread_id,
            runtime_thread_id=runtime_thread_id,
            language=lang,
            name=nm,
            session_id=session_id,
            source_event_seq=int(event_seq) if event_seq is not None else None,
        )
    return None


def _ensure_runtime_thread_child_link(db: ThreadsDB, parent_thread_id: str, runtime_thread_id: str) -> bool:
    """Ensure a configured runtime thread is a child of its caller thread.

    Current runtime threads are created with ``create_child_thread``.  This
    helper repairs older/incomplete rows where a ``runtime.config`` event links
    parent and runtime but the ``children`` edge is missing.  If the runtime is
    already attached to a different parent, leave it alone rather than creating
    a multi-parent tree.
    """

    try:
        parent = db.get_thread(parent_thread_id)
        runtime = db.get_thread(runtime_thread_id)
        if parent is None or runtime is None:
            return False

        rows = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=?",
            (runtime_thread_id,),
        ).fetchall()
        existing_parents = {str(row[0]) for row in rows if row and row[0]}
        desired_depth = int(parent.depth or 0) + 1
        if parent_thread_id in existing_parents:
            if int(runtime.depth or 0) != desired_depth:
                db.conn.execute(
                    "UPDATE threads SET depth=? WHERE thread_id=?",
                    (desired_depth, runtime_thread_id),
                )
            return False
        if existing_parents:
            return False

        db.conn.execute(
            "INSERT INTO children(parent_id, child_id, waiting_until) VALUES (?,?,NULL)",
            (parent_thread_id, runtime_thread_id),
        )
        db.conn.execute(
            "UPDATE threads SET depth=? WHERE thread_id=?",
            (desired_depth, runtime_thread_id),
        )
        try:
            db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=parent_thread_id,
                type_="thread.child_created",
                payload={
                    "parent_id": parent_thread_id,
                    "child_id": runtime_thread_id,
                    "name": runtime.name,
                    "reason": "runtime_child_link_repair",
                },
            )
        except Exception:
            pass
        return True
    except Exception:
        return False


def _get_or_create_runtime_thread_unlocked(
    db: ThreadsDB,
    parent_thread_id: str,
    *,
    language: str = "python",
    name: str = "default",
    session_id: Optional[str] = None,
    reason: str = "runtime",
) -> str:
    """Return a real child thread used as the runtime/audit container.

    The runtime thread is created under ``parent_thread_id`` if no live matching
    ``runtime.config`` event exists.  It is configured so that runtime-internal
    user/tool messages do not accidentally trigger LLM turns by default.
    """

    existing = find_runtime_thread(db, parent_thread_id, language=language, name=name)
    if existing is not None:
        _ensure_runtime_thread_child_link(db, parent_thread_id, existing.runtime_thread_id)
        return existing.runtime_thread_id

    # Import lazily to avoid api <-> session import cycles at module import time.
    from .api import append_message, create_child_thread, create_snapshot

    lang = _clean_runtime_part(language, "python")
    nm = _clean_runtime_part(name, "default")
    child = create_child_thread(db, parent_thread_id, name=runtime_thread_label(language=lang, name=nm))

    # Mark this as a runtime thread in its own log for easy diagnosis/UI.
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=child,
        type_="runtime.thread",
        msg_id=None,
        invoke_id=None,
        payload={
            "parent_thread_id": parent_thread_id,
            "language": lang,
            "name": nm,
            "reason": reason,
        },
    )

    append_runtime_config(
        db,
        parent_thread_id,
        child,
        language=lang,
        name=nm,
        session_id=session_id,
        reason=reason,
    )

    append_message(
        db,
        child,
        "system",
        (
            "You are an eggthreads runtime thread. This thread records persistent "
            "REPL/session execution and programmatic tool calls. Runtime-internal "
            "messages are normally hidden from provider APIs."
        ),
        extra={"no_api": True, "runtime": True},
    )
    create_snapshot(db, child)
    return child


def get_or_create_runtime_thread(
    db: ThreadsDB,
    parent_thread_id: str,
    *,
    language: str = "python",
    name: str = "default",
    session_id: Optional[str] = None,
    reason: str = "runtime",
) -> str:
    """Create runtime inheritance while guarding the captured Docker session."""

    while True:
        parent_cfg = get_thread_session_config(db, parent_thread_id)
        inherited_session_id = (
            parent_cfg.session_id
            if parent_cfg.enabled and parent_cfg.provider == "docker"
            else None
        )
        guard = _session_activity_guard(inherited_session_id) if inherited_session_id else nullcontext(True)
        retry = False
        with guard:
            current_cfg = get_thread_session_config(db, parent_thread_id)
            current_inherited_id = (
                current_cfg.session_id
                if current_cfg.enabled and current_cfg.provider == "docker"
                else None
            )
            if current_inherited_id != inherited_session_id:
                retry = True
            else:
                return _get_or_create_runtime_thread_unlocked(
                    db,
                    parent_thread_id,
                    language=language,
                    name=name,
                    session_id=session_id,
                    reason=reason,
                )
        if retry:
            continue


__all__ = [
    "RuntimeThreadConfig",
    "SessionConfig",
    "SessionProvider",
    "SessionProviderRegistry",
    "SessionStatus",
    "MemorySessionProvider",
    "DockerSessionProvider",
    "create_session_provider_registry",
    "get_session_provider",
    "get_session_provider_names",
    "get_thread_session_config",
    "get_thread_session_status",
    "docker_session_container_name",
    "docker_session_db_hash",
    "docker_session_available",
    "docker_session_mount_dir",
    "list_docker_session_containers",
    "cleanup_docker_sessions",
    "cleanup_thread_sessions",
    "auto_session_idle_timeout_sec",
    "reap_idle_auto_docker_sessions",
    "start_idle_auto_docker_reaper",
    "get_or_start_docker_session",
    "get_or_start_docker_session_handle",
    "set_thread_session_config",
    "enable_thread_session",
    "disable_thread_session",
    "stop_thread_session",
    "reset_thread_session",
    "ensure_thread_session_for_repl",
    "append_session_lifecycle_event",
    "execute_python_repl",
    "execute_bash_repl",
    "repl_channel_name",
    "runtime_thread_label",
    "append_runtime_config",
    "find_runtime_thread",
    "get_or_create_runtime_thread",
]
