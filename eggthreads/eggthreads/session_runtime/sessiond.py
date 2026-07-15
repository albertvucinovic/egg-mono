from __future__ import annotations

"""Container-side session daemon for explicit RLM Docker sessions."""

import argparse
import ast
import contextlib
import io
import json
import multiprocessing as mp
import os
import select
import signal
import subprocess
import sys
import time
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


PY_REPLS: Dict[str, Dict[str, Any]] = {}
PY_WORKERS: Dict[str, tuple[mp.Process, Any]] = {}
BASH_REPLS: Dict[str, subprocess.Popen] = {}
ACTIVE_EVALS: Dict[str, Dict[str, Any]] = {}
# Eval membership, per-channel queue membership, and the running handoff form
# one state machine.  Channel conditions deliberately share this re-entrant
# lock so claiming a same-channel request cannot deadlock against the worker
# that is promoting or removing the current queue head.
ACTIVE_EVALS_LOCK = threading.RLock()
CHANNEL_CONDITIONS: Dict[str, threading.Condition] = {}
CHANNEL_QUEUES: Dict[str, List[str]] = {}
CHANNEL_ACTIVITY: Dict[str, Dict[str, Any]] = {}
CHANNEL_REAPING: set[str] = set()
CHANNEL_IDLE_TIMEOUT_SEC: Optional[float] = None
DAEMON_GENERATION = uuid.uuid4().hex
DAEMON_STARTED_AT = time.time()
LAST_ACTIVITY_AT = DAEMON_STARTED_AT
STATUS_PATH: Optional[Path] = None
STATUS_WRITE_LOCK = threading.Lock()


def parse_positive_timeout(value: Any) -> Optional[float]:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return None
    if not (timeout > 0 and timeout < float("inf")):
        return None
    return timeout


def _channel_key(language: str, channel: str) -> str:
    return f"{language}:{channel}"


def _touch_channel_locked(
    language: str,
    channel: str,
    *,
    now: Optional[float] = None,
    clear_reap: bool = True,
) -> None:
    key = _channel_key(language, channel)
    record = CHANNEL_ACTIVITY.setdefault(key, {})
    record["last_activity_at"] = float(time.time() if now is None else now)
    if clear_reap:
        record.pop("reaped_at", None)
        record.pop("reap_reason", None)


def touch_channel(language: str, channel: str, *, now: Optional[float] = None) -> None:
    with ACTIVE_EVALS_LOCK:
        _touch_channel_locked(language, channel, now=now)


def _forget_channel_if_reset(language: str, channel: str) -> None:
    key = _channel_key(language, channel)
    with ACTIVE_EVALS_LOCK:
        if not CHANNEL_QUEUES.get(key):
            CHANNEL_ACTIVITY.pop(key, None)


def _wait_for_channel_reap_locked(channel_key: str) -> None:
    while channel_key in CHANNEL_REAPING:
        # The reaper reserves under ACTIVE_EVALS_LOCK, tears down outside it,
        # then notifies. Admission cannot register a same-channel request in
        # the vulnerable interval.
        condition = CHANNEL_CONDITIONS.setdefault(
            channel_key, threading.Condition(ACTIVE_EVALS_LOCK),
        )
        condition.wait(0.05)


def _python_worker_loop(conn) -> None:
    """Persistent per-REPL Python worker.

    Keeping Python state inside this worker gives us both persistence and a
    killable timeout boundary: sessiond can terminate the whole worker process
    if one eval runs too long, then recreate a fresh REPL on the next eval.
    """

    try:
        os.setsid()
    except Exception:
        pass
    try:
        conn.send({"ready": True, "pgid": os.getpgrp()})
    except Exception:
        return
    globs: Dict[str, Any] = {"__name__": "__egg_repl__"}
    while True:
        try:
            req = conn.recv()
        except EOFError:
            break
        except BaseException:
            break
        if not isinstance(req, dict):
            continue
        if req.get("op") == "stop":
            break
        code = str(req.get("code") or "")
        bridge_dir = Path(str(req.get("bridge_dir") or "/egg-bridge"))
        token = str(req.get("token") or "")
        runtime_dir = Path(str(req.get("runtime_dir") or "/egg-runtime"))
        thread_context_json = req.get("thread_context_json") if isinstance(req.get("thread_context_json"), str) else None
        host_owner_id = str(req.get("host_owner_id") or "")
        eval_request_id = str(req.get("eval_request_id") or "")
        old_owner = os.environ.get("EGG_HOST_OWNER_ID")
        old_eval_request = os.environ.get("EGG_EVAL_REQUEST_ID")
        os.environ["EGG_HOST_OWNER_ID"] = host_owner_id
        os.environ["EGG_EVAL_REQUEST_ID"] = eval_request_id
        try:
            output = _execute_python_inline(code, globs, bridge_dir, token, runtime_dir, thread_context_json)
            conn.send({"ok": True, "output": output})
        except BaseException as e:
            stderr = io.StringIO()
            traceback.print_exc(file=stderr)
            try:
                conn.send({"ok": False, "output": format_output("", stderr.getvalue() or f"{type(e).__name__}: {e}")})
            except Exception:
                break
        finally:
            if old_owner is None:
                os.environ.pop("EGG_HOST_OWNER_ID", None)
            else:
                os.environ["EGG_HOST_OWNER_ID"] = old_owner
            if old_eval_request is None:
                os.environ.pop("EGG_EVAL_REQUEST_ID", None)
            else:
                os.environ["EGG_EVAL_REQUEST_ID"] = old_eval_request


def _get_python_worker(repl_name: str) -> tuple[mp.Process, Any]:
    existing = PY_WORKERS.get(repl_name)
    if existing is not None:
        proc, conn = existing
        if proc.is_alive():
            return proc, conn
        try:
            conn.close()
        except Exception:
            pass
        PY_WORKERS.pop(repl_name, None)
    parent_conn, child_conn = mp.Pipe(duplex=True)
    proc = mp.Process(target=_python_worker_loop, args=(child_conn,), daemon=True)
    proc.start()
    try:
        child_conn.close()
    except Exception:
        pass
    try:
        if not parent_conn.poll(5.0):
            raise RuntimeError("Python worker did not become ready")
        ready = parent_conn.recv()
        if not (isinstance(ready, dict) and ready.get("ready")):
            raise RuntimeError("Python worker returned an invalid ready message")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            parent_conn.close()
        except Exception:
            pass
        raise
    PY_WORKERS[repl_name] = (proc, parent_conn)
    touch_channel("python", repl_name)
    return proc, parent_conn


def _kill_python_worker(repl_name: str, *, preserve_activity: bool = False) -> None:
    existing = PY_WORKERS.pop(repl_name, None)
    if existing is None:
        return
    proc, conn = existing
    try:
        conn.close()
    except Exception:
        pass
    if proc.is_alive():
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.join(1.0)
    except Exception:
        pass
    if not preserve_activity:
        _forget_channel_if_reset("python", repl_name)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _daemon_status_payload(now: Optional[float] = None) -> Dict[str, Any]:
    """Snapshot daemon/eval/channel health under the shared state lock."""

    heartbeat_at = float(now if now is not None else time.time())
    with ACTIVE_EVALS_LOCK:
        active_requests: List[Dict[str, Any]] = []
        active_snapshot = list(sorted(ACTIVE_EVALS.items()))
        queue_snapshot = {key: list(value) for key, value in CHANNEL_QUEUES.items()}
        python_channels = list(PY_WORKERS)
        bash_channels = [channel for channel, proc in BASH_REPLS.items() if proc.poll() is None]
        for req_id, active in active_snapshot:
            request = active.get("payload") if isinstance(active.get("payload"), dict) else {}
            active_requests.append({
                "request_id": req_id,
                "language": str(request.get("language") or "python"),
                "channel": str(request.get("channel") or request.get("repl_name") or "default"),
                "state": "running" if active.get("running") else "queued",
                "created_at": request.get("created_at"),
                "cancel_reason": active.get("cancel_reason"),
            })
        channel_state: Dict[str, Any] = {}
        all_channel_keys = {
            key for key, activity in CHANNEL_ACTIVITY.items()
            if activity.get("reaped_at") is not None
        } | set(queue_snapshot) | set(CHANNEL_REAPING)
        all_channel_keys.update(f"python:{channel}" for channel in python_channels)
        all_channel_keys.update(f"bash:{channel}" for channel in bash_channels)
        for channel_key in sorted(all_channel_keys):
            queue = queue_snapshot.get(channel_key, [])
            running_id = next(
                (req_id for req_id in queue if ACTIVE_EVALS.get(req_id, {}).get("running")),
                None,
            )
            activity = CHANNEL_ACTIVITY.get(channel_key, {})
            details: Dict[str, Any] = {
                "state": (
                    "reaping" if channel_key in CHANNEL_REAPING
                    else ("busy" if queue else ("reaped" if activity.get("reaped_at") else "ready"))
                ),
                "last_activity_at": activity.get("last_activity_at"),
            }
            if queue:
                details.update(
                    running_request_id=running_id,
                    queued_request_ids=[req_id for req_id in queue if req_id != running_id],
                )
            if activity.get("reaped_at") is not None:
                details["reaped_at"] = activity.get("reaped_at")
                details["reap_reason"] = activity.get("reap_reason")
            channel_state[channel_key] = details
    return {
        "protocol_version": 2,
        "daemon_generation": DAEMON_GENERATION,
        "started_at": DAEMON_STARTED_AT,
        "heartbeat_at": heartbeat_at,
        "last_activity_at": LAST_ACTIVITY_AT,
        "active_requests": active_requests,
        "channel_state": channel_state,
    }


def write_daemon_status(path: Optional[Path] = None, *, activity: bool = False) -> None:
    """Atomically publish a heartbeat and the current request/channel snapshot."""

    global LAST_ACTIVITY_AT
    if activity:
        LAST_ACTIVITY_AT = time.time()
    target = path or STATUS_PATH
    if target is None:
        return
    with STATUS_WRITE_LOCK:
        atomic_write_json(target, _daemon_status_payload())


def format_output(stdout_text: str, stderr_text: str) -> str:
    out = ""
    stdout_text = (stdout_text or "").strip()
    stderr_text = (stderr_text or "").strip()
    if stdout_text:
        out += f"--- STDOUT ---\n{stdout_text}\n"
    if stderr_text:
        out += f"--- STDERR ---\n{stderr_text}\n"
    return out.strip() or "--- The Python REPL executed successfully and produced no output ---"


def format_bash_output(output_text: str) -> str:
    output_text = (output_text or "").strip()
    return f"--- STDOUT ---\n{output_text}" if output_text else "--- The Bash REPL executed successfully and produced no output ---"


def _message_text_for_context_file(value: Any) -> str:
    if isinstance(value, list):
        rendered: List[str] = []
        for part in value:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                rendered.append(part.get("text") or "")
            elif part.get("type") == "attachment":
                filename = part.get("filename") or "(unnamed)"
                presentation = part.get("presentation") or "file"
                mime_type = part.get("mime_type") or "application/octet-stream"
                size = part.get("size_bytes")
                sha = str(part.get("sha256") or "")[:8] or "unknown"
                rendered.append(f"[Attachment: {presentation} {filename} {mime_type} {size} B sha256:{sha}]")
        if rendered:
            return "\n".join(rendered)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _message_search_blob(message: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("msg_id", "role", "name", "tool_call_id"):
        value = message.get(key)
        if value is not None:
            parts.append(str(value))
    parts.append(_message_text_for_context_file(message.get("content", "")))
    return "\n".join(parts).lower()


def install_thread_context_helpers(globs: Dict[str, Any], context: Dict[str, Any]) -> None:
    def reload_thread_context() -> Dict[str, Any]:
        # Docker REPL hydration is host-provided per eval.  A fresh eval will
        # rebuild from the event DB before running user code; within one eval,
        # keep the current hydrated copy rather than reaching into Egg internals
        # from inside the container.
        return globs.get("thread_context", context)

    def _current_context() -> Dict[str, Any]:
        current = globs.get("thread_context")
        return current if isinstance(current, dict) else context

    def search_thread(query: Any, role: Any = None, in_prompt: Any = None) -> List[Dict[str, Any]]:
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
        ctx = _current_context()
        by_id = ctx.get("messages_by_id") if isinstance(ctx, dict) else None
        if not isinstance(by_id, dict):
            return None
        return by_id.get(str(msg_id))

    def print_message(msg_id: Any) -> None:
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


def _execute_python_inline(
    code: str,
    globs: Dict[str, Any],
    bridge_dir: Path,
    token: str,
    runtime_dir: Path,
    thread_context_json: str | None = None,
) -> str:
    if thread_context_json:
        try:
            context = json.loads(thread_context_json)
            if isinstance(context, dict):
                install_thread_context_helpers(globs, context)
        except Exception:
            pass
    sys.path.insert(0, str(runtime_dir)) if str(runtime_dir) not in sys.path else None
    old_bridge = os.environ.get("EGG_BRIDGE_DIR")
    old_token = os.environ.get("EGG_EVAL_TOKEN")
    os.environ["EGG_BRIDGE_DIR"] = str(bridge_dir)
    os.environ["EGG_EVAL_TOKEN"] = token
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        tree = ast.parse(code or "", mode="exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                body = tree.body[:-1]
                expr = tree.body[-1].value
                if body:
                    exec(compile(ast.Module(body=body, type_ignores=[]), "<egg-docker-python-repl>", "exec"), globs, globs)
                value = eval(compile(ast.Expression(expr), "<egg-docker-python-repl>", "eval"), globs, globs)
                if value is not None:
                    print(repr(value))
            else:
                exec(compile(tree, "<egg-docker-python-repl>", "exec"), globs, globs)
    except Exception:
        traceback.print_exc(file=stderr)
    finally:
        if old_bridge is None:
            os.environ.pop("EGG_BRIDGE_DIR", None)
        else:
            os.environ["EGG_BRIDGE_DIR"] = old_bridge
        if old_token is None:
            os.environ.pop("EGG_EVAL_TOKEN", None)
        else:
            os.environ["EGG_EVAL_TOKEN"] = old_token
    return format_output(stdout.getvalue(), stderr.getvalue())


def execute_python(
    code: str,
    repl_name: str,
    bridge_dir: Path,
    token: str,
    runtime_dir: Path,
    thread_context_json: str | None = None,
    timeout_sec: float | None = None,
    cancel_check: Any = None,
    host_owner_id: str = "",
    eval_request_id: str = "",
) -> str:
    repl_key = repl_name or "default"
    try:
        timeout = float(timeout_sec) if timeout_sec is not None else None
    except Exception:
        timeout = None
    if timeout is not None and timeout <= 0:
        timeout = None

    try:
        proc, conn = _get_python_worker(repl_key)
    except Exception as e:
        return f"Error: Python worker failed to start: {type(e).__name__}: {e}"
    try:
        conn.send({
            "op": "eval",
            "code": code or "",
            "bridge_dir": str(bridge_dir),
            "token": token,
            "runtime_dir": str(runtime_dir),
            "thread_context_json": thread_context_json,
            "host_owner_id": host_owner_id,
            "eval_request_id": eval_request_id,
        })
    except Exception as e:
        _kill_python_worker(repl_key)
        return f"Error: Python worker failed: {type(e).__name__}: {e}"

    start = time.monotonic()
    while True:
        if cancel_check is not None and cancel_check():
            _kill_python_worker(repl_key)
            return "--- INTERRUPTED ---\nPython REPL eval was cancelled; this Python channel was reset."
        if conn.poll(0.05):
            try:
                payload = conn.recv()
            except Exception as e:
                _kill_python_worker(repl_key)
                return f"Error: Python worker failed: {type(e).__name__}: {e}"
            if isinstance(payload, dict) and payload.get("ok"):
                return str(payload.get("output") or "")
            if isinstance(payload, dict):
                return str(payload.get("output") or "Error: Python worker failed.")
            return "Error: Python worker returned an invalid result."
        if not proc.is_alive():
            _kill_python_worker(repl_key)
            return "Error: Python worker exited before returning a result."
        if timeout is not None and (time.monotonic() - start) >= timeout:
            _kill_python_worker(repl_key)
            return f"--- TIMEOUT ---\nPython REPL timed out after {timeout} seconds"


def _bash_proc(repl_name: str, bridge_dir: Path, token: str, runtime_dir: Path) -> subprocess.Popen:
    proc = BASH_REPLS.get(repl_name)
    if proc is not None and proc.poll() is None:
        return proc
    env = os.environ.copy()
    env["EGG_BRIDGE_DIR"] = str(bridge_dir)
    env["EGG_EVAL_TOKEN"] = token
    env["PATH"] = f"{runtime_dir}:{env.get('PATH', '')}"
    proc = subprocess.Popen(
        ["bash", "--noprofile", "--norc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    BASH_REPLS[repl_name] = proc
    touch_channel("bash", repl_name)
    return proc


def execute_bash(
    script: str,
    repl_name: str,
    bridge_dir: Path,
    token: str,
    runtime_dir: Path,
    timeout_sec: float | None = None,
    cancel_check: Any = None,
    host_owner_id: str = "",
    eval_request_id: str = "",
) -> str:
    repl_name = repl_name or "default"
    proc = _bash_proc(repl_name, bridge_dir, token, runtime_dir)
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("Bash REPL pipes are not available")
    sentinel = f"__EGG_DONE_{uuid.uuid4().hex}__"
    # Update per-eval bridge token/path inside the persistent shell.
    prelude = (
        f"export EGG_BRIDGE_DIR={json.dumps(str(bridge_dir))}\n"
        f"export EGG_EVAL_TOKEN={json.dumps(token)}\n"
        f"export EGG_HOST_OWNER_ID={json.dumps(host_owner_id)}\n"
        f"export EGG_EVAL_REQUEST_ID={json.dumps(eval_request_id)}\n"
        f"export PATH={json.dumps(str(runtime_dir) + ':' + os.environ.get('PATH', ''))}\n"
    )
    proc.stdin.write(prelude)
    proc.stdin.write(script or "")
    proc.stdin.write(f"\n__egg_status=$?; printf '\\n{sentinel}:%s\\n' \"$__egg_status\"\n")
    proc.stdin.flush()

    start = time.monotonic()
    output = ""
    while True:
        if cancel_check is not None and cancel_check():
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            BASH_REPLS.pop(repl_name, None)
            return "--- INTERRUPTED ---\nBash REPL eval was cancelled; this Bash channel was reset."
        if timeout_sec is not None and (time.monotonic() - start) >= timeout_sec:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            BASH_REPLS.pop(repl_name, None)
            return f"--- TIMEOUT ---\nBash REPL timed out after {timeout_sec} seconds"
        ready, _, _ = select.select([proc.stdout], [], [], 0.05)
        if not ready:
            continue
        # TextIOWrapper.readline() may prefetch several pipe lines into its own
        # buffer. Reading only one line after select can then wait forever
        # because the sentinel is buffered in Python while the OS fd is empty.
        # Drain the available pipe bytes directly and parse the sentinel from
        # the accumulated text instead.
        try:
            chunk = os.read(proc.stdout.fileno(), 65536).decode(errors="replace")
        except Exception:
            chunk = ""
        if not chunk and proc.poll() is not None:
            BASH_REPLS.pop(repl_name, None)
            return format_bash_output(output)
        output += chunk
        marker = f"\n{sentinel}:"
        marker_at = output.find(marker)
        if marker_at >= 0:
            return format_bash_output(output[:marker_at])


def claim(path: Path) -> Path | None:
    claimed = path.with_suffix(path.suffix + ".processing")
    try:
        os.replace(path, claimed)
        return claimed
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _response_path(bridge_dir: Path, req_id: str) -> Path:
    return bridge_dir / f"eval_{req_id}.res.json"


def _cancel_path(bridge_dir: Path, req_id: str) -> Path:
    return bridge_dir / f"eval_{req_id}.cancel.json"


def _cancel_ack_path(bridge_dir: Path, req_id: str) -> Path:
    return bridge_dir / f"eval_{req_id}.cancel.ack.json"


def _write_terminal_response(bridge_dir: Path, req_id: str, payload: Dict[str, Any]) -> bool:
    res_path = _response_path(bridge_dir, req_id)
    with ACTIVE_EVALS_LOCK:
        active = ACTIVE_EVALS.get(req_id)
        if (active is not None and active.get("terminal_written")) or res_path.exists():
            return False
        if active is not None and active["cancel"].is_set():
            request = active.get("payload") if isinstance(active.get("payload"), dict) else {}
            language = str(request.get("language") or payload.get("language") or "python")
            channel = str(request.get("channel") or request.get("repl_name") or payload.get("channel") or "default")
            cancel_reason = str(active.get("cancel_reason") or "interrupted")
            running = bool(active.get("running"))
            if cancel_reason == "timeout":
                reason = "timeout"
                message = f"{language.title()} REPL eval timed out"
            else:
                reason = "cancelled"
                message = f"{language.title()} REPL eval was cancelled"
            if running:
                message += f"; this {language.title()} channel was reset."
            else:
                message += " before execution."
            payload = {
                "ok": True,
                "reason": reason,
                "output": f"--- {reason.upper()} ---\n{message}",
                "channel": channel,
                "language": language,
                "host_owner_id": str(request.get("host_owner_id") or ""),
            }
        atomic_write_json(res_path, {
            "protocol_version": 2,
            "request_id": req_id,
            "daemon_generation": DAEMON_GENERATION,
            "completed_at": time.time(),
            **payload,
        })
        if active is not None:
            active["terminal_written"] = True
        return True


def _request_cancelled(req_id: str) -> bool:
    with ACTIVE_EVALS_LOCK:
        active = ACTIVE_EVALS.get(req_id)
        return bool(active and active["cancel"].is_set())


def _run_claimed_eval(
    claimed: Path,
    req_id: str,
    payload: Dict[str, Any],
    bridge_dir: Path,
    runtime_dir: Path,
) -> None:
    language = str(payload.get("language") or "python")
    channel = str(payload.get("channel") or payload.get("repl_name") or "default")
    channel_key = f"{language}:{channel}"
    with ACTIVE_EVALS_LOCK:
        active = ACTIVE_EVALS[req_id]
        channel_condition = CHANNEL_CONDITIONS[channel_key]
    try:
        with channel_condition:
            while CHANNEL_QUEUES[channel_key][0] != req_id and not active["cancel"].is_set():
                channel_condition.wait(0.05)
            # Mark the head request running before releasing the same condition
            # used for queue handoff.  Cancellation can now either see queued
            # state and avoid a kill, or running state and target this request;
            # it cannot race through the gap before execute_* starts.
            with ACTIVE_EVALS_LOCK:
                active["running"] = not active["cancel"].is_set()
        write_daemon_status(activity=True)
        if active["cancel"].is_set():
            output = f"--- INTERRUPTED ---\n{language.title()} REPL eval was cancelled before execution."
            reason = "cancelled"
        elif language == "python":
            output = execute_python(
                str(payload.get("code") or ""), channel, bridge_dir,
                str(payload.get("token") or ""), runtime_dir,
                str(payload.get("thread_context_json") or "") or None,
                payload.get("timeout_sec"),
                cancel_check=active["cancel"].is_set,
                host_owner_id=str(payload.get("host_owner_id") or ""),
                eval_request_id=req_id,
            )
            reason = "cancelled" if output.startswith("--- INTERRUPTED ---") else ("timeout" if output.startswith("--- TIMEOUT ---") else "success")
        elif language == "bash":
            output = execute_bash(
                str(payload.get("code") or payload.get("script") or ""), channel,
                bridge_dir, str(payload.get("token") or ""), runtime_dir,
                payload.get("timeout_sec"),
                cancel_check=active["cancel"].is_set,
                host_owner_id=str(payload.get("host_owner_id") or ""),
                eval_request_id=req_id,
            )
            reason = "cancelled" if output.startswith("--- INTERRUPTED ---") else ("timeout" if output.startswith("--- TIMEOUT ---") else "success")
        else:
            raise ValueError(f"Unsupported language: {language}")
        _write_terminal_response(bridge_dir, req_id, {
            "ok": True, "output": output, "reason": reason,
            "channel": channel, "language": language,
            "host_owner_id": str(payload.get("host_owner_id") or ""),
        })
    except Exception as e:
        _write_terminal_response(bridge_dir, req_id, {
            "ok": False, "reason": "error", "error": f"{type(e).__name__}: {e}",
            "channel": channel, "language": language,
        })
    finally:
        with ACTIVE_EVALS_LOCK:
            _touch_channel_locked(language, channel)
            ACTIVE_EVALS.pop(req_id, None)
            with channel_condition:
                queue = CHANNEL_QUEUES.get(channel_key, [])
                if req_id in queue:
                    queue.remove(req_id)
                channel_condition.notify_all()
                if not queue:
                    CHANNEL_QUEUES.pop(channel_key, None)
                    CHANNEL_CONDITIONS.pop(channel_key, None)
        try:
            claimed.unlink()
        except Exception:
            pass
        write_daemon_status(activity=True)


def process_eval_request(req_path: Path, bridge_dir: Path, runtime_dir: Path) -> None:
    claimed = claim(req_path)
    if claimed is None:
        return
    req_id = req_path.name[len("eval_"):-len(".req.json")]
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Docker eval request must be a JSON object")
        protocol_version = int(payload.get("protocol_version") or 1)
        payload_request_id = str(payload.get("request_id") or payload.get("id") or req_id)
        if protocol_version >= 2 and payload_request_id != req_id:
            raise ValueError("Docker eval request ID does not match its bridge filename")
        requested_generation = str(payload.get("daemon_generation") or "")
        if protocol_version >= 2 and requested_generation and requested_generation != DAEMON_GENERATION:
            _write_terminal_response(bridge_dir, req_id, {
                "ok": True,
                "reason": "daemon_restarted",
                "output": (
                    "--- INTERRUPTED ---\nDocker session daemon generation changed "
                    "before this eval was claimed; it was not executed."
                ),
            })
            try:
                claimed.unlink()
            except Exception:
                pass
            return
    except Exception as e:
        _write_terminal_response(bridge_dir, req_id, {"ok": False, "reason": "error", "error": f"{type(e).__name__}: {e}"})
        try:
            claimed.unlink()
        except Exception:
            pass
        return
    cancel_event = threading.Event()
    language = str(payload.get("language") or "python")
    channel = str(payload.get("channel") or payload.get("repl_name") or "default")
    channel_key = f"{language}:{channel}"
    with ACTIVE_EVALS_LOCK:
        _wait_for_channel_reap_locked(channel_key)
        _touch_channel_locked(language, channel)
        ACTIVE_EVALS[req_id] = {
            "cancel": cancel_event,
            "cancel_reason": None,
            "payload": payload,
            "running": False,
            "terminal_written": False,
        }
        channel_condition = CHANNEL_CONDITIONS.setdefault(
            channel_key,
            threading.Condition(ACTIVE_EVALS_LOCK),
        )
        # Queue creation/removal is serialized by ACTIVE_EVALS_LOCK.  Mutations
        # also take the channel condition so a finishing request cannot remove
        # an apparently empty queue while a newly claimed request is appended.
        with channel_condition:
            CHANNEL_QUEUES.setdefault(channel_key, []).append(req_id)
    write_daemon_status(activity=True)
    if _cancel_path(bridge_dir, req_id).exists():
        service_cancel_requests(bridge_dir)
    thread = threading.Thread(
        target=_run_claimed_eval,
        args=(claimed, req_id, payload, bridge_dir, runtime_dir),
        name=f"egg-eval-{req_id[:8]}",
        daemon=True,
    )
    thread.start()


def _cancel_active_channel(active: Dict[str, Any]) -> None:
    if not active.get("running"):
        return
    payload = active.get("payload") if isinstance(active.get("payload"), dict) else {}
    language = str(payload.get("language") or "python")
    channel = str(payload.get("channel") or payload.get("repl_name") or "default")
    if language == "python":
        _kill_python_worker(channel)
        return
    if language == "bash":
        proc = BASH_REPLS.pop(channel, None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def service_cancel_requests(bridge_dir: Path) -> None:
    for cancel_path in sorted(bridge_dir.glob("eval_*.cancel.json")):
        req_id = cancel_path.name[len("eval_"):-len(".cancel.json")]
        try:
            cancel_payload = json.loads(cancel_path.read_text(encoding="utf-8"))
            if not isinstance(cancel_payload, dict):
                cancel_payload = {}
        except Exception:
            cancel_payload = {}
        active_to_cancel: Optional[Dict[str, Any]] = None
        with ACTIVE_EVALS_LOCK:
            # Durable completion wins a cancellation race. Once the response
            # exists, never reset a channel whose result is already terminal.
            if _response_path(bridge_dir, req_id).exists():
                state = "already_finished"
            else:
                active = ACTIVE_EVALS.get(req_id)
                if active is None:
                    # The cancel file can arrive before sessiond has claimed
                    # the request. Keep it for the next control-loop pass so
                    # side-effectful code is never started after cancellation.
                    continue
                request = active.get("payload") if isinstance(active.get("payload"), dict) else {}
                request_owner = str(request.get("host_owner_id") or "")
                cancel_owner = str(cancel_payload.get("host_owner_id") or "")
                protocol_version = int(request.get("protocol_version") or 1)
                if protocol_version >= 2 and request_owner and cancel_owner != request_owner:
                    state = "owner_mismatch"
                else:
                    active["cancel_reason"] = str(cancel_payload.get("reason") or "interrupted")
                    active["cancel"].set()
                    active_to_cancel = active
                    state = "accepted"
                    # Keep this request registered until its current process has
                    # been targeted.  Otherwise completion can dequeue it and a
                    # successor can reuse the persistent channel before this
                    # kill runs, causing cancellation to reset the wrong eval.
                    _cancel_active_channel(active_to_cancel)
        if active_to_cancel is not None:
            _write_terminal_response(bridge_dir, req_id, {})
        atomic_write_json(_cancel_ack_path(bridge_dir, req_id), {
            "protocol_version": 2,
            "request_id": req_id,
            "daemon_generation": DAEMON_GENERATION,
            "state": state,
            "reason": str(cancel_payload.get("reason") or "interrupted"),
            "acknowledged_at": time.time(),
        })
        try:
            cancel_path.unlink()
        except Exception:
            pass
        write_daemon_status(activity=True)


def _terminate_bash_channel(channel: str, *, preserve_activity: bool = False) -> None:
    proc = BASH_REPLS.pop(channel, None)
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=1.0)
    except Exception:
        pass
    if not preserve_activity:
        _forget_channel_if_reset("bash", channel)


def reap_idle_channels(
    *,
    timeout_sec: Optional[float] = None,
    now: Optional[float] = None,
    before_teardown: Any = None,
) -> List[str]:
    """Reap idle interpreter channels without racing channel admission."""

    threshold = CHANNEL_IDLE_TIMEOUT_SEC if timeout_sec is None else parse_positive_timeout(timeout_sec)
    if threshold is None:
        return []
    observed_at = float(time.time() if now is None else now)
    candidates: List[str] = []
    with ACTIVE_EVALS_LOCK:
        live_keys = {
            *(_channel_key("python", channel) for channel in PY_WORKERS),
            *(_channel_key("bash", channel) for channel, proc in BASH_REPLS.items() if proc.poll() is None),
        }
        for channel_key in sorted(live_keys):
            activity = CHANNEL_ACTIVITY.get(channel_key, {})
            last_activity = activity.get("last_activity_at")
            queue = CHANNEL_QUEUES.get(channel_key, [])
            cancelling = any(
                bool(ACTIVE_EVALS.get(req_id, {}).get("cancel_reason"))
                or bool(
                    ACTIVE_EVALS.get(req_id, {}).get("cancel") is not None
                    and ACTIVE_EVALS[req_id]["cancel"].is_set()
                )
                for req_id in queue
            )
            if (
                channel_key in CHANNEL_REAPING
                or queue
                or cancelling
                or not isinstance(last_activity, (int, float))
                or observed_at - float(last_activity) <= threshold
            ):
                continue
            CHANNEL_REAPING.add(channel_key)
            candidates.append(channel_key)
    reaped: List[str] = []
    for channel_key in candidates:
        language, channel = channel_key.split(":", 1)
        try:
            if before_teardown is not None:
                before_teardown(channel_key)
            if language == "python":
                _kill_python_worker(channel, preserve_activity=True)
            elif language == "bash":
                _terminate_bash_channel(channel, preserve_activity=True)
            else:
                continue
            reaped.append(channel_key)
        finally:
            completed_at = float(time.time() if now is None else now)
            with ACTIVE_EVALS_LOCK:
                activity = CHANNEL_ACTIVITY.setdefault(channel_key, {})
                activity["last_activity_at"] = completed_at
                activity["reaped_at"] = completed_at
                activity["reap_reason"] = f"idle_timeout:{threshold:g}s"
                CHANNEL_REAPING.discard(channel_key)
                condition = CHANNEL_CONDITIONS.get(channel_key)
                if condition is not None:
                    condition.notify_all()
                    if not CHANNEL_QUEUES.get(channel_key):
                        CHANNEL_CONDITIONS.pop(channel_key, None)
    if reaped:
        write_daemon_status(activity=True)
    return reaped


def recover_stale_claims(bridge_dir: Path) -> None:
    for claimed in sorted(bridge_dir.glob("eval_*.req.json.processing")):
        name = claimed.name
        req_id = name[len("eval_"):-len(".req.json.processing")]
        _write_terminal_response(bridge_dir, req_id, {
            "ok": True, "reason": "daemon_restarted",
            "output": "--- INTERRUPTED ---\nDocker session daemon restarted while this eval was in progress; it was not replayed.",
        })
        try:
            claimed.unlink()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", default="/egg-bridge")
    parser.add_argument("--runtime-dir", default="/egg-runtime")
    parser.add_argument("--poll-sec", type=float, default=0.05)
    parser.add_argument("--channel-idle-timeout-sec", type=float, default=None)
    args = parser.parse_args()
    bridge_dir = Path(args.bridge_dir)
    runtime_dir = Path(args.runtime_dir)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    global STATUS_PATH, CHANNEL_IDLE_TIMEOUT_SEC
    STATUS_PATH = bridge_dir / "sessiond_status.json"
    CHANNEL_IDLE_TIMEOUT_SEC = parse_positive_timeout(args.channel_idle_timeout_sec)
    atomic_write_json(bridge_dir / "sessiond_generation.json", {
        "protocol_version": 2, "daemon_generation": DAEMON_GENERATION,
        "started_at": DAEMON_STARTED_AT,
    })
    recover_stale_claims(bridge_dir)
    write_daemon_status(activity=True)
    heartbeat_interval = max(0.1, min(1.0, args.poll_sec * 10.0))
    next_heartbeat = time.monotonic() + heartbeat_interval
    while True:
        service_cancel_requests(bridge_dir)
        for req in sorted(bridge_dir.glob("eval_*.req.json")):
            process_eval_request(req, bridge_dir, runtime_dir)
        if time.monotonic() >= next_heartbeat:
            reap_idle_channels()
            write_daemon_status()
            next_heartbeat = time.monotonic() + heartbeat_interval
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
