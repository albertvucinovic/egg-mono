from __future__ import annotations

"""Container-side session daemon for explicit RLM Docker sessions."""

import argparse
import ast
import contextlib
import io
import json
import os
import select
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


PY_REPLS: Dict[str, Dict[str, Any]] = {}
BASH_REPLS: Dict[str, subprocess.Popen] = {}


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


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


def execute_python(code: str, repl_name: str, bridge_dir: Path, token: str, runtime_dir: Path, thread_context_json: str | None = None) -> str:
    globs = PY_REPLS.setdefault(repl_name or "default", {"__name__": "__egg_repl__"})
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
    )
    BASH_REPLS[repl_name] = proc
    return proc


def execute_bash(script: str, repl_name: str, bridge_dir: Path, token: str, runtime_dir: Path, timeout_sec: float | None = None) -> str:
    repl_name = repl_name or "default"
    proc = _bash_proc(repl_name, bridge_dir, token, runtime_dir)
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("Bash REPL pipes are not available")
    sentinel = f"__EGG_DONE_{uuid.uuid4().hex}__"
    # Update per-eval bridge token/path inside the persistent shell.
    prelude = (
        f"export EGG_BRIDGE_DIR={json.dumps(str(bridge_dir))}\n"
        f"export EGG_EVAL_TOKEN={json.dumps(token)}\n"
        f"export PATH={json.dumps(str(runtime_dir) + ':' + os.environ.get('PATH', ''))}\n"
    )
    proc.stdin.write(prelude)
    proc.stdin.write(script or "")
    proc.stdin.write(f"\n__egg_status=$?; printf '\\n{sentinel}:%s\\n' \"$__egg_status\"\n")
    proc.stdin.flush()

    start = time.time()
    lines: list[str] = []
    while True:
        if timeout_sec is not None and (time.time() - start) >= timeout_sec:
            proc.kill()
            BASH_REPLS.pop(repl_name, None)
            return f"--- TIMEOUT ---\nBash REPL timed out after {timeout_sec} seconds"
        ready, _, _ = select.select([proc.stdout], [], [], 0.05)
        if not ready:
            continue
        line = proc.stdout.readline()
        if line == "" and proc.poll() is not None:
            BASH_REPLS.pop(repl_name, None)
            return format_bash_output("".join(lines))
        if line.startswith(sentinel + ":"):
            return format_bash_output("".join(lines))
        lines.append(line)


def claim(path: Path) -> Path | None:
    claimed = path.with_suffix(path.suffix + ".processing")
    try:
        os.replace(path, claimed)
        return claimed
    except FileNotFoundError:
        return None
    except Exception:
        return None


def process_eval_request(req_path: Path, bridge_dir: Path, runtime_dir: Path) -> None:
    claimed = claim(req_path)
    if claimed is None:
        return
    req_id = req_path.name[len("eval_"):-len(".req.json")]
    res_path = bridge_dir / f"eval_{req_id}.res.json"
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        language = str(payload.get("language") or "python")
        if language == "python":
            output = execute_python(
                str(payload.get("code") or ""),
                str(payload.get("repl_name") or "default"),
                bridge_dir,
                str(payload.get("token") or ""),
                runtime_dir,
                str(payload.get("thread_context_json") or "") or None,
            )
        elif language == "bash":
            output = execute_bash(
                str(payload.get("code") or payload.get("script") or ""),
                str(payload.get("repl_name") or "default"),
                bridge_dir,
                str(payload.get("token") or ""),
                runtime_dir,
                payload.get("timeout_sec"),
            )
        else:
            raise ValueError(f"Unsupported language: {payload.get('language')}")
        atomic_write_json(res_path, {"ok": True, "output": output})
    except Exception as e:
        atomic_write_json(res_path, {"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        try:
            claimed.unlink()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", default="/egg-bridge")
    parser.add_argument("--runtime-dir", default="/egg-runtime")
    parser.add_argument("--poll-sec", type=float, default=0.05)
    args = parser.parse_args()
    bridge_dir = Path(args.bridge_dir)
    runtime_dir = Path(args.runtime_dir)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    while True:
        for req in sorted(bridge_dir.glob("eval_*.req.json")):
            process_eval_request(req, bridge_dir, runtime_dir)
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
