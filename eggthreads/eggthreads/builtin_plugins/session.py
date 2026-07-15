from __future__ import annotations

"""Built-in persistent session and REPL tools/commands."""

import math
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolRegistry, resolve_tool_timeout_arg


def _thread_db(db: Any = None):
    from ..db import ThreadsDB

    return db if db is not None else ThreadsDB()


def _context_thread_id(args: Dict[str, Any], ctx: Any = None) -> str:
    """Return the authoritative thread id for a REPL tool call."""

    thread_id = getattr(ctx, "thread_id", None) if ctx is not None else None
    if not thread_id:
        thread_id = args.get("_thread_id")
    return str(thread_id or "").strip()


def _context_db(ctx: Any = None):
    db = getattr(ctx, "db", None) if ctx is not None else None
    db_path = getattr(db, "path", None)
    if db_path is not None:
        from ..db import ThreadsDB

        # REPL tools are synchronous implementations, so execute_async() runs
        # them in a worker thread.  The runner's ctx.db connection belongs to
        # the scheduler/event-loop thread; open a fresh connection to the same
        # SQLite file instead of crossing thread boundaries.
        return ThreadsDB(db_path)
    return _thread_db(db)


def _context_timeout_sec(args: Dict[str, Any], ctx: Any = None) -> float | None:
    timeout_sec = resolve_tool_timeout_arg(args)
    if timeout_sec is None and ctx is not None:
        timeout_sec = getattr(ctx, "timeout_sec", None)
    return timeout_sec


def execute_python_repl_tool(args: Dict[str, Any], ctx: Any = None) -> str:
    from ..session import execute_python_repl

    thread_id = _context_thread_id(args, ctx)
    if not thread_id:
        return "Error: python_repl requires thread context."
    code = args.get("code", "")
    repl_name = (args.get("repl_name") or "default").strip() or "default"
    runtime_name = (args.get("runtime_name") or "default").strip() or "default"
    timeout_sec = _context_timeout_sec(args, ctx)
    try:
        return execute_python_repl(
            _context_db(ctx),
            thread_id,
            str(code),
            repl_name=repl_name,
            runtime_name=runtime_name,
            timeout_sec=timeout_sec,
            cancel_check=getattr(ctx, "cancel_check", None),
        )
    except Exception as e:
        return f"Error: python_repl failed: {e}"


def execute_bash_repl_tool(args: Dict[str, Any], ctx: Any = None) -> str:
    from ..session import execute_bash_repl

    thread_id = _context_thread_id(args, ctx)
    if not thread_id:
        return "Error: bash_repl requires thread context."
    script = args.get("script", "")
    repl_name = (args.get("repl_name") or "default").strip() or "default"
    runtime_name = (args.get("runtime_name") or "default").strip() or "default"
    timeout_sec = _context_timeout_sec(args, ctx)
    try:
        return execute_bash_repl(
            _context_db(ctx),
            thread_id,
            str(script),
            repl_name=repl_name,
            runtime_name=runtime_name,
            timeout_sec=timeout_sec,
            cancel_check=getattr(ctx, "cancel_check", None),
        )
    except Exception as e:
        return f"Error: bash_repl failed: {e}"


def _append_session_health_details(lines: list[str], status: Any) -> None:
    if status.container_name:
        lines.append(f"  Container: {status.container_name}")
    if getattr(status, "daemon_generation", None):
        lines.append(f"  Daemon generation: {status.daemon_generation}")
    if getattr(status, "active_requests", ()):
        lines.append(f"  Active requests: {len(status.active_requests)}")
    if getattr(status, "channel_state", {}):
        channels = ", ".join(
            f"{name}={details.get('state', 'unknown') if isinstance(details, dict) else details}"
            for name, details in sorted(status.channel_state.items())
        )
        lines.append(f"  Channels: {channels}")
    if getattr(status, "last_activity", None) is not None:
        lines.append(f"  Last activity: {status.last_activity}")
    if getattr(status, "reason", None):
        lines.append(f"  Reason: {status.reason}")
    limits = getattr(status, "resource_limits", {})
    if limits:
        if limits.get("memory_bytes") is not None:
            lines.append(f"  Memory limit: {limits['memory_bytes']} bytes")
        if limits.get("memory_swap_bytes") is not None:
            lines.append(f"  Memory + swap limit: {limits['memory_swap_bytes']} bytes (swap disabled)")
        if limits.get("pids_limit") is not None:
            lines.append(f"  PID limit: {limits['pids_limit']}")
    if status.message:
        lines.append(f"  Message: {status.message}")


def format_session_status(thread_id: str, db: Any = None) -> str:
    import eggthreads as _eggthreads

    db = _thread_db(db)
    lines: list[str] = []
    st = _eggthreads.get_thread_session_status(db, thread_id)
    lines.append("Current thread session:")
    lines.append(f"  Enabled: {st.enabled}")
    lines.append(f"  Provider: {st.provider}")
    lines.append(f"  Session ID: {st.session_id or '(none)'}")
    lines.append(f"  Status: {st.status}")
    lines.append(f"  Share REPL channel: {getattr(st, 'share_repl', False)}")
    _append_session_health_details(lines, st)
    for language in ("python", "bash"):
        rt = _eggthreads.find_runtime_thread(db, thread_id, language=language)
        if rt is None:
            continue
        rst = _eggthreads.get_thread_session_status(db, rt.runtime_thread_id)
        lines.append("")
        lines.append(f"Runtime {language} ({rt.runtime_thread_id[-8:]}):")
        lines.append(f"  Session ID: {rst.session_id or '(none)'}")
        lines.append(f"  Provider: {rst.provider}")
        lines.append(f"  Status: {rst.status}")
        lines.append(f"  Share REPL channel: {getattr(rst, 'share_repl', False)}")
        _append_session_health_details(lines, rst)
    return "\n".join(lines)


def resolve_session_targets(thread_id: str, language: str, db: Any = None) -> tuple[Any, list[str]]:
    import eggthreads as _eggthreads

    db = _thread_db(db)
    targets: list[str] = []
    if language in ("python", "bash"):
        rt = _eggthreads.find_runtime_thread(db, thread_id, language=language)
        targets = [rt.runtime_thread_id] if rt is not None else [thread_id]
    elif language in ("runtimes", "runtime", "all"):
        for lang in ("python", "bash"):
            rt = _eggthreads.find_runtime_thread(db, thread_id, language=lang)
            if rt is not None:
                targets.append(rt.runtime_thread_id)
        if not targets:
            targets = [thread_id]
    else:
        targets = [thread_id]
    return db, targets


def _command_log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _command_target(context: Any, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _command_log(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def session_status_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _command_target(context, "sessionStatus")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        text = format_session_status(thread_id, db=db)
        _command_log(context, "Session status (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Session Status", text, border_style="magenta")
        else:
            _command_log(context, text)
    except Exception as e:
        _command_log(context, f"/sessionStatus error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def session_on_command(context: Any, arg: str):
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _command_target(context, "sessionOn")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    parsed = parse_args(arg or "")
    provider = parsed.get("provider") or parsed.positional_or(0, "docker") or "docker"
    image = parsed.get("image") or "egg-rlm-session"
    share_raw = parsed.get("share_with_children", parsed.get("share", "false")) or "false"
    share = str(share_raw).strip().lower() in ("1", "true", "yes", "on")
    share_repl_raw = parsed.get("share_repl", "false") or "false"
    share_repl = str(share_repl_raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        sid = _eggthreads.enable_thread_session(
            db,
            thread_id,
            provider=provider,
            image=image,
            share_with_children_default=share,
            share_repl=share_repl,
            reason="/sessionOn",
        )
        st = _eggthreads.get_thread_session_status(db, thread_id)
        _command_log(context, f"Session enabled: provider={provider} session={sid[-8:] if sid else '(none)'} status={st.status}")
    except Exception as e:
        _command_log(context, f"/sessionOn error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def session_off_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _command_target(context, "sessionOff")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        _eggthreads.disable_thread_session(db, thread_id, reason="/sessionOff")
        _command_log(context, "Session disabled for this thread.")
    except Exception as e:
        _command_log(context, f"/sessionOff error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def session_stop_command(context: Any, arg: str):
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _command_target(context, "sessionStop")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    parsed = parse_args(arg or "")
    language = (parsed.get("language") or parsed.positional_or(0, "") or "").strip().lower()
    try:
        db, targets = resolve_session_targets(thread_id, language, db=db)
        statuses = [_eggthreads.stop_thread_session(db, target, reason="/sessionStop") for target in targets]
        summary = ", ".join(f"{st.session_id or '(none)'}:{st.status}" for st in statuses)
        _command_log(context, f"Session stop requested: {summary}")
    except Exception as e:
        _command_log(context, f"/sessionStop error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def session_reset_command(context: Any, arg: str):
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _command_target(context, "sessionReset")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    parsed = parse_args(arg or "")
    language = (parsed.get("language") or parsed.positional_or(0, "") or "").strip().lower()
    try:
        db, targets = resolve_session_targets(thread_id, language, db=db)
        sids = [_eggthreads.reset_thread_session(db, target, reason="/sessionReset") for target in targets]
        _command_log(context, f"Session reset: {', '.join(sid[-8:] for sid in sids if sid) or '(none)'}")
    except Exception as e:
        _command_log(context, f"/sessionReset error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def session_cleanup_command(context: Any, arg: str):
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads
    from ..session import _parse_duration_seconds  # type: ignore

    target = _command_target(context, "sessionCleanup")
    if target is None:
        return CommandResult(clear_input=False)
    db, _thread_id = target
    parsed = parse_args(arg or "")
    mode = (parsed.positional_or(0, "dry-run") or "dry-run").strip().lower()
    apply = mode in ("apply", "clean", "remove")
    older_than = parsed.get("older_than") or parsed.get("olderThan")
    try:
        older_than_sec = _parse_duration_seconds(older_than)
        if older_than is not None and (
            older_than_sec is None or not math.isfinite(older_than_sec) or older_than_sec <= 0
        ):
            raise ValueError("older_than must be a positive duration such as 1h")
        removed = _eggthreads.cleanup_thread_sessions(
            db,
            provider_name="docker",
            dry_run=not apply,
            older_than_sec=older_than_sec,
        )
        if not removed:
            _command_log(context, "Session doctor found no Egg-owned resources to report.")
            return CommandResult(clear_input=True)
        lines = []
        for item in removed:
            status = str(item.get("action") or "skipped")
            reason = str(item.get("reason") or "unspecified")
            suffix = f" ({item.get('error')})" if item.get("error") else ""
            lines.append(f"{item.get('kind', 'resource')} {item.get('name')}: {status} — {reason}{suffix}")
        text = "\n".join(lines)
        _command_log(context, f"Session doctor reported {len(removed)} resource(s); mode={'apply' if apply else 'dry-run'}.")
        if context.console_print_block is not None:
            context.console_print_block("Session Cleanup", text, border_style="magenta")
        else:
            _command_log(context, text)
    except Exception as e:
        _command_log(context, f"/sessionCleanup error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def _enqueue_repl_command(context: Any, arg: str, *, tool_name: str, arg_name: str, command_name: str, origin: str):
    import eggthreads as _eggthreads
    from ..command_catalog import CommandResult

    target = _command_target(context, command_name)
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    text = arg or ""
    if not text.strip():
        _command_log(context, f"Usage: /{command_name} <{'python code' if tool_name == 'python_repl' else 'bash script'}>")
        return CommandResult(clear_input=False)
    try:
        tcid = _eggthreads.enqueue_user_tool_call(
            db,
            thread_id,
            tool_name,
            {arg_name: text},
            content=f"/{command_name} {text}",
            hidden=True,
            keep_user_turn=True,
            origin=origin,
            auto_approve=True,
            approval_reason=f"Approved /{command_name} command",
        )
        _eggthreads.create_snapshot(db, thread_id)
        _command_log(context, f"{'Python' if tool_name == 'python_repl' else 'Bash'} REPL queued as tool call {tcid[-8:]}; scheduler will execute it.")
        if context.start_scheduler is not None:
            context.start_scheduler(thread_id)
    except Exception as e:
        _command_log(context, f"/{command_name} error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True, start_schedulers=(thread_id,))


def python_repl_command(context: Any, arg: str):
    return _enqueue_repl_command(
        context,
        arg,
        tool_name="python_repl",
        arg_name="code",
        command_name="pythonRepl",
        origin="ui_python_repl",
    )


def bash_repl_command(context: Any, arg: str):
    return _enqueue_repl_command(
        context,
        arg,
        tool_name="bash_repl",
        arg_name="script",
        command_name="bashRepl",
        origin="ui_bash_repl",
    )


def register_session_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("sessionStatus", session_status_command, category="session", usage="/sessionStatus", description="Show persistent session status."))
    registry.register(CommandSpec("sessionOn", session_on_command, category="session", usage="/sessionOn [provider=docker|memory]", description="Enable persistent sessions."))
    registry.register(CommandSpec("sessionOff", session_off_command, category="session", usage="/sessionOff", description="Disable persistent sessions."))
    registry.register(CommandSpec("sessionStop", session_stop_command, category="session", usage="/sessionStop [python|bash|all]", description="Stop session runtimes."))
    registry.register(CommandSpec("sessionReset", session_reset_command, category="session", usage="/sessionReset [python|bash|all]", description="Reset session runtimes."))
    registry.register(CommandSpec("sessionCleanup", session_cleanup_command, category="session", usage="/sessionCleanup [dry-run|apply] [older_than=1h]", description="Diagnose or explicitly clean stale Egg session resources."))
    registry.register(CommandSpec("pythonRepl", python_repl_command, category="session", usage="/pythonRepl <code>", description="Run code in the persistent Python REPL."))
    registry.register(CommandSpec("bashRepl", bash_repl_command, category="session", usage="/bashRepl <script>", description="Run script in the persistent bash REPL."))


def register_session_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="python_repl",
        description=(
            "Execute Python code in this thread's persistent Python REPL session. "
            "The REPL is automatically hydrated with thread_context plus aliases "
            "all_messages, current_prompt_messages, older_messages_not_in_prompt, "
            "messages_by_id, messages_by_role, user_messages, assistant_messages, "
            "tool_messages, compactions, and context_files. Use search_thread(query, "
            "role=None, in_prompt=None), get_message(msg_id), print_message(msg_id), "
            "and reload_thread_context() when exact transcript details are needed; "
            "hidden/local-only content is excluded."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute in the persistent REPL."},
                "repl_name": {"type": "string", "description": "Optional REPL channel name (default: default)."},
                "runtime_name": {"type": "string", "description": "Optional runtime child thread name (default: default)."},
            },
            "required": ["code"],
        },
        impl=execute_python_repl_tool,
        accepts_context=True,
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
            },
            "required": ["script"],
        },
        impl=execute_bash_repl_tool,
        accepts_context=True,
    )


@dataclass(frozen=True)
class SessionPlugin:
    name: str = "session"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_session_tools(context.tool_registry)
        if context.command_registry is not None:
            register_session_commands(context.command_registry)


__all__ = [
    "SessionPlugin",
    "bash_repl_command",
    "execute_bash_repl_tool",
    "execute_python_repl_tool",
    "format_session_status",
    "python_repl_command",
    "register_session_commands",
    "register_session_tools",
    "resolve_session_targets",
    "session_cleanup_command",
    "session_off_command",
    "session_on_command",
    "session_reset_command",
    "session_status_command",
    "session_stop_command",
]
