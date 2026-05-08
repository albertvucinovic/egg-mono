from __future__ import annotations

"""Built-in subprocess execution tools."""

import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..terminal_safety import looks_like_terminal_control_text, sanitize_terminal_text
from ..tools import ToolCapabilities, ToolContext, ToolExecutionResult, ToolRegistry, resolve_tool_timeout_arg


def _called_from_different_thread(db: Any) -> bool:
    """Return True when using db.conn in this thread would violate sqlite."""
    # sqlite3 exposes no stable public owner-thread id.  A tiny read probe is
    # the least invasive way to preserve safe fallback behavior for legacy
    # sync tools that may run in ToolRegistry.execute_async() worker threads.
    try:
        conn = getattr(db, "conn", None)
        if conn is None:
            return False
        conn.execute("SELECT 1")
        return False
    except Exception as e:
        return "thread" in str(e).lower() and "sqlite" in str(e).lower()


def _thread_db(db: Any):
    from ..db import ThreadsDB

    if db is not None and not _called_from_different_thread(db):
        return db
    db_path = getattr(db, "path", None)
    return ThreadsDB(db_path) if db_path is not None else ThreadsDB()


def _format_subprocess_output(
    stdout_bytes: bytes | str | None,
    stderr_bytes: bytes | str | None,
    *,
    empty_message: str,
) -> str:
    out = ""
    stdout = stdout_bytes.decode(errors="replace") if isinstance(stdout_bytes, (bytes, bytearray)) else (stdout_bytes or "")
    stderr = stderr_bytes.decode(errors="replace") if isinstance(stderr_bytes, (bytes, bytearray)) else (stderr_bytes or "")
    if stdout:
        body = sanitize_terminal_text(stdout.strip()) if looks_like_terminal_control_text(stdout) else stdout.strip()
        out += f"--- STDOUT ---\n{body}\n"
    if stderr:
        body = sanitize_terminal_text(stderr.strip()) if looks_like_terminal_control_text(stderr) else stderr.strip()
        out += f"--- STDERR ---\n{body}\n"
    return out.strip() or empty_message


def _sanitize_combined_output(text: str) -> str:
    """Apply the same terminal-safety cleanup used by sync subprocess tools."""
    if not text:
        return ""
    return sanitize_terminal_text(text.strip()) if looks_like_terminal_control_text(text) else text.strip()


def _sandboxed_argv(
    args: Dict[str, Any],
    base_argv: list[str],
    *,
    thread_arg: str,
    db: Any = None,
    container_name: str | None = None,
) -> tuple[list[str], str | None, str | None, str | None]:
    thread_id = (args.get(thread_arg) or "").strip()
    cwd = None
    if thread_id:
        try:
            from ..api import _ensure_thread_working_directory
            from ..db import ThreadsDB
            from ..sandbox import get_thread_sandbox_config, wrap_argv_for_sandbox_with_settings

            db_for_thread = _thread_db(db)
            sb = get_thread_sandbox_config(db_for_thread, thread_id)
            cwd = _ensure_thread_working_directory(db_for_thread, thread_id)
            argv = wrap_argv_for_sandbox_with_settings(
                base_argv,
                enabled=sb.enabled,
                settings={**dict(sb.settings or {}), "_egg_thread_context": {"thread_id": thread_id, "db_path": str(db_for_thread.path)}},
                working_dir=cwd,
                provider=sb.provider,
                container_name=container_name,
            )
            return argv, cwd, sb.provider, container_name if sb.enabled and sb.provider == "docker" else None
        except Exception:
            return base_argv, cwd, None, None

    from ..sandbox import wrap_argv_for_sandbox

    return wrap_argv_for_sandbox(base_argv), cwd, None, None


def _run_interruptible_subprocess(
    args: Dict[str, Any],
    argv: list[str],
    *,
    cwd: str | None,
    timeout_message: str,
    interrupt_message: str,
    empty_message: str,
) -> str:
    timeout = resolve_tool_timeout_arg(args)
    cancel_check = args.get("_cancel_check")

    start_time = time.time()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    try:
        while proc.poll() is None:
            if cancel_check and cancel_check():
                proc.kill()
                proc.wait()
                return interrupt_message
            if timeout and (time.time() - start_time) >= timeout:
                proc.kill()
                proc.wait()
                return timeout_message.format(timeout=timeout)
            time.sleep(0.1)
        stdout_bytes, stderr_bytes = proc.communicate()
    except Exception as e:
        proc.kill()
        proc.wait()
        return f"--- ERROR ---\n{e}"

    return _format_subprocess_output(stdout_bytes, stderr_bytes, empty_message=empty_message)


def execute_bash_tool(args: Dict[str, Any]) -> str:
    script = args.get("script", "")
    argv, cwd, _, _ = _sandboxed_argv(args, ["/bin/bash", "-lc", script], thread_arg="_thread_id")
    return _run_interruptible_subprocess(
        args,
        argv,
        cwd=cwd,
        timeout_message="--- TIMEOUT ---\nCommand timed out after {timeout} seconds",
        interrupt_message="--- INTERRUPTED ---\nCommand was interrupted by user",
        empty_message="--- The command executed successfully and produced no output ---",
    )


def execute_python_tool(args: Dict[str, Any]) -> str:
    script = args.get("script", "")
    argv, cwd, _, _ = _sandboxed_argv(args, ["python3", "-c", script], thread_arg="_thread_id")
    return _run_interruptible_subprocess(
        args,
        argv,
        cwd=cwd,
        timeout_message="--- TIMEOUT ---\nScript timed out after {timeout} seconds",
        interrupt_message="--- INTERRUPTED ---\nScript was interrupted by user",
        empty_message="--- The script executed successfully and produced no output ---",
    )


async def execute_bash_tool_streaming(args: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    """Execute bash with live streaming/cancellation via ToolContext hooks."""
    import asyncio as _asyncio
    import os as _os
    import signal as _signal

    script = args.get("script", "")
    invoke_id = ctx.invoke_id or ""
    container_name = f"egg_{invoke_id}" if invoke_id else None
    argv, cwd, sandbox_provider, sandbox_container_name = _sandboxed_argv(
        {**args, "_thread_id": ctx.thread_id or ""},
        ["/bin/bash", "-lc", script],
        thread_arg="_thread_id",
        db=ctx.db,
        container_name=container_name,
    )

    proc = await _asyncio.create_subprocess_exec(
        *argv,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        preexec_fn=_os.setsid,
        cwd=cwd,
    )
    timed_out = False
    interrupted = False
    timeout = ctx.timeout_sec
    start = time.time()

    def _kill_process_and_container() -> None:
        if sandbox_container_name and sandbox_provider == "docker":
            try:
                from ..sandbox import stop_docker_container

                stop_docker_container(sandbox_container_name, timeout=2)
            except Exception:
                pass
        try:
            pgid = _os.getpgid(proc.pid)
            _os.killpg(pgid, _signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    async def _watcher() -> None:
        nonlocal timed_out, interrupted
        while proc.returncode is None:
            await _asyncio.sleep(0.1)
            if timeout and (time.time() - start) >= timeout:
                timed_out = True
                _kill_process_and_container()
                return
            if ctx.cancel_check and ctx.cancel_check():
                interrupted = True
                _kill_process_and_container()
                return

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []

    async def _reader(stream, is_stdout: bool) -> None:
        nonlocal interrupted
        header_emitted = False
        prefix = "--- STDOUT ---\n" if is_stdout else "--- STDERR ---\n"
        while True:
            try:
                chunk = await stream.readline()
            except Exception:
                break
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            if not header_emitted:
                if is_stdout:
                    stdout_buf.append(prefix)
                else:
                    stderr_buf.append(prefix)
                header_emitted = True
                if ctx.stream is not None and not ctx.stream.stream_delta(prefix):
                    interrupted = True
                    _kill_process_and_container()
                    break
            if is_stdout:
                stdout_buf.append(text)
            else:
                stderr_buf.append(text)
            if ctx.stream is not None and not ctx.stream.stream_delta(text):
                interrupted = True
                _kill_process_and_container()
                break
            await _asyncio.sleep(0)

    watcher = _asyncio.create_task(_watcher())
    stdout_task = _asyncio.create_task(_reader(proc.stdout, True))
    stderr_task = _asyncio.create_task(_reader(proc.stderr, False))
    try:
        await proc.wait()
        await _asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    finally:
        watcher.cancel()
        try:
            await watcher
        except BaseException:
            pass

    full_result = _sanitize_combined_output("".join(stdout_buf) + "".join(stderr_buf)) or "--- The command executed successfully and produced no output ---"
    if timed_out:
        full_result = f"--- TIMEOUT ---\nCommand timed out after {timeout} seconds.\n\n" + full_result
        return ToolExecutionResult(full_result, reason="timeout", streamed=ctx.stream is not None)
    elif interrupted:
        full_result = "--- INTERRUPTED ---\nCommand was interrupted by user\n\n" + full_result
        return ToolExecutionResult(full_result, reason="interrupted", streamed=ctx.stream is not None)
    return ToolExecutionResult(full_result, reason="success", streamed=ctx.stream is not None)


def register_execution_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="bash",
        description="Execute a bash script and return combined stdout/stderr. Use timeout_sec to limit execution time.",
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "The bash script to execute."},
                "timeout_sec": {"type": "number", "description": "Maximum seconds to allow the script to run before killing it."},
            },
            "required": ["script"],
        },
        impl=execute_bash_tool_streaming,
        accepts_context=True,
        capabilities=ToolCapabilities(supports_streaming=True, supports_cancellation=True),
    )

    registry.register(
        name="python",
        description="Execute a Python script and return combined stdout/stderr. Use timeout_sec to limit execution time.",
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "The Python script to execute."},
                "timeout_sec": {"type": "number", "description": "Maximum seconds to allow the script to run before killing it."},
            },
        },
        impl=execute_python_tool,
        capabilities=ToolCapabilities(supports_cancellation=True),
    )


@dataclass(frozen=True)
class ExecutionPlugin:
    name: str = "execution"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        register_execution_tools(context.tool_registry)


__all__ = [
    "ExecutionPlugin",
    "execute_bash_tool",
    "execute_bash_tool_streaming",
    "execute_python_tool",
    "register_execution_tools",
]
