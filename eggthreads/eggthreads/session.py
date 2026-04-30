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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

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
    share_with_children_default: bool = False
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


@dataclass(frozen=True)
class DockerSessionHandle:
    session_id: str
    container_name: str
    bridge_dir: str
    runtime_dir: str


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


def _start_docker_container(cfg: SessionConfig, container_name: str, bridge_dir: Path, runtime_dir: Path) -> None:
    existing_running = _docker_inspect_running(container_name)
    if existing_running is True:
        return
    if existing_running is False:
        subprocess.run(["docker", "start", container_name], capture_output=True, check=True, timeout=20)
        return

    workspace = cfg.workspace or "/workspace"
    cmd = [
        "docker", "run", "-d", "--init",
        "--name", container_name,
        "--label", "egg.kind=rlm-session",
        "--label", f"egg.session_id={cfg.session_id}",
        "-v", f"{bridge_dir}:/egg-bridge",
        "-v", f"{runtime_dir}:/egg-runtime:ro",
        "-v", f"{Path.cwd().resolve()}:{workspace}",
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
        share_with_children_default=bool(payload.get("share_with_children_default", False)),
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
    share_with_children_default: bool = False,
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
        "share_with_children_default": bool(share_with_children_default),
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
        return SessionStatus(False, cfg.provider, cfg.session_id, "disabled", "Session is disabled")
    if cfg.provider == "memory":
        return SessionStatus(True, cfg.provider, cfg.session_id, "available", "In-memory test session provider")
    if cfg.provider == "docker":
        name = docker_session_container_name(db, cfg.session_id or _session_id_for_thread(thread_id))
        if not docker_session_available():
            return SessionStatus(True, cfg.provider, cfg.session_id, "unavailable", "Docker is not available", name)
        return SessionStatus(True, cfg.provider, cfg.session_id, "available", "Docker session provider skeleton is available", name)
    return SessionStatus(True, cfg.provider, cfg.session_id, "unavailable", f"Unknown session provider: {cfg.provider}")


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
    if status.status != "available" or not status.container_name:
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
    _write_runtime_files(runtime_dir)
    try:
        _start_docker_container(cfg, status.container_name, bridge_dir, runtime_dir)
        action = "docker_started"
    except Exception as e:
        append_session_lifecycle_event(
            db,
            thread_id,
            action="docker_error",
            session_id=cfg.session_id,
            payload={"container_name": status.container_name, "error": str(e)},
        )
        return SessionStatus(True, cfg.provider, cfg.session_id, "error", str(e), status.container_name)

    append_session_lifecycle_event(
        db,
        thread_id,
        action=action,
        session_id=cfg.session_id,
        payload={
            "container_name": status.container_name,
            "image": cfg.image,
            "workspace": cfg.workspace,
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


def _make_eggtools_module(eval_token: str):
    """Create an in-memory eggtools module bound to an eval token."""

    import types
    from . import repl_bridge

    mod = types.ModuleType("eggtools")

    def tool(name: str, **kwargs: Any) -> str:
        return repl_bridge.call_tool(eval_token, name, kwargs)

    def spawn_agent(context_text: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["context_text"] = context_text
        return repl_bridge.call_tool(eval_token, "spawn_agent", args)

    def spawn_agent_auto(context_text: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["context_text"] = context_text
        return repl_bridge.call_tool(eval_token, "spawn_agent_auto", args)

    def wait(thread_ids: list[str], **kwargs: Any) -> str:
        args = dict(kwargs)
        args["thread_ids"] = thread_ids
        return repl_bridge.call_tool(eval_token, "wait", args)

    def web_search(query: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["query"] = query
        return repl_bridge.call_tool(eval_token, "web_search", args)

    def fetch_url(url: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["url"] = url
        return repl_bridge.call_tool(eval_token, "fetch_url", args)

    def bash(script: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["script"] = script
        return repl_bridge.call_tool(eval_token, "bash", args)

    def python(script: str, **kwargs: Any) -> str:
        args = dict(kwargs)
        args["script"] = script
        return repl_bridge.call_tool(eval_token, "python", args)

    mod.tool = tool
    mod.spawn_agent = spawn_agent
    mod.spawn_agent_auto = spawn_agent_auto
    mod.wait = wait
    mod.web_search = web_search
    mod.fetch_url = fetch_url
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

    runtime_thread_id = get_or_create_runtime_thread(
        db,
        caller_thread_id,
        language="python",
        name=runtime_name,
        reason="python_repl",
    )
    cfg = get_thread_session_config(db, runtime_thread_id)
    if not cfg.enabled or not cfg.session_id:
        return (
            "Error: persistent session is not enabled for this thread. "
            "Call enable_thread_session(..., provider='memory') for tests or "
            "provider='docker' once Docker sessions are implemented."
        )

    append_session_lifecycle_event(
        db,
        runtime_thread_id,
        action="python_eval",
        session_id=cfg.session_id,
        payload={"provider": cfg.provider, "repl_name": repl_name},
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
            return _execute_python_memory(cfg.session_id, repl_name, code, eval_token=ctx.token)
        finally:
            dispose_eval_context(ctx.token)
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
            return _execute_python_docker(
                db,
                runtime_thread_id,
                code,
                repl_name=repl_name,
                eval_token=ctx.token,
                timeout_sec=bridge_timeout_sec,
            )
        finally:
            dispose_eval_context(ctx.token)
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

    runtime_thread_id = get_or_create_runtime_thread(
        db,
        caller_thread_id,
        language="bash",
        name=runtime_name,
        reason="bash_repl",
    )
    cfg = get_thread_session_config(db, runtime_thread_id)
    if not cfg.enabled or not cfg.session_id:
        return (
            "Error: persistent session is not enabled for this thread. "
            "Call enable_thread_session(..., provider='memory') for tests or provider='docker'."
        )

    append_session_lifecycle_event(
        db,
        runtime_thread_id,
        action="bash_eval",
        session_id=cfg.session_id,
        payload={"provider": cfg.provider, "repl_name": repl_name},
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
            return _execute_bash_memory(cfg.session_id, repl_name, script, eval_token=ctx.token, timeout_sec=bridge_timeout_sec)
        if cfg.provider == "docker":
            return _execute_bash_docker(
                db,
                runtime_thread_id,
                script,
                repl_name=repl_name,
                eval_token=ctx.token,
                timeout_sec=bridge_timeout_sec,
            )
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
    "docker_session_available",
    "get_or_start_docker_session",
    "get_or_start_docker_session_handle",
    "set_thread_session_config",
    "enable_thread_session",
    "disable_thread_session",
    "append_session_lifecycle_event",
    "execute_python_repl",
    "execute_bash_repl",
    "runtime_thread_label",
    "append_runtime_config",
    "find_runtime_thread",
    "get_or_create_runtime_thread",
]
