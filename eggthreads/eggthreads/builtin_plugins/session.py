from __future__ import annotations

"""Built-in persistent session and REPL tools."""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolRegistry, resolve_tool_timeout_arg


def execute_python_repl_tool(args: Dict[str, Any]) -> str:
    from ..db import ThreadsDB
    from ..session import execute_python_repl

    thread_id = (args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: python_repl requires thread context."
    code = args.get("code", "")
    repl_name = (args.get("repl_name") or "default").strip() or "default"
    runtime_name = (args.get("runtime_name") or "default").strip() or "default"
    timeout_sec = resolve_tool_timeout_arg(args)
    try:
        return execute_python_repl(
            ThreadsDB(),
            thread_id,
            str(code),
            repl_name=repl_name,
            runtime_name=runtime_name,
            timeout_sec=timeout_sec,
        )
    except Exception as e:
        return f"Error: python_repl failed: {e}"


def execute_bash_repl_tool(args: Dict[str, Any]) -> str:
    from ..db import ThreadsDB
    from ..session import execute_bash_repl

    thread_id = (args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: bash_repl requires thread context."
    script = args.get("script", "")
    repl_name = (args.get("repl_name") or "default").strip() or "default"
    runtime_name = (args.get("runtime_name") or "default").strip() or "default"
    timeout_sec = resolve_tool_timeout_arg(args)
    try:
        return execute_bash_repl(
            ThreadsDB(),
            thread_id,
            str(script),
            repl_name=repl_name,
            runtime_name=runtime_name,
            timeout_sec=timeout_sec,
        )
    except Exception as e:
        return f"Error: bash_repl failed: {e}"


def format_session_status(thread_id: str) -> str:
    from ..db import ThreadsDB
    from ..session import find_runtime_thread, get_thread_session_status

    db = ThreadsDB()
    lines: list[str] = []
    st = get_thread_session_status(db, thread_id)
    lines.append("Current thread session:")
    lines.append(f"  Enabled: {st.enabled}")
    lines.append(f"  Provider: {st.provider}")
    lines.append(f"  Session ID: {st.session_id or '(none)'}")
    lines.append(f"  Status: {st.status}")
    lines.append(f"  Share REPL channel: {st.share_repl}")
    if st.container_name:
        lines.append(f"  Container: {st.container_name}")
    if st.message:
        lines.append(f"  Message: {st.message}")
    for language in ("python", "bash"):
        rt = find_runtime_thread(db, thread_id, language=language)
        if rt is None:
            continue
        rst = get_thread_session_status(db, rt.runtime_thread_id)
        lines.append("")
        lines.append(f"Runtime {language} ({rt.runtime_thread_id[-8:]}):")
        lines.append(f"  Session ID: {rst.session_id or '(none)'}")
        lines.append(f"  Provider: {rst.provider}")
        lines.append(f"  Status: {rst.status}")
        lines.append(f"  Share REPL channel: {rst.share_repl}")
        if rst.container_name:
            lines.append(f"  Container: {rst.container_name}")
        if rst.message:
            lines.append(f"  Message: {rst.message}")
    return "\n".join(lines)


def execute_session_status_tool(args: Dict[str, Any]) -> str:
    thread_id = (args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: session_status requires thread context."
    return format_session_status(thread_id)


def resolve_session_targets(thread_id: str, language: str) -> tuple[Any, list[str]]:
    from ..db import ThreadsDB
    from ..session import find_runtime_thread

    db = ThreadsDB()
    targets: list[str] = []
    if language in ("python", "bash"):
        rt = find_runtime_thread(db, thread_id, language=language)
        targets = [rt.runtime_thread_id] if rt is not None else [thread_id]
    elif language in ("runtimes", "runtime", "all"):
        for lang in ("python", "bash"):
            rt = find_runtime_thread(db, thread_id, language=lang)
            if rt is not None:
                targets.append(rt.runtime_thread_id)
        if not targets:
            targets = [thread_id]
    else:
        targets = [thread_id]
    return db, targets


def execute_session_reset_tool(args: Dict[str, Any]) -> str:
    from ..session import reset_thread_session

    thread_id = (args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: session_reset requires thread context."
    language = str(args.get("language") or "").strip().lower()
    db, targets = resolve_session_targets(thread_id, language)
    lines = []
    for target in targets:
        sid = reset_thread_session(db, target, reason="session_reset tool")
        lines.append(f"Reset session for {target[-8:]}: {sid}")
    return "\n".join(lines)


def execute_session_stop_tool(args: Dict[str, Any]) -> str:
    from ..session import stop_thread_session

    thread_id = (args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: session_stop requires thread context."
    language = str(args.get("language") or "").strip().lower()
    db, targets = resolve_session_targets(thread_id, language)
    lines = []
    for target in targets:
        st = stop_thread_session(db, target, reason="session_stop tool")
        lines.append(f"Stop session for {target[-8:]}: {st.status} ({st.session_id or '(none)'})")
    return "\n".join(lines)


def register_session_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="python_repl",
        description="Execute Python code in this thread's persistent Python REPL session.",
        parameters_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute in the persistent REPL."},
                "repl_name": {"type": "string", "description": "Optional REPL channel name (default: default)."},
                "runtime_name": {"type": "string", "description": "Optional runtime child thread name (default: default)."},
                "timeout_sec": {"type": "number", "description": "Maximum seconds to allow this eval and its programmatic eggtools calls before timing out."},
            },
            "required": ["code"],
        },
        impl=execute_python_repl_tool,
    )

    registry.register(
        name="bash_repl",
        description="Execute Bash code in this thread's persistent Bash REPL session.",
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Bash script to execute in the persistent REPL."},
                "repl_name": {"type": "string", "description": "Optional REPL channel name (default: default)."},
                "runtime_name": {"type": "string", "description": "Optional runtime child thread name (default: default)."},
                "timeout_sec": {"type": "number", "description": "Maximum seconds to allow this eval and its programmatic eggtools calls before timing out."},
            },
            "required": ["script"],
        },
        impl=execute_bash_repl_tool,
    )

    registry.register(
        name="session_status",
        description="Show persistent REPL/session status for the current thread and runtime children.",
        parameters_schema={"type": "object", "properties": {}},
        impl=execute_session_status_tool,
    )

    registry.register(
        name="session_reset",
        description="Reset the current thread persistent REPL/session state. Optional language=python|bash|all targets runtime sessions.",
        parameters_schema={
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "Optional target runtime language: python, bash, or all."},
            },
        },
        impl=execute_session_reset_tool,
    )

    registry.register(
        name="session_stop",
        description="Stop the current thread persistent REPL/session without disabling its config. Optional language=python|bash|all targets runtime sessions.",
        parameters_schema={
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "Optional target runtime language: python, bash, or all."},
            },
        },
        impl=execute_session_stop_tool,
    )


@dataclass(frozen=True)
class SessionPlugin:
    name: str = "session"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        register_session_tools(context.tool_registry)


__all__ = [
    "SessionPlugin",
    "execute_bash_repl_tool",
    "execute_python_repl_tool",
    "execute_session_reset_tool",
    "execute_session_status_tool",
    "execute_session_stop_tool",
    "format_session_status",
    "register_session_tools",
    "resolve_session_targets",
]
