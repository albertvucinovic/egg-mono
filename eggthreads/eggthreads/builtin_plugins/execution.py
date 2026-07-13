from __future__ import annotations

"""Built-in subprocess execution tools."""

import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..terminal_safety import looks_like_terminal_control_text, sanitize_terminal_text
from ..tools import ToolCapabilities, ToolContext, ToolExecutionResult, ToolRegistry, resolve_tool_timeout_arg


_BASH_TIMEOUT_TERM_GRACE_SEC = 2.0
_BASH_TIMEOUT_FORCE_GRACE_SEC = 1.0


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
            from ..sandbox import SandboxSetupError, get_thread_sandbox_config, sandbox_unavailable_message, wrap_argv_for_sandbox_with_settings

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
        except SandboxSetupError:
            raise
        except Exception:
            from ..sandbox import SandboxSetupError, sandbox_unavailable_message

            raise SandboxSetupError(sandbox_unavailable_message("unknown", "Sandbox setup failed before execution."))

    from ..sandbox import wrap_argv_for_sandbox

    return wrap_argv_for_sandbox(base_argv), cwd, None, None


def _run_interruptible_subprocess(
    args: Dict[str, Any],
    argv: list[str],
    *,
    cwd: str | None,
    sandbox_provider: str | None = None,
    sandbox_container_name: str | None = None,
    timeout_message: str,
    interrupt_message: str,
    empty_message: str,
) -> str:
    timeout = resolve_tool_timeout_arg(args)
    cancel_check = args.get("_cancel_check")

    def _kill_process_and_container(proc: subprocess.Popen, *, force: bool = False) -> None:
        if sandbox_container_name and sandbox_provider == "docker":
            try:
                from ..sandbox import stop_docker_container

                stop_docker_container(sandbox_container_name, timeout=2)
            except Exception:
                pass
        try:
            import os as _os
            import signal as _signal

            pgid = _os.getpgid(proc.pid)
            _os.killpg(pgid, _signal.SIGKILL if force else _signal.SIGTERM)
        except Exception:
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except Exception:
                pass

    start_time = time.time()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, start_new_session=True)
    def _wait_after_stop() -> None:
        try:
            proc.wait(timeout=_BASH_TIMEOUT_TERM_GRACE_SEC)
            return
        except subprocess.TimeoutExpired:
            _kill_process_and_container(proc, force=True)
        try:
            proc.wait(timeout=_BASH_TIMEOUT_FORCE_GRACE_SEC)
        except Exception:
            pass

    try:
        while True:
            if cancel_check and cancel_check():
                _kill_process_and_container(proc)
                _wait_after_stop()
                return interrupt_message
            if timeout and (time.time() - start_time) >= timeout:
                _kill_process_and_container(proc)
                _wait_after_stop()
                return timeout_message.format(timeout=timeout)
            wait_timeout = 0.1
            if timeout:
                wait_timeout = max(0.0, min(wait_timeout, timeout - (time.time() - start_time)))
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=wait_timeout)
                break
            except subprocess.TimeoutExpired:
                pass
    except Exception as e:
        _kill_process_and_container(proc, force=True)
        try:
            proc.wait(timeout=_BASH_TIMEOUT_FORCE_GRACE_SEC)
        except Exception:
            pass
        return f"--- ERROR ---\n{e}"

    return _format_subprocess_output(stdout_bytes, stderr_bytes, empty_message=empty_message)


def execute_bash_tool(args: Dict[str, Any]) -> str:
    script = args.get("script", "")
    try:
        import os as _os

        argv, cwd, sandbox_provider, sandbox_container_name = _sandboxed_argv(
            args,
            ["/bin/bash", "-lc", script],
            thread_arg="_thread_id",
            container_name=f"egg_tool_{_os.urandom(8).hex()}",
        )
    except Exception as e:
        try:
            from ..sandbox import SandboxSetupError

            if isinstance(e, SandboxSetupError):
                return f"--- SANDBOX ERROR ---\n{e}"
        except Exception:
            pass
        return f"--- ERROR ---\n{e}"
    return _run_interruptible_subprocess(
        args,
        argv,
        cwd=cwd,
        sandbox_provider=sandbox_provider,
        sandbox_container_name=sandbox_container_name,
        timeout_message="--- TIMEOUT ---\nCommand timed out after {timeout} seconds",
        interrupt_message="--- INTERRUPTED ---\nCommand was interrupted by user",
        empty_message="--- The command executed successfully and produced no output ---",
    )


def execute_python_tool(args: Dict[str, Any]) -> str:
    script = args.get("script", "")
    try:
        import os as _os

        argv, cwd, sandbox_provider, sandbox_container_name = _sandboxed_argv(
            args,
            ["python3", "-c", script],
            thread_arg="_thread_id",
            container_name=f"egg_tool_{_os.urandom(8).hex()}",
        )
    except Exception as e:
        try:
            from ..sandbox import SandboxSetupError

            if isinstance(e, SandboxSetupError):
                return f"--- SANDBOX ERROR ---\n{e}"
        except Exception:
            pass
        return f"--- ERROR ---\n{e}"
    return _run_interruptible_subprocess(
        args,
        argv,
        cwd=cwd,
        sandbox_provider=sandbox_provider,
        sandbox_container_name=sandbox_container_name,
        timeout_message="--- TIMEOUT ---\nScript timed out after {timeout} seconds",
        interrupt_message="--- INTERRUPTED ---\nScript was interrupted by user",
        empty_message="--- The script executed successfully and produced no output ---",
    )


def execute_python_tool_context(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Execute Python with full ToolContext so sandbox lookup uses the runner DB."""

    script = args.get("script", "")
    call_args = dict(args)
    if ctx.thread_id and "_thread_id" not in call_args:
        call_args["_thread_id"] = ctx.thread_id
    if ctx.timeout_sec is not None and "_tool_timeout_sec" not in call_args:
        call_args["_tool_timeout_sec"] = ctx.timeout_sec
    if ctx.cancel_check is not None and "_cancel_check" not in call_args:
        call_args["_cancel_check"] = ctx.cancel_check
    try:
        import os as _os

        argv, cwd, sandbox_provider, sandbox_container_name = _sandboxed_argv(
            call_args,
            ["python3", "-c", script],
            thread_arg="_thread_id",
            db=ctx.db,
            container_name=f"egg_tool_{_os.urandom(8).hex()}",
        )
    except Exception as e:
        try:
            from ..sandbox import SandboxSetupError

            if isinstance(e, SandboxSetupError):
                return f"--- SANDBOX ERROR ---\n{e}"
        except Exception:
            pass
        return f"--- ERROR ---\n{e}"
    return _run_interruptible_subprocess(
        call_args,
        argv,
        cwd=cwd,
        sandbox_provider=sandbox_provider,
        sandbox_container_name=sandbox_container_name,
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
    container_name = f"egg_{invoke_id}" if invoke_id else f"egg_tool_{_os.urandom(8).hex()}"
    try:
        argv, cwd, sandbox_provider, sandbox_container_name = _sandboxed_argv(
            {**args, "_thread_id": ctx.thread_id or ""},
            ["/bin/bash", "-lc", script],
            thread_arg="_thread_id",
            db=ctx.db,
            container_name=container_name,
        )
    except Exception as e:
        try:
            from ..sandbox import SandboxSetupError

            if isinstance(e, SandboxSetupError):
                return ToolExecutionResult(f"--- SANDBOX ERROR ---\n{e}", reason="sandbox_error", streamed=ctx.stream is not None)
        except Exception:
            pass
        return ToolExecutionResult(f"--- ERROR ---\n{e}", reason="error", streamed=ctx.stream is not None)

    proc = await _asyncio.create_subprocess_exec(
        *argv,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        preexec_fn=_os.setsid,
        cwd=cwd,
    )
    timed_out = False
    interrupted = False
    terminate_sent = False
    kill_sent = False
    timeout = ctx.timeout_sec
    start = time.time()
    stop_requested_at: float | None = None

    def _kill_process_and_container(*, force: bool = False) -> None:
        if sandbox_container_name and sandbox_provider == "docker":
            try:
                from ..sandbox import stop_docker_container

                stop_docker_container(sandbox_container_name, timeout=2)
            except Exception:
                pass
        try:
            pgid = _os.getpgid(proc.pid)
            _os.killpg(pgid, _signal.SIGKILL if force else _signal.SIGTERM)
        except Exception:
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except Exception:
                pass

    async def _watcher() -> None:
        nonlocal timed_out, interrupted, terminate_sent, kill_sent, stop_requested_at
        while proc.returncode is None:
            await _asyncio.sleep(0.1)
            now = time.time()
            if timeout and (now - start) >= timeout:
                timed_out = True
                if stop_requested_at is None:
                    stop_requested_at = now
                if not terminate_sent:
                    terminate_sent = True
                    _kill_process_and_container()
                elif not kill_sent and (now - stop_requested_at) >= _BASH_TIMEOUT_TERM_GRACE_SEC:
                    kill_sent = True
                    _kill_process_and_container(force=True)
            if ctx.cancel_check and ctx.cancel_check():
                interrupted = True
                if stop_requested_at is None:
                    stop_requested_at = now
                if not terminate_sent:
                    terminate_sent = True
                    _kill_process_and_container()
                elif not kill_sent:
                    kill_sent = True
                    _kill_process_and_container(force=True)

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    stream_buf: list[str] = []
    stream_buf_chars = 0
    stream_last_flush = 0.0
    last_reader_activity = time.monotonic()
    stream_lock = _asyncio.Lock()

    async def _flush_stream(*, force: bool = False) -> bool:
        nonlocal stream_buf, stream_buf_chars, stream_last_flush, interrupted
        if ctx.stream is None:
            return True
        async with stream_lock:
            if not stream_buf:
                return True
            if not force and stream_buf_chars < 4096 and (time.monotonic() - stream_last_flush) < 0.05:
                return True
            text = "".join(stream_buf)
            stream_buf = []
            stream_buf_chars = 0
            stream_last_flush = time.monotonic()
        if not ctx.stream.stream_delta(text):
            interrupted = True
            _kill_process_and_container()
            return False
        return True

    async def _queue_stream(text: str) -> bool:
        nonlocal stream_buf_chars
        if ctx.stream is None or not text:
            return True
        async with stream_lock:
            stream_buf.append(text)
            stream_buf_chars += len(text)
        return await _flush_stream()

    async def _reader(stream, is_stdout: bool) -> None:
        nonlocal interrupted, last_reader_activity
        header_emitted = False
        prefix = "--- STDOUT ---\n" if is_stdout else "--- STDERR ---\n"
        while True:
            try:
                chunk = await stream.read(64 * 1024)
            except Exception:
                break
            if not chunk:
                break
            last_reader_activity = time.monotonic()
            text = chunk.decode(errors="replace")
            if not header_emitted:
                if is_stdout:
                    stdout_buf.append(prefix)
                else:
                    stderr_buf.append(prefix)
                header_emitted = True
                if not await _queue_stream(prefix):
                    break
            if is_stdout:
                stdout_buf.append(text)
            else:
                stderr_buf.append(text)
            if not await _queue_stream(text):
                break
            await _asyncio.sleep(0)

    watcher = _asyncio.create_task(_watcher())
    stdout_task = _asyncio.create_task(_reader(proc.stdout, True))
    stderr_task = _asyncio.create_task(_reader(proc.stderr, False))
    proc_wait_task = _asyncio.create_task(proc.wait())
    proc_reported_exit = False

    def _close_pipe_transports() -> None:
        """Close parent-side pipe transports to unblock reader tasks.

        A bash script may intentionally launch a background daemon and then let
        the shell exit.  If that daemon inherits stdout/stderr, asyncio's pipe
        readers never see EOF even though the command process is done.  Close
        Egg's read side when we decide to stop draining so those inherited file
        descriptors cannot keep the tool state machine alive forever.
        """

        transport = getattr(proc, "_transport", None)
        get_pipe_transport = getattr(transport, "get_pipe_transport", None)
        if callable(get_pipe_transport):
            for fd in (1, 2):
                try:
                    pipe_transport = get_pipe_transport(fd)
                    close = getattr(pipe_transport, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass

    async def _cancel_reader_tasks() -> None:
        _close_pipe_transports()
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        try:
            done, _pending = await _asyncio.wait({stdout_task, stderr_task}, timeout=1.0)
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass
        except BaseException:
            pass

    try:
        while True:
            if proc.returncode is not None:
                proc_reported_exit = True
                break
            done, _pending = await _asyncio.wait(
                {proc_wait_task},
                timeout=0.1,
                return_when=_asyncio.FIRST_COMPLETED,
            )
            if proc_wait_task in done:
                proc_reported_exit = True
                break
            if timed_out or interrupted:
                now = time.time()
                if stop_requested_at is None:
                    stop_requested_at = now
                if not terminate_sent:
                    terminate_sent = True
                    _kill_process_and_container()
                if not kill_sent and (now - stop_requested_at) >= _BASH_TIMEOUT_TERM_GRACE_SEC:
                    kill_sent = True
                    _kill_process_and_container(force=True)
                if kill_sent and (now - stop_requested_at) >= (_BASH_TIMEOUT_TERM_GRACE_SEC + _BASH_TIMEOUT_FORCE_GRACE_SEC):
                    # The process did not report exit even after SIGKILL/container
                    # kill.  Do not leave the runner/tool stream stuck forever;
                    # return a timeout result and let process cleanup best-effort
                    # continue outside the user-visible tool state machine.
                    break
        reader_drain_started = time.monotonic() if proc_reported_exit and not (timed_out or interrupted) else None
        readers_cancelled = False
        while True:
            if stdout_task.done() and stderr_task.done():
                break
            now = time.monotonic()
            if (
                timed_out
                or interrupted
                or (
                    reader_drain_started is not None
                    and (now - last_reader_activity >= 1.0 or now - reader_drain_started >= 5.0)
                )
            ):
                await _cancel_reader_tasks()
                readers_cancelled = True
                break
            await _asyncio.sleep(0.25)
        if not readers_cancelled:
            await _asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        await _flush_stream(force=True)
    finally:
        if not proc_wait_task.done():
            proc_wait_task.cancel()
        try:
            await _asyncio.wait({proc_wait_task}, timeout=1.0)
        except BaseException:
            pass
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
        description="Execute a bash script and return combined stdout/stderr. Use timeout to limit execution time.",
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "The bash script to execute."},
            },
            "required": ["script"],
        },
        impl=execute_bash_tool_streaming,
        accepts_context=True,
        capabilities=ToolCapabilities(supports_streaming=True, supports_cancellation=True),
    )

    registry.register(
        name="python",
        description="Execute a Python script and return combined stdout/stderr. Use timeout to limit execution time.",
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "The Python script to execute."},
            },
        },
        impl=execute_python_tool_context,
        accepts_context=True,
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
    "execute_python_tool_context",
    "register_execution_tools",
]
