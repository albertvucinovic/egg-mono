from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from .terminal_safety import looks_like_terminal_control_text, sanitize_terminal_text


class ToolRegistry:
    """Simple registry for OpenAI function-call compatible tools.

    - tools_spec() returns the JSON schema list to pass to the LLM
    - execute(name, arguments) dispatches to the registered callable
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, description: str, parameters_schema: Dict[str, Any], impl: Callable[[Dict[str, Any]], Any], local_only: bool = False):
        """Register a tool.

        Args:
            name: Tool name used in tool_calls.
            description: Human-readable description for the LLM.
            parameters_schema: JSONSchema for the tool arguments.
            impl: Callable implementing the tool. Receives a dict of args.
            local_only: If True, the tool is *not* exposed to the LLM via
                tools_spec(), but can still be executed via execute(). This is
                useful for UI-only helpers like spawn_agent or wait that should
                not be called directly by the model.
        """
        self._tools[name] = {
            "spec": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters_schema,
                }
            },
            "impl": impl,
            "local_only": local_only,
        }

    def tools_spec(self) -> List[Dict[str, Any]]:
        """Return the list of tool specs to expose to the LLM.

        Tools marked local_only=True are omitted so they can be used by the
        UI (RA3 user commands, etc.) without being surfaced as model tools.
        """
        return [d["spec"] for d in self._tools.values() if not d.get("local_only")]

    def execute(self, name: str, arguments: Any, **context: Any) -> Any:
        entry = self._tools.get(name)
        if not entry:
            raise KeyError(f"Unknown tool: {name}")
        impl = entry["impl"]
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except Exception:
                args = {"_raw": arguments}
        elif isinstance(arguments, dict):
            # Work on a shallow copy so we can safely inject context
            # keys (e.g. _thread_id) without mutating the caller's dict.
            args = dict(arguments)
        else:
            args = {"_arg": arguments}

        # Inject the current thread id into arguments for tools that need
        # to know "who called me" (e.g. spawn_agent). We use a reserved
        # key name to avoid colliding with user-provided arguments.
        thread_id = context.get("thread_id")
        if thread_id and "parent_thread_id" not in args and "_thread_id" not in args:
            args["_thread_id"] = thread_id

        # Similarly, propagate the calling thread's model key so other
        # tools can inherit or inspect it when needed. Spawn tools no
        # longer use this implicit override for model selection; child
        # threads should inherit from their parent thread directly.
        init_m = context.get("initial_model_key")
        if init_m and "initial_model_key" not in args and "_initial_model_key" not in args:
            args["_initial_model_key"] = init_m

        # Propagate tool timeout for subprocess-based tools
        tool_timeout = context.get("tool_timeout_sec")
        if tool_timeout is not None and "_tool_timeout_sec" not in args:
            args["_tool_timeout_sec"] = tool_timeout

        # Propagate cancel check callback for interruptible tools
        cancel_check = context.get("cancel_check")
        if cancel_check is not None and "_cancel_check" not in args:
            args["_cancel_check"] = cancel_check

        return impl(args)


# Default tools similar to chat.sh
def create_default_tools() -> ToolRegistry:
    """Create a ToolRegistry with the default set of tools.

    Returns a registry pre-populated with common tools:
    - bash: Execute shell commands
    - python: Execute Python scripts
    - javascript: Browser JavaScript execution (placeholder)
    - spawn_agent: Create child threads for delegation
    - spawn_agent_auto: Create auto-approved child threads
    - replace_between: File text replacement
    - web_search: Web search via the configured backend (SearXNG by default)
    - fetch_url: Fetch and extract readable markdown for a URL
    - wait: Synchronize on child thread completion

    Returns:
        ToolRegistry with default tools registered.
    """
    import asyncio, subprocess, sys, os, json as _json, time as _time
    from io import StringIO
    from pathlib import Path

    reg = ToolRegistry()

    # bash
    def _bash(args: Dict[str, Any]):
        from .sandbox import get_thread_sandbox_config, wrap_argv_for_sandbox_with_settings
        from .api import get_thread_working_directory
        from .db import ThreadsDB
        import subprocess
        import time as _time

        script = args.get('script', '')
        # Timeout priority: LLM-specified > RunnerConfig > None
        llm_timeout = args.get('timeout_sec')
        config_timeout = args.get('_tool_timeout_sec')
        try:
            timeout = float(llm_timeout) if llm_timeout is not None else config_timeout
        except (ValueError, TypeError):
            timeout = config_timeout
        # Cancel check callback - returns True if command should be cancelled
        cancel_check = args.get('_cancel_check')
        # Mirror the async runner: build an explicit argv and optionally
        # wrap it in the sandbox instead of relying on shell=True.
        base_argv = ['/bin/bash', '-lc', script]

        # Honour per-thread sandbox settings when available.
        tid = (args.get('_thread_id') or '').strip()
        cwd = None
        if tid:
            try:
                db = ThreadsDB()
                sb = get_thread_sandbox_config(db, tid)
                from .api import _ensure_thread_working_directory
                cwd = _ensure_thread_working_directory(db, tid)
                argv = wrap_argv_for_sandbox_with_settings(base_argv, enabled=sb.enabled, settings=sb.settings, working_dir=cwd, provider=sb.provider)
            except Exception:
                argv = base_argv
        else:
            # No thread context: default behaviour (use default policy).
            from .sandbox import wrap_argv_for_sandbox
            argv = wrap_argv_for_sandbox(base_argv)

        # Use Popen for interruptible execution
        start_time = _time.time()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
        try:
            while proc.poll() is None:
                # Check for cancellation (e.g., Ctrl+C in UI)
                if cancel_check and cancel_check():
                    proc.kill()
                    proc.wait()
                    return "--- INTERRUPTED ---\nCommand was interrupted by user"
                # Check for timeout
                if timeout and (_time.time() - start_time) >= timeout:
                    proc.kill()
                    proc.wait()
                    return f"--- TIMEOUT ---\nCommand timed out after {timeout} seconds"
                _time.sleep(0.1)  # Poll interval
            stdout_bytes, stderr_bytes = proc.communicate()
        except Exception as e:
            proc.kill()
            proc.wait()
            return f"--- ERROR ---\n{e}"

        out = ''
        stdout = stdout_bytes.decode(errors='replace') if isinstance(stdout_bytes, (bytes, bytearray)) else (stdout_bytes or "")
        stderr = stderr_bytes.decode(errors='replace') if isinstance(stderr_bytes, (bytes, bytearray)) else (stderr_bytes or "")
        if stdout:
            body = sanitize_terminal_text(stdout.strip()) if looks_like_terminal_control_text(stdout) else stdout.strip()
            out += f"--- STDOUT ---\n{body}\n"
        if stderr:
            body = sanitize_terminal_text(stderr.strip()) if looks_like_terminal_control_text(stderr) else stderr.strip()
            out += f"--- STDERR ---\n{body}\n"
        return out.strip() or "--- The command executed successfully and produced no output ---"

    reg.register(
        name='bash',
        description='Execute a bash script and return combined stdout/stderr. Use timeout_sec to limit execution time.',
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "The bash script to execute."},
                "timeout_sec": {"type": "number", "description": "Maximum seconds to allow the script to run before killing it."},
            },
            "required": ["script"],
        },
        impl=_bash,
    )

    # python
    def _python(args: Dict[str, Any]):
        """Execute Python in a subprocess (sandboxed when enabled).

        We intentionally avoid ``exec()`` in-process because sandboxing
        must be enforceable and because in-process execution can mutate
        global interpreter state.
        """

        from .sandbox import get_thread_sandbox_config, wrap_argv_for_sandbox_with_settings
        from .api import get_thread_working_directory
        from .db import ThreadsDB
        import subprocess, sys
        import time as _time

        script = args.get('script', '')
        thread_id = (args.get('_thread_id') or '').strip()
        # Timeout priority: LLM-specified > RunnerConfig > None
        llm_timeout = args.get('timeout_sec')
        config_timeout = args.get('_tool_timeout_sec')
        try:
            timeout = float(llm_timeout) if llm_timeout is not None else config_timeout
        except (ValueError, TypeError):
            timeout = config_timeout
        # Cancel check callback - returns True if command should be cancelled
        cancel_check = args.get('_cancel_check')

        # Build argv for python -c.
        base_argv = ['python3', '-c', script]

        cwd = None
        # Apply sandbox wrapper, respecting per-thread sandbox config.
        if thread_id:
            try:
                db = ThreadsDB()
                sb = get_thread_sandbox_config(db, thread_id)
                from .api import _ensure_thread_working_directory
                cwd = _ensure_thread_working_directory(db, thread_id)
                argv = wrap_argv_for_sandbox_with_settings(base_argv, enabled=sb.enabled, settings=sb.settings, working_dir=cwd, provider=sb.provider)
            except Exception:
                argv = base_argv
        else:
            from .sandbox import wrap_argv_for_sandbox
            argv = wrap_argv_for_sandbox(base_argv)

        # Use Popen for interruptible execution
        start_time = _time.time()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
        try:
            while proc.poll() is None:
                # Check for cancellation (e.g., Ctrl+C in UI)
                if cancel_check and cancel_check():
                    proc.kill()
                    proc.wait()
                    return "--- INTERRUPTED ---\nScript was interrupted by user"
                # Check for timeout
                if timeout and (_time.time() - start_time) >= timeout:
                    proc.kill()
                    proc.wait()
                    return f"--- TIMEOUT ---\nScript timed out after {timeout} seconds"
                _time.sleep(0.1)  # Poll interval
            stdout_bytes, stderr_bytes = proc.communicate()
        except Exception as e:
            proc.kill()
            proc.wait()
            return f"--- ERROR ---\n{e}"

        out = ''
        stdout = stdout_bytes.decode(errors='replace') if isinstance(stdout_bytes, (bytes, bytearray)) else (stdout_bytes or "")
        stderr = stderr_bytes.decode(errors='replace') if isinstance(stderr_bytes, (bytes, bytearray)) else (stderr_bytes or "")
        if stdout:
            body = sanitize_terminal_text(stdout.strip()) if looks_like_terminal_control_text(stdout) else stdout.strip()
            out += f"--- STDOUT ---\n{body}\n"
        if stderr:
            body = sanitize_terminal_text(stderr.strip()) if looks_like_terminal_control_text(stderr) else stderr.strip()
            out += f"--- STDERR ---\n{body}\n"
        return out.strip() or "--- The script executed successfully and produced no output ---"

    reg.register(
        name='python',
        description='Execute a Python script and return combined stdout/stderr. Use timeout_sec to limit execution time.',
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "The Python script to execute."},
                "timeout_sec": {"type": "number", "description": "Maximum seconds to allow the script to run before killing it."},
            },
        },
        impl=_python,
    )

    def _python_repl(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .session import execute_python_repl

        thread_id = (args.get('_thread_id') or '').strip()
        if not thread_id:
            return 'Error: python_repl requires thread context.'
        code = args.get('code', '')
        repl_name = (args.get('repl_name') or 'default').strip() or 'default'
        runtime_name = (args.get('runtime_name') or 'default').strip() or 'default'
        try:
            bridge_timeout_sec = float(args.get('bridge_timeout_sec')) if args.get('bridge_timeout_sec') is not None else None
        except Exception:
            bridge_timeout_sec = 30.0
        drive_runtime_tools = bool(args.get('drive_runtime_tools', False))
        try:
            return execute_python_repl(
                ThreadsDB(),
                thread_id,
                str(code),
                repl_name=repl_name,
                runtime_name=runtime_name,
                bridge_timeout_sec=bridge_timeout_sec,
                drive_runtime_tools=drive_runtime_tools,
            )
        except Exception as e:
            return f"Error: python_repl failed: {e}"

    reg.register(
        name='python_repl',
        description='Execute Python code in this thread\'s persistent Python REPL session.',
        parameters_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute in the persistent REPL."},
                "repl_name": {"type": "string", "description": "Optional REPL channel name (default: default)."},
                "runtime_name": {"type": "string", "description": "Optional runtime child thread name (default: default)."},
                "bridge_timeout_sec": {"type": "number", "description": "Seconds to wait for programmatic eggtools calls from this eval."},
                "drive_runtime_tools": {"type": "boolean", "description": "Testing/headless mode: directly drive runtime thread tool calls instead of relying on an active subtree scheduler."},
            },
            "required": ["code"],
        },
        impl=_python_repl,
    )

    def _bash_repl(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .session import execute_bash_repl

        thread_id = (args.get('_thread_id') or '').strip()
        if not thread_id:
            return 'Error: bash_repl requires thread context.'
        script = args.get('script', '')
        repl_name = (args.get('repl_name') or 'default').strip() or 'default'
        runtime_name = (args.get('runtime_name') or 'default').strip() or 'default'
        try:
            bridge_timeout_sec = float(args.get('bridge_timeout_sec')) if args.get('bridge_timeout_sec') is not None else None
        except Exception:
            bridge_timeout_sec = 30.0
        drive_runtime_tools = bool(args.get('drive_runtime_tools', False))
        try:
            return execute_bash_repl(
                ThreadsDB(),
                thread_id,
                str(script),
                repl_name=repl_name,
                runtime_name=runtime_name,
                bridge_timeout_sec=bridge_timeout_sec,
                drive_runtime_tools=drive_runtime_tools,
            )
        except Exception as e:
            return f"Error: bash_repl failed: {e}"

    reg.register(
        name='bash_repl',
        description='Execute Bash code in this thread\'s persistent Bash REPL session.',
        parameters_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Bash script to execute in the persistent REPL."},
                "repl_name": {"type": "string", "description": "Optional REPL channel name (default: default)."},
                "runtime_name": {"type": "string", "description": "Optional runtime child thread name (default: default)."},
                "bridge_timeout_sec": {"type": "number", "description": "Seconds to wait for this eval/programmatic eggtool calls."},
                "drive_runtime_tools": {"type": "boolean", "description": "Testing/headless mode: directly drive runtime thread tool calls."},
            },
            "required": ["script"],
        },
        impl=_bash_repl,
    )

    def _session_status(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .session import find_runtime_thread, get_thread_session_status

        thread_id = (args.get('_thread_id') or '').strip()
        if not thread_id:
            return 'Error: session_status requires thread context.'
        db = ThreadsDB()
        lines = []
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

    reg.register(
        name='session_status',
        description='Show persistent REPL/session status for the current thread and runtime children.',
        parameters_schema={"type": "object", "properties": {}},
        impl=_session_status,
    )

    def _session_reset(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .session import find_runtime_thread, reset_thread_session

        thread_id = (args.get('_thread_id') or '').strip()
        if not thread_id:
            return 'Error: session_reset requires thread context.'
        language = str(args.get('language') or '').strip().lower()
        targets: list[str] = []
        db = ThreadsDB()
        if language in ('python', 'bash'):
            rt = find_runtime_thread(db, thread_id, language=language)
            targets = [rt.runtime_thread_id] if rt is not None else [thread_id]
        elif language in ('runtimes', 'runtime', 'all'):
            for lang in ('python', 'bash'):
                rt = find_runtime_thread(db, thread_id, language=lang)
                if rt is not None:
                    targets.append(rt.runtime_thread_id)
            if not targets:
                targets = [thread_id]
        else:
            targets = [thread_id]
        lines = []
        for target in targets:
            sid = reset_thread_session(db, target, reason='session_reset tool')
            lines.append(f"Reset session for {target[-8:]}: {sid}")
        return "\n".join(lines)

    reg.register(
        name='session_reset',
        description='Reset the current thread persistent REPL/session state. Optional language=python|bash|all targets runtime sessions.',
        parameters_schema={
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "Optional target runtime language: python, bash, or all."},
            },
        },
        impl=_session_reset,
    )

    def _session_stop(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .session import find_runtime_thread, stop_thread_session

        thread_id = (args.get('_thread_id') or '').strip()
        if not thread_id:
            return 'Error: session_stop requires thread context.'
        language = str(args.get('language') or '').strip().lower()
        targets: list[str] = []
        db = ThreadsDB()
        if language in ('python', 'bash'):
            rt = find_runtime_thread(db, thread_id, language=language)
            targets = [rt.runtime_thread_id] if rt is not None else [thread_id]
        elif language in ('runtimes', 'runtime', 'all'):
            for lang in ('python', 'bash'):
                rt = find_runtime_thread(db, thread_id, language=lang)
                if rt is not None:
                    targets.append(rt.runtime_thread_id)
            if not targets:
                targets = [thread_id]
        else:
            targets = [thread_id]
        lines = []
        for target in targets:
            st = stop_thread_session(db, target, reason='session_stop tool')
            lines.append(f"Stop session for {target[-8:]}: {st.status} ({st.session_id or '(none)'})")
        return "\n".join(lines)

    reg.register(
        name='session_stop',
        description='Stop the current thread persistent REPL/session without disabling its config. Optional language=python|bash|all targets runtime sessions.',
        parameters_schema={
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "Optional target runtime language: python, bash, or all."},
            },
        },
        impl=_session_stop,
    )

    # javascript (placeholder for remote-debugging execution; here we only echo input)
    def _javascript(args: Dict[str, Any]):
        script = args.get('script', '')
        url = args.get('url', '')
        return _json.dumps({"script": script[:200], "url": url})

    reg.register(
        name='javascript',
        description='Execute JavaScript in a browser via remote debugging (app layer should implement).',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}, "url": {"type": "string"}},
            "required": ["script"],
        },
        impl=_javascript,
    )

    def _clean_optional_text(value: Any) -> str | None:
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
        return None

    def _spawn_parent_id(args: Dict[str, Any]) -> str:
        # Direct/local callers provide parent_thread_id explicitly.
        # Model-initiated calls inherit the current thread via _thread_id,
        # which ToolRegistry.execute injects from runner context.
        return (args.get('parent_thread_id') or args.get('_thread_id') or '').strip()

    def _spawn_initial_model_key(args: Dict[str, Any]) -> str | None:
        # The model-facing spawn tools no longer expose model selection.
        # Model-initiated calls therefore inherit from the parent thread.
        #
        # However, direct/local callers that explicitly pass
        # parent_thread_id (for example app commands or programmatic use)
        # may still choose to override the model. This preserves the
        # lower-level API/command behaviour while removing model choice
        # from the model-visible tool schema.
        if 'parent_thread_id' not in args:
            return None
        return _clean_optional_text(args.get('initial_model_key'))

    def _tool_names_from_arg(value: Any) -> list[str]:
        if isinstance(value, str):
            # Accept comma/whitespace separated strings for local callers.
            import re as _re
            return [p for p in _re.split(r'[\s,]+', value) if p]
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]
        return []

    def _clean_bool_arg(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)

    def _apply_spawn_child_configuration(args: Dict[str, Any], parent_id: str, child: str) -> None:
        """Apply attenuated tool/session config requested by spawn args."""

        from .db import ThreadsDB
        from .tools_config import get_thread_tools_config, set_thread_tool_allowlist, disable_tool_for_thread
        from .session import get_thread_session_config, set_thread_session_config

        db = ThreadsDB()

        # Tool capability attenuation: requested child allowlist is
        # intersected with the parent's effective capabilities.  If no
        # explicit child allowlist was requested, inherit any explicit
        # parent allowlist by value so descendants cannot silently widen it.
        parent_cfg = get_thread_tools_config(db, parent_id)
        requested_allowed = _tool_names_from_arg(args.get('allowed_tools'))
        if requested_allowed:
            allowed = sorted({name for name in requested_allowed if parent_cfg.is_tool_allowed(name)})
            set_thread_tool_allowlist(db, child, allowed)
        elif parent_cfg.allowed_tools is not None:
            inherited = sorted({name for name in parent_cfg.allowed_tools if parent_cfg.is_tool_allowed(name)})
            set_thread_tool_allowlist(db, child, inherited)

        for name in _tool_names_from_arg(args.get('disabled_tools')):
            disable_tool_for_thread(db, child, name)

        # Optional explicit session sharing.  If share_session is omitted,
        # honour the parent's share_with_children_default policy.
        parent_session = get_thread_session_config(db, parent_id)
        share_arg = args.get('share_session')
        share_requested = _clean_bool_arg(share_arg) if share_arg is not None else bool(parent_session.share_with_children_default)
        if share_requested and parent_session.enabled and parent_session.session_id:
            set_thread_session_config(
                db,
                child,
                enabled=True,
                provider=parent_session.provider,
                image=parent_session.image,
                share='session',
                session_id=parent_session.session_id,
                owner_thread_id=parent_session.owner_thread_id or parent_id,
                workspace=parent_session.workspace,
                network=parent_session.network,
                share_with_children_default=parent_session.share_with_children_default,
                share_repl=_clean_bool_arg(args.get('share_repl')) if args.get('share_repl') is not None else parent_session.share_repl,
                reason='spawn_agent share_session',
            )

    # spawn_agent: create a child thread under a given parent, mirroring
    # the behaviour of the /spawn UI command but in a UI-agnostic way.
    def _spawn_agent(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .api import create_child_thread, append_message, create_snapshot

        # Parent is either explicitly provided (for UI/user commands) or
        # inferred from the calling thread context (_thread_id injected by
        # ToolRegistry.execute when called from a runner).
        parent_id = _spawn_parent_id(args)
        if not parent_id:
            return 'Error: parent_thread_id is required.'

        # Optional fields
        label = (args.get('label') or 'spawn').strip() or 'spawn'
        user_text = (args.get('context_text') or '').strip() or 'Spawned task'
        initial_model_key = _spawn_initial_model_key(args)
        system_prompt = (args.get('system_prompt') or '').strip() or None

        db = ThreadsDB()

        # If no explicit system prompt was provided, try to inherit it from
        # the parent thread's first system message. This mirrors the
        # interactive app's behaviour where child threads reuse the global
        # system prompt. As a final fallback, use a generic helper prompt.
        if not system_prompt:
            try:
                row = db.get_thread(parent_id)
                # Build a fresh snapshot if needed so we see system
                # messages even for recently created parents.
                if row and not row.snapshot_json:
                    try:
                        create_snapshot(db, parent_id)
                        row = db.get_thread(parent_id)
                    except Exception:
                        pass
                if row and row.snapshot_json:
                    try:
                        snap = _json.loads(row.snapshot_json)
                        msgs = snap.get('messages', []) or []
                        for m in msgs:
                            try:
                                if m.get('role') == 'system' and isinstance(m.get('content'), str):
                                    system_prompt = m.get('content') or None
                                    if system_prompt:
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass
            except Exception:
                pass
        if not system_prompt:
            system_prompt = 'You are a helpful assistant.'

        # Create child thread
        try:
            child = create_child_thread(db, parent_id, name=label, initial_model_key=initial_model_key)
        except Exception as e:
            return f"Error: failed to create child thread: {e}"

        # Sandbox configuration inheritance is handled by
        # eggthreads.api.create_child_thread.

        try:
            _apply_spawn_child_configuration(args, parent_id, child)
        except Exception:
            # Child creation should not fail solely because optional
            # attenuation/session propagation failed.  The conservative
            # defaults still apply through normal thread inheritance.
            pass

        # Seed the child with the resolved system prompt.
        try:
            append_message(db, child, 'system', system_prompt)
        except Exception:
            # Best-effort; child thread still exists.
            pass

        # Attach the requested user/context text
        try:
            append_message(db, child, 'user', user_text)
        except Exception as e:
            return f"Error: created child {child} but failed to append user message: {e}"

        try:
            create_snapshot(db, child)
        except Exception:
            # Non-fatal
            pass

        return child

    reg.register(
        name='spawn_agent',
        description=(
            'Spawn a child agent as a new thread under the current task. '
            'Use this whenever you want to delegate a sub-problem to a '
            'child agent. Provide a short natural-language description of '
            'the sub-task in context_text. The child agent will run '
            'independently and eventually produce an assistant message as '
            'its result. To retrieve that result, call the "wait" tool on '
            'the returned thread id and read the last assistant message.'
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                # The parent thread is always the current calling thread;
                # the model never needs to provide its id explicitly.
                "context_text": {"type": "string"},
                "label": {"type": "string"},
                "system_prompt": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "disabled_tools": {"type": "array", "items": {"type": "string"}},
                "share_session": {"type": "boolean"},
                "share_repl": {"type": "boolean"},
            },
            # Models must provide a short description of the sub-task.
            "required": ["context_text"],
        },
        impl=_spawn_agent,
        # Exposed to the model so it can explicitly spawn child agents.
        local_only=False,
    )

    # spawn_agent_auto: like spawn_agent, but also enables global tool
    # call auto-approval for the spawned child thread so it can execute
    # tools without further per-call approvals.
    def _spawn_agent_auto(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .api import create_child_thread, append_message, create_snapshot

        parent_id = _spawn_parent_id(args)
        if not parent_id:
            return 'Error: parent_thread_id is required.'

        label = (args.get('label') or 'spawn_auto').strip() or 'spawn_auto'
        user_text = (args.get('context_text') or '').strip() or 'Spawned task'
        initial_model_key = _spawn_initial_model_key(args)
        system_prompt = (args.get('system_prompt') or '').strip() or None

        db = ThreadsDB()

        # Inherit or default system prompt as in _spawn_agent.
        if not system_prompt:
            try:
                row = db.get_thread(parent_id)
                if row and not row.snapshot_json:
                    try:
                        create_snapshot(db, parent_id)
                        row = db.get_thread(parent_id)
                    except Exception:
                        pass
                if row and row.snapshot_json:
                    try:
                        snap = _json.loads(row.snapshot_json)
                        msgs = snap.get('messages', []) or []
                        for m in msgs:
                            try:
                                if m.get('role') == 'system' and isinstance(m.get('content'), str):
                                    system_prompt = m.get('content') or None
                                    if system_prompt:
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass
            except Exception:
                pass
        if not system_prompt:
            system_prompt = 'You are a helpful assistant.'

        # Create child thread
        try:
            child = create_child_thread(db, parent_id, name=label, initial_model_key=initial_model_key)
        except Exception as e:
            return f"Error: failed to create child thread: {e}"

        # Sandbox configuration inheritance is handled by
        # eggthreads.api.create_child_thread.

        try:
            _apply_spawn_child_configuration(args, parent_id, child)
        except Exception:
            pass

        # Seed system prompt and user text
        try:
            append_message(db, child, 'system', system_prompt)
        except Exception:
            pass
        try:
            append_message(db, child, 'user', user_text)
        except Exception as e:
            return f"Error: created child {child} but failed to append user message: {e}"

        # Record a global auto-approval decision for this child thread so
        # it can execute tools without further per-call approvals.
        try:
            db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=child,
                type_='tool_call.approval',
                msg_id=None,
                invoke_id=None,
                payload={
                    'decision': 'global_approval',
                    'reason': 'spawn_agent_auto enabled global tool auto-approval for this thread',
                },
            )
        except Exception:
            pass

        try:
            create_snapshot(db, child)
        except Exception:
            pass

        return child

    reg.register(
        name='spawn_agent_auto',
        description=(
            'Like spawn_agent, but configures the spawned child thread to '
            'have global tool auto-approval. The child agent can call '
            'tools without further approval events. Use context_text to '
            'describe the delegated sub-task, then use the "wait" tool on '
            'the returned thread id to read its final assistant message.'
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "context_text": {"type": "string"},
                "label": {"type": "string"},
                "system_prompt": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "disabled_tools": {"type": "array", "items": {"type": "string"}},
                "share_session": {"type": "boolean"},
                "share_repl": {"type": "boolean"},
            },
            "required": ["context_text"],
        },
        impl=_spawn_agent_auto,
        local_only=False,
    )

    # replace_between
    def _replace_between(args: Dict[str, Any]):
        file_path = args.get('file_path', '')
        start_text = args.get('start_text', '')
        end_text = args.get('end_text', '')
        new_text = args.get('new_text', '')
        p = Path(file_path)
        content = p.read_text() if p.exists() else ''
        sidx = content.find(start_text)
        if sidx == -1:
            return "Error: start_text not found."
        eidx = content.find(end_text, sidx + len(start_text))
        if eidx == -1:
            return "Error: end_text not found after start_text."
        new_content = content[:sidx] + new_text + content[eidx + len(end_text):]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_content)
        return "Success: replaced region."

    reg.register(
        name='replace_between',
        description='Replace the first region between two exact string boundaries (start_text and the first subsequent end_text) with new_text. Exact literal matching only (no regex). The boundaries themselves are also replaced. Works across line breaks.',
        parameters_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "start_text": {"type": "string"},
                "end_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["file_path", "start_text", "end_text", "new_text"],
        },
        impl=_replace_between,
    )

    # web_search / fetch_url backed by a pluggable WebBackend.
    # Default backend is SearXNG; override with EGG_WEB_BACKEND=tavily
    # to use Tavily's API instead.
    from .web import WebBackendError, get_backend as _get_web_backend

    # Default result count: 10 (roughly SearXNG's natural "one page"
    # once engines are deduplicated). Override per-process via
    # EGG_WEB_MAX_RESULTS, or per-call via the tool's max_results arg.
    _WEB_RESULTS_CAP = 25

    def _resolve_max_results(args: Dict[str, Any]) -> int:
        raw = args.get('max_results')
        if raw is None:
            raw = os.environ.get('EGG_WEB_MAX_RESULTS')
        try:
            n = int(raw) if raw is not None and str(raw).strip() != '' else 10
        except (TypeError, ValueError):
            n = 10
        if n < 1:
            n = 1
        if n > _WEB_RESULTS_CAP:
            n = _WEB_RESULTS_CAP
        return n

    def _web_search(args: Dict[str, Any]):
        query = str(args.get('query') or '').strip()
        if not query:
            return 'Error: "query" is required.'
        n = _resolve_max_results(args)
        try:
            backend = _get_web_backend()
            results = backend.search(query, max_results=n)
        except WebBackendError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: web_search failed: {e}"
        if not results:
            return "No results."
        lines = []
        for r in results:
            if not (r.title or r.url):
                continue
            snippet = (r.snippet or '').strip().replace('\n', ' ')
            if len(snippet) > 200:
                snippet = snippet[:200].rstrip() + '…'
            if snippet:
                lines.append(f"- {r.title}  {r.url}\n    {snippet}")
            else:
                lines.append(f"- {r.title}  {r.url}")
        return "\n".join(lines)

    def _fetch_url(args: Dict[str, Any]):
        url = str(args.get('url') or '').strip()
        if not url:
            return 'Error: "url" is required.'
        try:
            backend = _get_web_backend()
            return backend.fetch(url)
        except WebBackendError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: fetch_url failed: {e}"

    _search_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of results to return "
                    f"(default 10, max {_WEB_RESULTS_CAP})."
                ),
                "minimum": 1,
                "maximum": _WEB_RESULTS_CAP,
            },
        },
        "required": ["query"],
    }
    _fetch_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch."},
        },
        "required": ["url"],
    }

    reg.register(
        name='web_search',
        description=(
            'Perform a web search and return results with titles, URLs, and short snippets. '
            f'Defaults to 10 results (cap {_WEB_RESULTS_CAP}); pass max_results to adjust. '
            'Backend is selected via EGG_WEB_BACKEND (default: searxng).'
        ),
        parameters_schema=_search_schema,
        impl=_web_search,
    )
    reg.register(
        name='fetch_url',
        description=(
            'Fetch and extract readable markdown from a URL. Use this when you '
            'already know the page URL.'
        ),
        parameters_schema=_fetch_schema,
        impl=_fetch_url,
    )

    # wait: synchronize on other threads and return their last assistant
    # message when finished. This is a standard tool so that both the
    # assistant and user commands (/wait) can use the same implementation.
    def _wait_tool(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .api import wait_for_threads

        tids_arg = args.get('thread_ids') or args.get('threads') or args.get('thread_id')
        if isinstance(tids_arg, str):
            thread_ids = [tids_arg]
        elif isinstance(tids_arg, list):
            thread_ids = [str(t) for t in tids_arg if isinstance(t, (str, int))]
        else:
            return 'Error: "thread_ids" must be a string or a list of strings.'
        if not thread_ids:
            return 'Error: no valid thread_ids provided.'

        timeout = args.get('timeout_sec') or args.get('timeout')
        try:
            timeout_sec = float(timeout) if timeout is not None else None
        except Exception:
            timeout_sec = None

        db = ThreadsDB()
        results = wait_for_threads(db, thread_ids, timeout_sec=timeout_sec, poll_interval=0.2)

        # Format summary
        lines: list[str] = []
        for tid in thread_ids:
            short = tid[-8:]
            res = results.get(tid)
            if res is not None and res.finished:
                content = res.last_assistant_message or '(no assistant content found)'
                lines.append(f"Thread {short} finished. Last assistant message:\n{content}")
            else:
                st = res.state if res is not None else 'unknown'
                lines.append(f"Thread {short} not finished (state={st}).")
        return "\n\n".join(lines)

    reg.register(
        name='wait',
        description=(
            'Wait for one or more threads to finish and return their last '
            'assistant message. A thread is considered finished when its '
            "state is 'waiting_user'. Optional timeout_sec limits how long to wait."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "thread_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of thread_ids to wait for.",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "Maximum seconds to wait before returning.",
                },
            },
            "required": ["thread_ids"],
        },
        impl=_wait_tool,
        # Exposed to the model so it can explicitly wait for spawned
        # child agents and read their final responses.
        local_only=False,
    )

    # Note: agent-oriented tools like popContext/spawn_agent are excluded from default registry
    # to prevent unintended tool calls in basic chats. The UI layer can register them explicitly.

    return reg

