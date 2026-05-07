from __future__ import annotations

"""Built-in subprocess execution tools."""

import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..terminal_safety import looks_like_terminal_control_text, sanitize_terminal_text
from ..tools import ToolRegistry, resolve_tool_timeout_arg


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


def _sandboxed_argv(args: Dict[str, Any], base_argv: list[str], *, thread_arg: str) -> tuple[list[str], str | None]:
    from ..db import ThreadsDB

    thread_id = (args.get(thread_arg) or "").strip()
    cwd = None
    if thread_id:
        try:
            from ..api import _ensure_thread_working_directory
            from ..sandbox import get_thread_sandbox_config, wrap_argv_for_sandbox_with_settings

            db = ThreadsDB()
            sb = get_thread_sandbox_config(db, thread_id)
            cwd = _ensure_thread_working_directory(db, thread_id)
            argv = wrap_argv_for_sandbox_with_settings(
                base_argv,
                enabled=sb.enabled,
                settings={**dict(sb.settings or {}), "_egg_thread_context": {"thread_id": thread_id, "db_path": str(db.path)}},
                working_dir=cwd,
                provider=sb.provider,
            )
            return argv, cwd
        except Exception:
            return base_argv, cwd

    from ..sandbox import wrap_argv_for_sandbox

    return wrap_argv_for_sandbox(base_argv), cwd


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
    argv, cwd = _sandboxed_argv(args, ["/bin/bash", "-lc", script], thread_arg="_thread_id")
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
    argv, cwd = _sandboxed_argv(args, ["python3", "-c", script], thread_arg="_thread_id")
    return _run_interruptible_subprocess(
        args,
        argv,
        cwd=cwd,
        timeout_message="--- TIMEOUT ---\nScript timed out after {timeout} seconds",
        interrupt_message="--- INTERRUPTED ---\nScript was interrupted by user",
        empty_message="--- The script executed successfully and produced no output ---",
    )


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
        impl=execute_bash_tool,
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
    "execute_python_tool",
    "register_execution_tools",
]
