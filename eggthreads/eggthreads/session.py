from __future__ import annotations

"""Runtime-thread/session helpers for explicit RLM.

This module intentionally starts with the event-sourced *runtime thread*
layer before adding Docker/REPL providers.  A runtime thread is a real child
thread used as the execution/audit container for programmatic REPL tool calls.
"""

import json
import os
import subprocess
import tempfile
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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


@dataclass(frozen=True)
class DockerSessionHandle:
    session_id: str
    container_name: str
    bridge_dir: str
    runtime_dir: str
    mount_dir: str
    workspace: str


_DOCKER_MOUNT_POLICY = "thread-workdir-mask-egg-sandbox-v2"


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


def _latest_session_lifecycle_action(db: ThreadsDB, thread_id: str, session_id: Optional[str]) -> Optional[str]:
    """Return latest lifecycle action for a specific session id on a thread."""

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
        if not isinstance(payload, dict):
            continue
        if payload.get("session_id") != session_id:
            continue
        action = payload.get("action")
        return str(action) if action is not None else None
    return None


def _session_id_for_thread(thread_id: str) -> str:
    """Return a stable default session id for a thread/runtime."""

    safe = ''.join(ch for ch in str(thread_id) if ch.isalnum())
    return f"sess_{safe}" if safe else f"sess_{os.urandom(5).hex()}"


def docker_session_container_name(db: ThreadsDB, session_id: str) -> str:
    """Return deterministic Docker container name for a session id."""

    import hashlib

    db_hash = hashlib.sha256(str(db.path).encode("utf-8")).hexdigest()[:12]
    safe_session = ''.join(ch.lower() if ch.isalnum() else '-' for ch in str(session_id))
    return f"egg-rlm-{db_hash}-{safe_session[:48]}"


def docker_session_db_hash(db: ThreadsDB) -> str:
    """Return the stable db hash used in Docker session labels/names."""

    import hashlib

    return hashlib.sha256(str(db.path).encode("utf-8")).hexdigest()[:12]


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
    fs = settings.get("filesystem") if isinstance(settings, dict) else None
    if not isinstance(fs, dict):
        return []
    raw = fs.get(key)
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: List[str] = []
    for value in raw:
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def _resolve_sandbox_path(value: str, mount_dir: Path) -> Optional[Path]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = mount_dir / p
        return p.resolve()
    except Exception:
        return None


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
                # (notably .egg/.egg_outputs). Avoid duplicate/nested Docker
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


def _safe_thread_output_dir_name(thread_id: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in str(thread_id or 'thread'))
    return safe or 'thread'


def _docker_repl_thread_output_mount_args(*, mount_dir: Path, workspace: str, runtime_thread_id: str) -> List[str]:
    """Expose only this thread's long-output stash inside the REPL container."""

    mount_dir = mount_dir.resolve()
    workspace = workspace or "/workspace"
    safe_tid = _safe_thread_output_dir_name(runtime_thread_id)
    host_dir = mount_dir / ".egg_outputs" / safe_tid
    try:
        host_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    container_dir = str(Path(workspace.rstrip("/") or "/") / ".egg_outputs" / safe_tid)
    return ["-v", f"{host_dir}:{container_dir}:ro"]


def _prepare_outputs_mask_dir(session_id: str, runtime_thread_id: str) -> Path:
    """Create an empty .egg_outputs mask with this thread's mountpoint."""

    mask_dir = _session_mask_dir(session_id, "egg_outputs")
    safe_tid = _safe_thread_output_dir_name(runtime_thread_id)
    try:
        (mask_dir / safe_tid).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return mask_dir


def _docker_existing_mount_policy(container_name: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{ index .Config.Labels \"egg.mount_policy\" }}", container_name],
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


def _write_runtime_files(runtime_dir: Path) -> None:
    from importlib import resources

    for name in ("eggtools.py", "sessiond.py", "eggtool"):
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


def _docker_inspect_running(container_name: str) -> Optional[bool]:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().lower() == "true"


def _start_docker_container(
    db: ThreadsDB,
    runtime_thread_id: str,
    cfg: SessionConfig,
    container_name: str,
    bridge_dir: Path,
    runtime_dir: Path,
) -> None:
    existing_running = _docker_inspect_running(container_name)
    if existing_running is True:
        return
    if existing_running is False:
        policy = _docker_existing_mount_policy(container_name)
        if policy and policy != _DOCKER_MOUNT_POLICY:
            raise RuntimeError(
                f"Existing Docker session container {container_name!r} uses old mount policy {policy!r}; "
                "run /sessionReset or remove the container to recreate it safely."
            )
        subprocess.run(["docker", "start", container_name], capture_output=True, check=True, timeout=20)
        return

    workspace = cfg.workspace or "/workspace"
    network = cfg.network or "none"
    mount_dir = docker_session_mount_dir(db, runtime_thread_id, cfg)
    mandatory_mask_args = _docker_repl_mandatory_mask_args(
        mount_dir=mount_dir,
        workspace=workspace,
        session_id=cfg.session_id or container_name,
    )
    outputs_mask_dir = _prepare_outputs_mask_dir(cfg.session_id or container_name, runtime_thread_id)
    thread_output_mount_args = _docker_repl_thread_output_mount_args(
        mount_dir=mount_dir,
        workspace=workspace,
        runtime_thread_id=runtime_thread_id,
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
            # Do not call apply_mandatory_protections("srt", ...) here: that
            # would inject the whole .egg directory into denyWrite, and this
            # REPL mount layer has stronger fixed masks for .egg/.egg_outputs
            # below. We also want missing allowWrite to mean workspace rw by
            # default, not the Docker provider default's allowWrite=["."].
            network = _sandbox_network_to_docker(settings.get("network"), network)
            sandbox_mount_args = _docker_repl_mount_args_from_sandbox(
                mount_dir=mount_dir,
                workspace=workspace,
                sandbox_settings=settings,
                skip_denied_paths=[mount_dir / ".egg", mount_dir / ".egg_outputs"],
            )
    except Exception:
        sandbox_effective = False

    cmd = [
        "docker", "run", "-d", "--init",
        "--name", container_name,
        "--user", f"{os.getuid()}",
        "--network", network,
        "--label", "egg.kind=rlm-session",
        "--label", f"egg.session_id={cfg.session_id}",
        "--label", f"egg.owner_thread_id={cfg.owner_thread_id or runtime_thread_id}",
        "--label", f"egg.runtime_thread_id={runtime_thread_id}",
        "--label", f"egg.db_hash={docker_session_db_hash(db)}",
        "--label", f"egg.mount_policy={_DOCKER_MOUNT_POLICY}",
        "--label", f"egg.sandbox_mounts={'on' if sandbox_effective else 'off'}",
        "-v", f"{bridge_dir}:/egg-bridge",
        "-v", f"{runtime_dir}:/egg-runtime:ro",
        *sandbox_mount_args,
        "-v", f"{outputs_mask_dir}:{workspace.rstrip('/')}/.egg_outputs:ro",
        *thread_output_mount_args,
        *mandatory_mask_args,
        "--cap-drop", "ALL",
        "-w", workspace,
        cfg.image,
        "python3", "/egg-runtime/sessiond.py", "--bridge-dir", "/egg-bridge", "--runtime-dir", "/egg-runtime",
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=60)


def get_thread_session_config(db: ThreadsDB, thread_id: str) -> SessionConfig:
    """Resolve effective session.config for a thread, with ancestor inheritance."""

    found = _nearest_session_payload(db, thread_id)
    if found is None:
        return SessionConfig()

    source_tid, payload = found
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


def get_thread_session_status(db: ThreadsDB, thread_id: str) -> SessionStatus:
    """Return lightweight status for the effective session config.

    The real Docker lifecycle lands later; this status helper already gives
    callers a stable API and supports the in-memory test provider.
    """

    cfg = get_thread_session_config(db, thread_id)
    if not cfg.enabled:
        return SessionStatus(False, cfg.provider, cfg.session_id, "disabled", "Session is disabled", share_repl=cfg.share_repl)
    if cfg.provider == "memory":
        action = _latest_session_lifecycle_action(db, thread_id, cfg.session_id)
        if action == "stopped":
            return SessionStatus(True, cfg.provider, cfg.session_id, "stopped", "In-memory test session provider is stopped", share_repl=cfg.share_repl)
        return SessionStatus(True, cfg.provider, cfg.session_id, "available", "In-memory test session provider", share_repl=cfg.share_repl)
    if cfg.provider == "docker":
        name = docker_session_container_name(db, cfg.session_id or _session_id_for_thread(thread_id))
        if not docker_session_available():
            return SessionStatus(True, cfg.provider, cfg.session_id, "unavailable", "Docker is not available", name, cfg.share_repl)
        action = _latest_session_lifecycle_action(db, thread_id, cfg.session_id)
        if action == "stopped":
            return SessionStatus(True, cfg.provider, cfg.session_id, "stopped", "Docker session is stopped", name, cfg.share_repl)
        return SessionStatus(True, cfg.provider, cfg.session_id, "available", "Docker session provider skeleton is available", name, cfg.share_repl)
    return SessionStatus(True, cfg.provider, cfg.session_id, "unavailable", f"Unknown session provider: {cfg.provider}", share_repl=cfg.share_repl)


def get_or_start_docker_session(db: ThreadsDB, thread_id: str) -> SessionStatus:
    """Skeleton for persistent Docker session start/reattach.

    This establishes deterministic naming/status/lifecycle events without yet
    implementing the full `egg-sessiond` protocol.  It is deliberately safe:
    if Docker is unavailable, no container command is attempted.
    """

    cfg = get_thread_session_config(db, thread_id)
    if not cfg.enabled or cfg.provider != "docker" or not cfg.session_id:
        return get_thread_session_status(db, thread_id)
    status = get_thread_session_status(db, thread_id)
    if status.status not in ("available", "stopped") or not status.container_name:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="docker_unavailable",
            session_id=cfg.session_id,
            payload={"message": status.message},
        )
        return status

    bridge_dir = _session_bridge_dir(cfg.session_id)
    runtime_dir = _session_runtime_dir(cfg.session_id)
    mount_dir = docker_session_mount_dir(db, thread_id, cfg)
    _write_runtime_files(runtime_dir)
    try:
        _start_docker_container(db, thread_id, cfg, status.container_name, bridge_dir, runtime_dir)
        action = "reattached" if status.status == "stopped" else "docker_started"
    except Exception as e:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="docker_error",
            session_id=cfg.session_id,
            payload={"container_name": status.container_name, "error": str(e)},
        )
        return SessionStatus(True, cfg.provider, cfg.session_id, "error", str(e), status.container_name, cfg.share_repl)

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
        },
    )
    return status


def get_or_start_docker_session_handle(db: ThreadsDB, thread_id: str) -> DockerSessionHandle:
    cfg = get_thread_session_config(db, thread_id)
    if not cfg.enabled or cfg.provider != "docker" or not cfg.session_id:
        raise RuntimeError("Docker session is not enabled for this thread")
    status = get_or_start_docker_session(db, thread_id)
    if status.status not in ("available",) or not status.container_name:
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


def _service_tool_requests(bridge_dir: Path) -> None:
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


def _execute_python_docker(
    db: ThreadsDB,
    runtime_thread_id: str,
    code: str,
    *,
    repl_name: str,
    eval_token: str,
    timeout_sec: Optional[float],
) -> str:
    handle = get_or_start_docker_session_handle(db, runtime_thread_id)
    bridge_dir = Path(handle.bridge_dir)
    req_id = os.urandom(8).hex()
    req_path = bridge_dir / f"eval_{req_id}.req.json"
    res_path = bridge_dir / f"eval_{req_id}.res.json"
    _atomic_write_json(req_path, {
        "id": req_id,
        "language": "python",
        "code": code,
        "repl_name": repl_name,
        "token": eval_token,
    })
    start = time.time()
    while True:
        _service_tool_requests(bridge_dir)
        if res_path.exists():
            try:
                payload = json.loads(res_path.read_text(encoding="utf-8"))
            finally:
                try:
                    res_path.unlink()
                except Exception:
                    pass
            if payload.get("ok"):
                return str(payload.get("output") or "")
            return f"Error: Docker Python REPL failed: {payload.get('error') or 'unknown error'}"
        if timeout_sec is not None and (time.time() - start) >= float(timeout_sec):
            return "Error: Docker Python REPL timed out."


def _execute_bash_docker(
    db: ThreadsDB,
    runtime_thread_id: str,
    script: str,
    *,
    repl_name: str,
    eval_token: str,
    timeout_sec: Optional[float],
) -> str:
    handle = get_or_start_docker_session_handle(db, runtime_thread_id)
    bridge_dir = Path(handle.bridge_dir)
    req_id = os.urandom(8).hex()
    req_path = bridge_dir / f"eval_{req_id}.req.json"
    res_path = bridge_dir / f"eval_{req_id}.res.json"
    _atomic_write_json(req_path, {
        "id": req_id,
        "language": "bash",
        "script": script,
        "repl_name": repl_name,
        "token": eval_token,
        "timeout_sec": timeout_sec,
    })
    start = time.time()
    while True:
        _service_tool_requests(bridge_dir)
        if res_path.exists():
            try:
                payload = json.loads(res_path.read_text(encoding="utf-8"))
            finally:
                try:
                    res_path.unlink()
                except Exception:
                    pass
            if payload.get("ok"):
                return str(payload.get("output") or "")
            return f"Error: Docker Bash REPL failed: {payload.get('error') or 'unknown error'}"
        if timeout_sec is not None and (time.time() - start) >= float(timeout_sec):
            return "Error: Docker Bash REPL timed out."
        time.sleep(0.05)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# In-memory Python provider (test/development only)
# ---------------------------------------------------------------------------

_MEMORY_PYTHON_REPLS: Dict[tuple[str, str], Dict[str, Any]] = {}
_MEMORY_BASH_ENVS: Dict[tuple[str, str], Dict[str, str]] = {}


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
        timeout_sec = args.get("timeout_sec")
        if timeout_sec is not None:
            args.setdefault("_egg_tool_timeout_sec", timeout_sec)
            try:
                return float(timeout_sec)
            except Exception:
                return None
        return None

    def tool(name: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        return repl_bridge.call_tool(eval_token, name, args, timeout_sec=_tool_timeout(args))

    def spawn_agent(context_text: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["context_text"] = context_text
        args.setdefault("_egg_raw_thread_id_result", True)
        return repl_bridge.call_tool(eval_token, "spawn_agent", args, timeout_sec=_tool_timeout(args))

    def spawn_agent_auto(context_text: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["context_text"] = context_text
        args.setdefault("_egg_raw_thread_id_result", True)
        return repl_bridge.call_tool(eval_token, "spawn_agent_auto", args, timeout_sec=_tool_timeout(args))

    def send_message_to_child(child_thread_id: str, message: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["child_thread_id"] = child_thread_id
        args["message"] = message
        return repl_bridge.call_tool(eval_token, "send_message_to_child", args, timeout_sec=_tool_timeout(args))

    def wait(thread_ids: Any, **kwargs: Any) -> str:
        if isinstance(thread_ids, (str, int)):
            thread_ids = [str(thread_ids)]
        if isinstance(thread_ids, (list, tuple, set)):
            thread_ids = [str(t).splitlines()[-1].strip() for t in thread_ids if isinstance(t, (str, int))]
        args = dict(kwargs)
        args["thread_ids"] = thread_ids
        return repl_bridge.call_tool(eval_token, "wait", args, timeout_sec=_tool_timeout(args))

    def web_search(query: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["query"] = query
        return repl_bridge.call_tool(eval_token, "web_search", args, timeout_sec=_tool_timeout(args))

    def fetch_url(url: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["url"] = url
        return repl_bridge.call_tool(eval_token, "fetch_url", args, timeout_sec=_tool_timeout(args))

    def skill(name: Optional[str] = None, **kwargs: Any) -> str:
        args = dict(kwargs)
        if name is not None:
            args["name"] = name
        return repl_bridge.call_tool(eval_token, "skill", args, timeout_sec=_tool_timeout(args))

    def bash(script: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["script"] = script
        return repl_bridge.call_tool(eval_token, "bash", args, timeout_sec=_tool_timeout(args))

    def python(script: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["script"] = script
        return repl_bridge.call_tool(eval_token, "python", args, timeout_sec=_tool_timeout(args))

    def session_status(**kwargs: Any) -> str:
        return repl_bridge.call_tool(eval_token, "session_status", dict(kwargs))

    def session_reset(**kwargs: Any) -> str:
        return repl_bridge.call_tool(eval_token, "session_reset", dict(kwargs))

    def session_stop(**kwargs: Any) -> str:
        return repl_bridge.call_tool(eval_token, "session_stop", dict(kwargs))

    mod.tool = tool
    mod.spawn_agent = spawn_agent
    mod.spawn_agent_auto = spawn_agent_auto
    mod.send_message_to_child = send_message_to_child
    mod.wait = wait
    mod.web_search = web_search
    mod.fetch_url = fetch_url
    mod.skill = skill
    mod.bash = bash
    mod.python = python
    mod.session_status = session_status
    mod.session_reset = session_reset
    mod.session_stop = session_stop
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
    """Stop the effective session for ``thread_id`` when the provider supports it.

    The configuration event is intentionally left intact; this is a lifecycle
    operation, not ``/sessionOff``.  A later REPL eval may reattach/restart the
    same configured session id.
    """

    cfg = get_thread_session_config(db, thread_id)
    if not cfg.enabled or not cfg.session_id:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="stop_ignored",
            session_id=cfg.session_id,
            payload={"reason": reason, "message": "Session is not enabled"},
        )
        return get_thread_session_status(db, thread_id)

    if cfg.provider == "memory":
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

    if cfg.provider == "docker":
        container_name = docker_session_container_name(db, cfg.session_id)
        if not docker_session_available():
            append_session_lifecycle_event(
                db,
                thread_id,
                action="stop_unavailable",
                session_id=cfg.session_id,
                payload={"provider": cfg.provider, "container_name": container_name, "reason": reason},
            )
            return SessionStatus(True, cfg.provider, cfg.session_id, "unavailable", "Docker is not available", container_name, cfg.share_repl)
        try:
            subprocess.run(["docker", "stop", container_name], capture_output=True, check=False, timeout=20)
            append_session_lifecycle_event(
                db,
                thread_id,
                action="stopped",
                session_id=cfg.session_id,
                payload={"provider": cfg.provider, "container_name": container_name, "reason": reason},
            )
            return SessionStatus(True, cfg.provider, cfg.session_id, "stopped", _session_provider_stop_message(cfg), container_name, cfg.share_repl)
        except Exception as e:
            append_session_lifecycle_event(
                db,
                thread_id,
                action="stop_error",
                session_id=cfg.session_id,
                payload={"provider": cfg.provider, "container_name": container_name, "reason": reason, "error": str(e)},
            )
            return SessionStatus(True, cfg.provider, cfg.session_id, "error", str(e), container_name, cfg.share_repl)

    append_session_lifecycle_event(
        db,
        thread_id,
        action="stop_error",
        session_id=cfg.session_id,
        payload={"provider": cfg.provider, "reason": reason, "error": f"Unknown session provider: {cfg.provider}"},
    )
    return SessionStatus(True, cfg.provider, cfg.session_id, "unavailable", f"Unknown session provider: {cfg.provider}", share_repl=cfg.share_repl)


def reset_thread_session(db: ThreadsDB, thread_id: str, *, reason: str = "user") -> str:
    """Reset a session's mutable state and assign a fresh session id.

    Reset is event-sourced as a stop/lifecycle event followed by a new
    ``session.config`` event with the same provider/image/share policy but a
    new session id.  This preserves auditability and keeps containers out of
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


def execute_python_repl(
    db: ThreadsDB,
    caller_thread_id: str,
    code: str,
    *,
    repl_name: str = "default",
    runtime_name: str = "default",
    bridge_timeout_sec: Optional[float] = 30.0,
    drive_runtime_tools: bool = False,
) -> str:
    """Execute Python code in the caller's persistent runtime session.

    MVP behavior:
      * creates/reuses ``@runtime:python`` child thread;
      * requires an enabled ``session.config`` (inherited by the runtime);
      * supports explicit ``provider='memory'`` for tests/development;
      * returns an actionable error for Docker until the Docker provider lands.
    """

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

    if cfg.provider == "memory":
        from .repl_bridge import create_eval_context, dispose_eval_context

        ctx = create_eval_context(
            db,
            caller_thread_id=caller_thread_id,
            runtime_thread_id=runtime_thread_id,
            session_id=cfg.session_id,
            bridge_timeout_sec=bridge_timeout_sec,
            drive_runtime_tools=drive_runtime_tools,
        )
        try:
            out = _execute_python_memory(cfg.session_id, channel, code, eval_token=ctx.token)
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
    if cfg.provider == "docker":
        from .repl_bridge import create_eval_context, dispose_eval_context

        ctx = create_eval_context(
            db,
            caller_thread_id=caller_thread_id,
            runtime_thread_id=runtime_thread_id,
            session_id=cfg.session_id,
            bridge_timeout_sec=bridge_timeout_sec,
            drive_runtime_tools=drive_runtime_tools,
        )
        try:
            out = _execute_python_docker(
                db,
                runtime_thread_id,
                code,
                repl_name=channel,
                eval_token=ctx.token,
                timeout_sec=bridge_timeout_sec,
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
    return f"Error: unknown session provider: {cfg.provider}"


def execute_bash_repl(
    db: ThreadsDB,
    caller_thread_id: str,
    script: str,
    *,
    repl_name: str = "default",
    runtime_name: str = "default",
    bridge_timeout_sec: Optional[float] = 30.0,
    drive_runtime_tools: bool = False,
) -> str:
    """Execute Bash in the caller's persistent runtime session."""

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
        bridge_timeout_sec=bridge_timeout_sec,
        drive_runtime_tools=drive_runtime_tools,
    )
    try:
        if cfg.provider == "memory":
            out = _execute_bash_memory(cfg.session_id, channel, script, eval_token=ctx.token, timeout_sec=bridge_timeout_sec)
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
        if cfg.provider == "docker":
            out = _execute_bash_docker(
                db,
                runtime_thread_id,
                script,
                repl_name=channel,
                eval_token=ctx.token,
                timeout_sec=bridge_timeout_sec,
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
        return f"Error: unknown session provider: {cfg.provider}"
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


def get_or_create_runtime_thread(
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
        return existing.runtime_thread_id

    # Import lazily to avoid api <-> session import cycles at module import time.
    from .api import append_message, create_child_thread, create_snapshot
    from .tools_config import set_thread_tools_enabled

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

    # Runtime threads are execution/audit containers by default, not LLM agents.
    set_thread_tools_enabled(db, child, False)
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


__all__ = [
    "RuntimeThreadConfig",
    "SessionConfig",
    "SessionStatus",
    "get_thread_session_config",
    "get_thread_session_status",
    "docker_session_container_name",
    "docker_session_db_hash",
    "docker_session_available",
    "docker_session_mount_dir",
    "list_docker_session_containers",
    "cleanup_docker_sessions",
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
