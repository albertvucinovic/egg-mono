from __future__ import annotations

"""Shared UI command/autocomplete catalog for Egg frontends."""

import os
import json
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping


def _normalize_command_name(name: str) -> str:
    normalized = (name or "").strip()
    if normalized.startswith('/'):
        normalized = normalized[1:]
    if not normalized:
        raise ValueError("Command name must not be empty")
    if any(ch.isspace() for ch in normalized):
        raise ValueError(f"Command name must not contain whitespace: {name!r}")
    return normalized


@dataclass(frozen=True)
class CommandResult:
    """Result returned by a slash-command handler."""

    clear_input: bool = True
    exit_app: bool = False
    switched_thread: str | None = None
    start_schedulers: tuple[str, ...] = ()
    message: str | None = None


@dataclass(frozen=True)
class CommandContext:
    """Runtime context passed to command handlers.

    The fields are intentionally optional so the registry can be introduced
    before every existing UI command has migrated away from direct app access.
    """

    db: Any = None
    current_thread: str | None = None
    set_current_thread: Callable[[str], None] | None = None
    log_system: Callable[[str], None] | None = None
    console_print_block: Callable[..., None] | None = None
    start_scheduler: Callable[[str], None] | None = None
    llm_client: Any = None
    system_prompt: str | None = None
    get_current_model: Callable[[str], str | None] | None = None
    watch_current_thread: Callable[[], Any] | None = None
    print_current_thread: Callable[..., None] | None = None
    format_threads: Callable[..., str] | None = None
    select_threads: Callable[[str], list[str]] | None = None
    app: Any = None


CommandHandler = Callable[[CommandContext, str], CommandResult | None]
CommandCompleter = Callable[[CommandContext, str], Iterable[str | Mapping[str, Any]]]
InputPrefixHandler = Callable[[CommandContext, str], CommandResult | None]


@dataclass(frozen=True)
class CommandSpec:
    """Metadata and handler for a registered slash command."""

    name: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    category: str = "general"
    usage: str = ""
    description: str = ""
    complete: CommandCompleter | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalize_command_name(self.name))
        object.__setattr__(
            self,
            "aliases",
            tuple(_normalize_command_name(alias) for alias in self.aliases),
        )


class CommandRegistry:
    """Deterministic registry for slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(self, spec: CommandSpec) -> None:
        if spec.name in self._commands or spec.name in self._aliases:
            raise ValueError(f"Command already registered: /{spec.name}")
        for alias in spec.aliases:
            if alias in self._commands or alias in self._aliases or alias == spec.name:
                raise ValueError(f"Command alias already registered: /{alias}")

        self._commands[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name

    def get(self, name: str) -> CommandSpec:
        normalized = _normalize_command_name(name)
        target = self._aliases.get(normalized, normalized)
        try:
            return self._commands[target]
        except KeyError:
            raise KeyError(f"Unknown command: /{normalized}") from None

    def specs(self) -> list[CommandSpec]:
        return list(self._commands.values())

    def names(self, *, include_aliases: bool = False) -> list[str]:
        names = list(self._commands.keys())
        if include_aliases:
            names.extend(self._aliases.keys())
        return names

    def execute(self, name: str, context: CommandContext, arg: str = "") -> CommandResult:
        result = self.get(name).handler(context, arg)
        return result if isinstance(result, CommandResult) else CommandResult()

    def complete(self, name: str, context: CommandContext, arg: str = "") -> list[str | Mapping[str, Any]]:
        completer = self.get(name).complete
        if completer is None:
            return []
        return list(completer(context, arg))


@dataclass(frozen=True)
class InputPrefixSpec:
    """Handler metadata for non-slash input prefixes such as `$` and `$$`."""

    prefix: str
    handler: InputPrefixHandler
    description: str = ""
    clear_input: bool = True

    def __post_init__(self) -> None:
        if not self.prefix:
            raise ValueError("Input prefix must not be empty")


class InputPrefixRegistry:
    """Longest-prefix-match registry for user input handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, InputPrefixSpec] = {}

    def register(self, spec: InputPrefixSpec) -> None:
        if spec.prefix in self._handlers:
            raise ValueError(f"Input prefix already registered: {spec.prefix!r}")
        self._handlers[spec.prefix] = spec

    def specs(self) -> list[InputPrefixSpec]:
        return [self._handlers[prefix] for prefix in sorted(self._handlers, key=lambda p: (-len(p), p))]

    def match(self, text: str) -> tuple[InputPrefixSpec, str] | None:
        for spec in self.specs():
            if text.startswith(spec.prefix):
                return spec, text[len(spec.prefix):]
        return None

    def execute(self, text: str, context: CommandContext) -> CommandResult | None:
        matched = self.match(text)
        if matched is None:
            return None
        spec, rest = matched
        result = spec.handler(context, rest)
        if isinstance(result, CommandResult):
            return result
        return CommandResult(clear_input=spec.clear_input)


def render_command_registry_help(registry: CommandRegistry) -> str:
    """Render slash-command help from CommandRegistry metadata."""
    lines: List[str] = ["Commands:"]
    categories: dict[str, list[CommandSpec]] = {}
    for spec in registry.specs():
        categories.setdefault(spec.category or "general", []).append(spec)

    for category, specs in categories.items():
        lines.append(f"  {category.replace('_', ' ').title()}:")
        for spec in specs:
            usage = spec.usage or f"/{spec.name}"
            if spec.aliases:
                aliases = ", ".join(f"/{alias}" for alias in spec.aliases)
                usage = f"{usage} (aliases: {aliases})"
            if spec.description:
                lines.append(f"    {usage} — {spec.description}")
            else:
                lines.append(f"    {usage}")
    return "\n".join(lines)


def _core_help_handler(context: CommandContext, arg: str) -> CommandResult:
    registry = getattr(context.app, "command_registry", None) or create_default_command_registry()
    help_text = render_command_registry_help(registry)
    try:
        if context.log_system is not None:
            context.log_system("Help (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Help", help_text, border_style="blue")
        elif context.log_system is not None:
            context.log_system(help_text)
    except Exception:
        if context.log_system is not None:
            context.log_system(help_text)
    return CommandResult(clear_input=True)


def _core_quit_handler(context: CommandContext, arg: str) -> CommandResult:
    if context.app is not None:
        context.app.running = False
    return CommandResult(clear_input=True, exit_app=True)


def _core_reload_handler(context: CommandContext, arg: str) -> CommandResult:
    thread_id = context.current_thread or getattr(context.app, "current_thread", "")
    if not thread_id:
        if context.log_system is not None:
            context.log_system("/reload failed: no current thread.")
        return CommandResult(clear_input=False)

    os.environ["EGG_RELOAD_THREAD_ID"] = thread_id
    if context.app is not None:
        context.app._reload_via_shell = False
    state_file = os.environ.get("EGG_RELOAD_STATE_FILE")
    if state_file:
        try:
            Path(state_file).write_text(f"{thread_id}\n", encoding="utf-8")
            if context.app is not None:
                context.app._reload_via_shell = True
        except Exception as e:
            if context.log_system is not None:
                context.log_system(f"/reload failed to save thread id for egg.sh: {e}; using direct restart.")
    elif context.log_system is not None:
        context.log_system("/reload: no egg.sh state file; using direct restart.")

    if context.app is not None:
        context.app._reload_requested = True
        context.app.running = False
    return CommandResult(clear_input=False, exit_app=True)


def _log_command_message(context: CommandContext, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _command_db_and_thread(context: CommandContext, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _log_command_message(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def _tools_enabled_handler(context: CommandContext, enabled: bool) -> CommandResult:
    command_name = "toolsOn" if enabled else "toolsOff"
    target = _command_db_and_thread(context, command_name)
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        import eggthreads as _eggthreads  # type: ignore

        _eggthreads.set_thread_tools_enabled(db, thread_id, enabled)
        if enabled:
            _log_command_message(context, "Tools enabled for this thread (LLM may call tools).")
        else:
            _log_command_message(context, "Tools disabled for this thread (LLM tool calls suppressed).")
    except Exception as e:
        _log_command_message(context, f"/{command_name.lower()} error: {e}")
    return CommandResult(clear_input=True)


def _tools_on_handler(context: CommandContext, arg: str) -> CommandResult:
    return _tools_enabled_handler(context, True)


def _tools_off_handler(context: CommandContext, arg: str) -> CommandResult:
    return _tools_enabled_handler(context, False)


def _disable_tool_handler(context: CommandContext, arg: str) -> CommandResult:
    name = (arg or "").strip()
    if not name:
        _log_command_message(context, "Usage: /disabletool <tool_name>")
        return CommandResult(clear_input=False)
    target = _command_db_and_thread(context, "disableTool")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        import eggthreads as _eggthreads  # type: ignore

        _eggthreads.disable_tool_for_thread(db, thread_id, name)
        _log_command_message(context, f"Tool '{name}' disabled for this thread.")
    except Exception as e:
        _log_command_message(context, f"/disabletool error: {e}")
    return CommandResult(clear_input=True)


def _enable_tool_handler(context: CommandContext, arg: str) -> CommandResult:
    name = (arg or "").strip()
    if not name:
        _log_command_message(context, "Usage: /enabletool <tool_name>")
        return CommandResult(clear_input=False)
    target = _command_db_and_thread(context, "enableTool")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        import eggthreads as _eggthreads  # type: ignore

        _eggthreads.enable_tool_for_thread(db, thread_id, name)
        _log_command_message(context, f"Tool '{name}' enabled for this thread.")
    except Exception as e:
        _log_command_message(context, f"/enabletool error: {e}")
    return CommandResult(clear_input=True)


def _tools_secrets_handler(context: CommandContext, arg: str) -> CommandResult:
    mode = (arg or "").strip().lower()
    if mode not in ("on", "off"):
        _log_command_message(context, "Usage: /toolsSecrets <on|off>  (on = allow raw tool output, off = mask secrets)")
        return CommandResult(clear_input=False)
    target = _command_db_and_thread(context, "toolsSecrets")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    allow_raw = mode == "on"
    try:
        import eggthreads as _eggthreads  # type: ignore

        _eggthreads.set_thread_allow_raw_tool_output(db, thread_id, allow_raw)
        if allow_raw:
            _log_command_message(context, "Tool output secrets: raw mode ENABLED (secrets will not be masked).")
        else:
            _log_command_message(context, "Tool output secrets: masking ENABLED (attempting to mask detected secrets).")
    except Exception as e:
        _log_command_message(context, f"/toolsecrets error: {e}")
    return CommandResult(clear_input=True)


def _get_available_tools() -> dict[str, dict[str, Any]]:
    from .tools import create_default_tools

    registry = create_default_tools()
    return {
        name: {
            "spec": entry["spec"],
            "local_only": entry.get("local_only", False),
        }
        for name, entry in registry._tools.items()
    }


def _tools_status_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "toolsStatus")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        import eggthreads as _eggthreads  # type: ignore

        cfg = _eggthreads.get_thread_tools_config(db, thread_id)
        tool_statuses = _eggthreads.get_tool_statuses_for_config(cfg, _get_available_tools())
    except Exception as e:
        _log_command_message(context, f"/toolStatus error: {e}")
        return CommandResult(clear_input=False)

    lines = []
    tools_status = "ENABLED" if cfg.llm_tools_enabled else "DISABLED"
    lines.append(f"Tools for LLM: {tools_status}")

    secrets_mode = "raw (secrets visible)" if getattr(cfg, "allow_raw_tool_output", False) else "masked"
    lines.append(f"Tool output secrets: {secrets_mode}")

    allowed_tools = getattr(cfg, "allowed_tools", None)
    if allowed_tools is None:
        lines.append("Tool allowlist: all registered tools")
    else:
        allowed_names = ", ".join(sorted(allowed_tools)) or "(none)"
        lines.append(f"Tool allowlist: {allowed_names}")

    lines.append("")
    lines.append("Available tools:")
    for tool_status in tool_statuses:
        status_parts = [tool_status["status_label"]]
        if tool_status.get("local_only", False):
            status_parts.append("local-only")
        lines.append(f"  {tool_status['name']}: {', '.join(status_parts)}")

    lines.append("")
    lines.append("Use /disableTool <name> or /enableTool <name> to control individual tools")
    lines.append("Use /toolInfo <name> to see tool description")

    text = "\n".join(lines)
    try:
        _log_command_message(context, "Tools status (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Tools Status", text, border_style="blue")
        else:
            _log_command_message(context, text)
    except Exception:
        _log_command_message(context, text)
    return CommandResult(clear_input=True)


def _tool_info_handler(context: CommandContext, arg: str) -> CommandResult:
    tool_name = (arg or "").strip()
    if not tool_name:
        _log_command_message(context, "Usage: /toolInfo <tool_name>")
        return CommandResult(clear_input=False)

    available_tools = _get_available_tools()
    tool_info = available_tools.get(tool_name)
    if not tool_info:
        for name, info in available_tools.items():
            if name.lower() == tool_name.lower():
                tool_info = info
                tool_name = name
                break

    if not tool_info:
        available_names = sorted(available_tools.keys())
        _log_command_message(context, f"Tool '{tool_name}' not found.\nAvailable tools: {', '.join(available_names)}")
        return CommandResult(clear_input=False)

    text = "\n".join(
        [
            f"Tool: {tool_name}",
            f"Local-only: {tool_info.get('local_only', False)}",
            "",
            "Spec (sent to LLM):",
            json.dumps(tool_info["spec"], indent=2),
        ]
    )
    try:
        _log_command_message(context, f"Tool info: {tool_name} (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block(f"Tool: {tool_name}", text, border_style="blue")
        else:
            _log_command_message(context, text)
    except Exception:
        _log_command_message(context, text)
    return CommandResult(clear_input=True)


def _toggle_auto_approval_for_thread(
    db: Any,
    thread_id: str,
    log_message: Callable[[str], None] | None,
    approve_func: Callable[..., Any],
) -> None:
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval' ORDER BY event_seq ASC",
            (thread_id,),
        )
        last_decision = None
        for (payload_json,) in cur.fetchall():
            try:
                payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
            except Exception:
                payload = {}
            decision = payload.get("decision")
            if decision in ("global_approval", "revoke_global_approval"):
                last_decision = decision
        enable = last_decision != "global_approval"
    except Exception:
        enable = True

    decision = "global_approval" if enable else "revoke_global_approval"
    try:
        approve_func(
            db,
            thread_id,
            decision=decision,
            reason="Toggled by user via /toggleAutoApproval",
        )
        if log_message is not None:
            log_message(
                "Global tool auto-approval ENABLED for this thread."
                if enable
                else "Global tool auto-approval DISABLED for this thread.",
            )
    except Exception as e:
        if log_message is not None:
            log_message(f"Error toggling auto-approval: {e}")


def _toggle_auto_approval_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "toggleAutoApproval")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        import eggthreads as _eggthreads  # type: ignore
    except Exception:
        _log_command_message(context, "Auto-approval toggle not available (eggthreads import failed).")
        return CommandResult(clear_input=False)
    _toggle_auto_approval_for_thread(db, thread_id, context.log_system, _eggthreads.approve_tool_calls_for_thread)
    return CommandResult(clear_input=True)


def _schedule_coro(coro_factory: Callable[[], Any] | None) -> None:
    if coro_factory is None:
        return
    try:
        loop = asyncio.get_running_loop()
        coro = coro_factory()
        try:
            task = loop.create_task(coro)
            if task is None and hasattr(coro, "close"):
                coro.close()
        except Exception:
            if hasattr(coro, "close"):
                coro.close()
    except Exception:
        pass


def _set_current_thread(context: CommandContext, thread_id: str) -> None:
    if context.set_current_thread is not None:
        context.set_current_thread(thread_id)
    elif context.app is not None:
        context.app.current_thread = thread_id


def _start_scheduler(context: CommandContext, thread_id: str) -> None:
    if context.start_scheduler is not None:
        context.start_scheduler(thread_id)


def _print_current_thread(context: CommandContext, heading: str) -> None:
    if context.print_current_thread is not None:
        try:
            context.print_current_thread(heading=heading)
        except TypeError:
            context.print_current_thread(heading)


def _current_model_for_thread(context: CommandContext, thread_id: str) -> str | None:
    if context.get_current_model is not None:
        return context.get_current_model(thread_id)
    if context.app is not None and hasattr(context.app, "current_model_for_thread"):
        return context.app.current_model_for_thread(thread_id)
    try:
        from .api import current_thread_model

        return current_thread_model(context.db, thread_id)
    except Exception:
        pass
    return None


def _select_threads_by_selector(context: CommandContext, selector: str) -> list[str]:
    if context.select_threads is not None:
        return list(context.select_threads(selector))
    if context.app is not None and hasattr(context.app, "select_threads_by_selector"):
        return list(context.app.select_threads_by_selector(selector))
    try:
        from .api import list_threads

        rows = list_threads(context.db)
    except Exception:
        rows = []
    sel_l = (selector or "").lower()
    matches: list[str] = []
    for row in rows:
        if row.thread_id == selector:
            return [row.thread_id]
    if sel_l:
        matches = [row.thread_id for row in rows if row.thread_id.lower().endswith(sel_l)]
    if not matches and sel_l:
        matches = [row.thread_id for row in rows if sel_l in row.thread_id.lower()]
    if not matches and sel_l:
        matches = [row.thread_id for row in rows if isinstance(row.name, str) and sel_l in row.name.lower()]
    if not matches and sel_l:
        matches = [row.thread_id for row in rows if isinstance(row.short_recap, str) and sel_l in row.short_recap.lower()]
    return matches


def _sort_thread_matches_newest_first(db: Any, matches: list[str]) -> list[str]:
    try:
        from .api import list_threads

        rows = list_threads(db)
        created_at = {row.thread_id: row.created_at for row in rows}
    except Exception:
        created_at = {}
    return sorted(matches, key=lambda tid: created_at.get(tid, ""), reverse=True)


def _resolve_thread_selector(context: CommandContext, selector: str) -> str | None:
    sel = (selector or "").strip()
    if not sel:
        return None
    matches = _select_threads_by_selector(context, sel)
    if not matches and " " in sel:
        matches = _select_threads_by_selector(context, sel.split()[0])
    if not matches:
        try:
            from .api import list_threads

            suffix = sel.lower()
            matches = [row.thread_id for row in list_threads(context.db) if row.thread_id.lower().endswith(suffix)]
        except Exception:
            matches = []
    if not matches:
        return None
    return _sort_thread_matches_newest_first(context.db, matches)[0]


def _threads_new_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "newThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        from .api import append_message, create_root_thread, create_snapshot
    except Exception as e:
        _log_command_message(context, f"/newThread error: {e}")
        return CommandResult(clear_input=False)

    new_name = (arg or "").strip() or "Root"
    new_root = create_root_thread(
        db,
        name=new_name,
        initial_model_key=_current_model_for_thread(context, current_thread),
    )
    append_message(db, new_root, "system", context.system_prompt or "You are a helpful assistant.")
    create_snapshot(db, new_root)
    _start_scheduler(context, new_root)
    _set_current_thread(context, new_root)
    _schedule_coro(context.watch_current_thread)
    _log_command_message(context, f"Created new root thread: {new_root[-8:]}")
    _print_current_thread(context, heading=f"Switched to thread: {new_root}")
    return CommandResult(clear_input=True, switched_thread=new_root, start_schedulers=(new_root,))


def _threads_list_handler(context: CommandContext, arg: str) -> CommandResult:
    try:
        text = context.format_threads() if context.format_threads is not None else "No thread formatter available."
        _log_command_message(context, "Threads by subtree (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Threads", text, border_style="blue")
        else:
            _log_command_message(context, text)
    except Exception as e:
        _log_command_message(context, f"Error listing threads: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def _thread_switch_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "thread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    selector = (arg or "").strip()
    if not selector:
        _log_command_message(context, f"Current thread: {current_thread}")
        return CommandResult(clear_input=True)

    new_thread = _resolve_thread_selector(context, selector)
    if not new_thread:
        _log_command_message(context, f"No thread matches selector: {selector}")
        return CommandResult(clear_input=False)
    _start_scheduler(context, new_thread)
    _set_current_thread(context, new_thread)
    _schedule_coro(context.watch_current_thread)
    _log_command_message(context, f"Switched to thread: {new_thread[-8:]}")
    _print_current_thread(context, heading=f"Switched to thread: {new_thread}")
    return CommandResult(clear_input=True, switched_thread=new_thread, start_schedulers=(new_thread,))


def _delete_thread_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "deleteThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    selector = (arg or "").strip()
    if not selector:
        _log_command_message(context, "Usage: /delete <thread-id|suffix|name|recap-fragment>")
        return CommandResult(clear_input=False)

    matches = _select_threads_by_selector(context, selector)
    if not matches and " " in selector:
        matches = _select_threads_by_selector(context, selector.split()[0])
    if not matches:
        try:
            from .api import list_threads

            suffix = selector.lower()
            matches = [row.thread_id for row in list_threads(db) if row.thread_id.lower().endswith(suffix)]
        except Exception:
            matches = []
    matches = [thread_id for thread_id in matches if thread_id != current_thread]
    if not matches:
        _log_command_message(context, "No deletable thread matches selector.")
        return CommandResult(clear_input=False)

    target_thread = _sort_thread_matches_newest_first(db, matches)[0]
    try:
        from .api import delete_thread

        delete_thread(db, target_thread)
        _log_command_message(context, f"Thread {target_thread[-8:]} deleted.")
    except Exception as e:
        _log_command_message(context, f"Error deleting thread: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def _parent_thread_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "parentThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        from .api import get_parent

        parent_id = get_parent(db, current_thread)
    except Exception:
        parent_id = None
    if not parent_id:
        _log_command_message(context, "Already at root or no parent found.")
        return CommandResult(clear_input=False)
    _set_current_thread(context, parent_id)
    _schedule_coro(context.watch_current_thread)
    _log_command_message(context, "Moved to parent thread")
    _print_current_thread(context, heading=f"Switched to thread: {parent_id}")
    return CommandResult(clear_input=True, switched_thread=parent_id)


def _list_children_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "listChildren")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        from .api import list_children_ids

        has_children = bool(list_children_ids(db, current_thread))
    except Exception:
        has_children = False
    if not has_children:
        _log_command_message(context, "No subthreads.")
        return CommandResult(clear_input=True)
    block = context.format_threads(current_thread) if context.format_threads is not None else "No thread formatter available."
    _log_command_message(context, "Subtree (see console for full):")
    if context.console_print_block is not None:
        context.console_print_block("Subtree", block, border_style="blue")
    else:
        _log_command_message(context, block)
    return CommandResult(clear_input=True)


def _duplicate_thread_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "duplicateThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        from .api import duplicate_thread, duplicate_thread_up_to
        from .arg_parser import parse_args
    except Exception as e:
        _log_command_message(context, f"/duplicateThread error: {e}")
        return CommandResult(clear_input=False)

    args = parse_args(arg or "")
    source_thread_id = current_thread
    name = None
    up_to_msg_id = None
    if args.named:
        name = args.named.get("name")
        up_to_msg_id = args.named.get("msg_id")
        if "thread_id" in args.named or "threadId" in args.named:
            source_thread_id = args.named.get("thread_id") or args.named.get("threadId") or source_thread_id
    if args.positional:
        if len(args.positional) == 1:
            name = args.positional[0]
        elif len(args.positional) == 2:
            name = args.positional[0]
            up_to_msg_id = args.positional[1]
        elif len(args.positional) >= 3:
            source_thread_id = args.positional[0]
            name = args.positional[1]
            up_to_msg_id = args.positional[2]

    try:
        new_thread = duplicate_thread_up_to(db, source_thread_id, up_to_msg_id, name=name) if up_to_msg_id else duplicate_thread(db, source_thread_id, name=name)
    except Exception as e:
        _log_command_message(context, f"/duplicateThread error: {e}")
        return CommandResult(clear_input=False)
    _start_scheduler(context, new_thread)
    _log_command_message(context, f"Duplicated thread to new root: {new_thread[-8:]}")
    _set_current_thread(context, new_thread)
    _schedule_coro(context.watch_current_thread)
    _print_current_thread(context, heading=f"Switched to duplicated thread: {new_thread}")
    return CommandResult(clear_input=True, switched_thread=new_thread, start_schedulers=(new_thread,))


def _continue_thread_handler(context: CommandContext, arg: str) -> CommandResult:
    target = _command_db_and_thread(context, "continue")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        from .api import continue_thread, is_thread_continuable
        from .arg_parser import parse_args
    except Exception as e:
        _log_command_message(context, f"/continue error: {e}")
        return CommandResult(clear_input=False)

    args = parse_args(arg or "")
    msg_id = args.named.get("msg_id") or args.positional_or(0)
    delay_sec = args.get_float("wait")

    if not is_thread_continuable(db, current_thread):
        _log_command_message(context, "Thread cannot be continued (may be running or waiting for input)")
        return CommandResult(clear_input=False)

    if delay_sec is not None and delay_sec > 0:
        async def delayed_continue() -> None:
            await asyncio.sleep(delay_sec)
            result = continue_thread(db, current_thread, msg_id=msg_id)
            if result.success:
                _log_command_message(context, f"After {delay_sec}s delay: {result.message}")
                _print_current_thread(context, heading=f"Continued thread: {current_thread}")
            else:
                _log_command_message(context, f"/continue error: {result.message}")

        asyncio.get_running_loop().create_task(delayed_continue())
        _log_command_message(context, f"Continue scheduled in {delay_sec}s" + (f" from message {msg_id[-8:]}" if msg_id else ""))
        return CommandResult(clear_input=True)

    result = continue_thread(db, current_thread, msg_id=msg_id)
    if result.success:
        _log_command_message(context, result.message)
        _print_current_thread(context, heading=f"Continued thread: {current_thread}")
        return CommandResult(clear_input=True)
    _log_command_message(context, f"/continue error: {result.message}")
    return CommandResult(clear_input=False)


def _legacy_app_handler(method_name: str) -> CommandHandler:
    def handler(context: CommandContext, arg: str) -> CommandResult:
        app = context.app
        if app is None:
            raise RuntimeError(f"/{method_name[4:]} requires an app context")
        if method_name == "cmd_spawnChildThread":
            getattr(app, method_name)(arg, text=f"/{method_name[4:]} {arg}".rstrip())
        else:
            getattr(app, method_name)(arg)
        return CommandResult()

    return handler


def _register_legacy_command(
    registry: CommandRegistry,
    name: str,
    *,
    category: str,
    usage: str = "",
    description: str = "",
    aliases: tuple[str, ...] = (),
    method_name: str | None = None,
) -> None:
    registry.register(
        CommandSpec(
            name=name,
            aliases=aliases,
            category=category,
            usage=usage,
            description=description,
            handler=_legacy_app_handler(method_name or f"cmd_{name}"),
        )
    )


def create_default_command_registry() -> CommandRegistry:
    """Create the built-in slash-command registry.

    Handlers are initially thin adapters to the existing UI mixin methods so
    metadata, dispatch, and autocomplete can move to a registry before each
    command group's service layer is migrated.
    """

    registry = CommandRegistry()

    registry.register(CommandSpec("help", _core_help_handler, category="core", usage="/help", description="Show available commands."))
    registry.register(CommandSpec("quit", _core_quit_handler, category="core", usage="/quit", description="Exit the application."))
    registry.register(CommandSpec("reload", _core_reload_handler, category="core", usage="/reload", description="Restart Egg and reopen the current thread."))

    _register_legacy_command(registry, "model", category="model", usage="/model <key>", description="Set or display the active model.")
    _register_legacy_command(registry, "updateAllModels", category="model", usage="/updateAllModels <provider>", description="Refresh a provider model catalog.")
    _register_legacy_command(registry, "login", category="auth", usage="/login", description="Start ChatGPT OAuth login.")
    _register_legacy_command(registry, "logout", category="auth", usage="/logout", description="Clear ChatGPT OAuth tokens.")
    _register_legacy_command(registry, "authStatus", category="auth", usage="/authStatus", description="Show ChatGPT OAuth status.")

    registry.register(CommandSpec("toolsOn", _tools_on_handler, category="tools", usage="/toolsOn", description="Enable LLM tool calls for this thread."))
    registry.register(CommandSpec("toolsOff", _tools_off_handler, category="tools", usage="/toolsOff", description="Disable LLM tool calls for this thread."))
    registry.register(CommandSpec("disableTool", _disable_tool_handler, category="tools", usage="/disableTool <name>", description="Disable a specific tool for this thread."))
    registry.register(CommandSpec("enableTool", _enable_tool_handler, category="tools", usage="/enableTool <name>", description="Enable a specific tool for this thread."))
    registry.register(CommandSpec("toolsStatus", _tools_status_handler, category="tools", usage="/toolsStatus", description="Show tool configuration and availability."))
    registry.register(CommandSpec("toolInfo", _tool_info_handler, category="tools", usage="/toolInfo <name>", description="Show a tool schema and metadata."))
    registry.register(CommandSpec("toolsSecrets", _tools_secrets_handler, category="tools", usage="/toolsSecrets <on|off>", description="Toggle raw tool output in local UI."))
    registry.register(CommandSpec("toggleAutoApproval", _toggle_auto_approval_handler, category="tools", usage="/toggleAutoApproval", description="Toggle global tool auto-approval."))
    _register_legacy_command(registry, "schedulers", category="tools", usage="/schedulers", description="List active schedulers.")

    registry.register(CommandSpec("threads", _threads_list_handler, category="threads", usage="/threads", description="List threads."))
    registry.register(CommandSpec("thread", _thread_switch_handler, category="threads", usage="/thread <selector>", description="Switch to a thread."))
    registry.register(CommandSpec("newThread", _threads_new_handler, category="threads", usage="/newThread <name>", description="Create a new root thread."))
    registry.register(CommandSpec("deleteThread", _delete_thread_handler, category="threads", usage="/deleteThread <selector>", description="Delete a thread."))
    registry.register(CommandSpec("duplicateThread", _duplicate_thread_handler, category="threads", usage="/duplicateThread <name> [msg_id]", description="Duplicate the current thread."))
    registry.register(CommandSpec("parentThread", _parent_thread_handler, category="threads", usage="/parentThread", description="Switch to the parent thread."))
    registry.register(CommandSpec("listChildren", _list_children_handler, category="threads", usage="/listChildren", description="List child threads."))
    registry.register(CommandSpec("continue", _continue_thread_handler, category="threads", usage="/continue [msg_id=<id>]", description="Continue a thread from a specific point."))

    _register_legacy_command(registry, "spawnChildThread", category="subagents", usage="/spawnChildThread <text>", description="Spawn a child thread.")
    _register_legacy_command(registry, "spawnAutoApprovedChildThread", category="subagents", usage="/spawnAutoApprovedChildThread <text>", description="Spawn an auto-approved child thread.")
    _register_legacy_command(registry, "waitForThreads", category="subagents", usage="/waitForThreads <threads>", description="Wait for child threads.")

    _register_legacy_command(registry, "sessionStatus", category="session", usage="/sessionStatus", description="Show persistent session status.")
    _register_legacy_command(registry, "sessionOn", category="session", usage="/sessionOn [provider=docker|memory]", description="Enable persistent sessions.")
    _register_legacy_command(registry, "sessionOff", category="session", usage="/sessionOff", description="Disable persistent sessions.")
    _register_legacy_command(registry, "sessionStop", category="session", usage="/sessionStop [python|bash|all]", description="Stop session runtimes.")
    _register_legacy_command(registry, "sessionReset", category="session", usage="/sessionReset [python|bash|all]", description="Reset session runtimes.")
    _register_legacy_command(registry, "sessionCleanup", category="session", usage="/sessionCleanup [stopped|all] [older_than=1h]", description="Clean up session containers.")
    _register_legacy_command(registry, "pythonRepl", category="session", usage="/pythonRepl <code>", description="Run code in the persistent Python REPL.")
    _register_legacy_command(registry, "bashRepl", category="session", usage="/bashRepl <script>", description="Run script in the persistent bash REPL.")

    _register_legacy_command(registry, "toggleSandboxing", category="sandbox", usage="/toggleSandboxing", description="Toggle sandboxing for the thread subtree.")
    _register_legacy_command(registry, "setSandboxConfiguration", category="sandbox", usage="/setSandboxConfiguration <file.json>", description="Apply sandbox configuration.")
    _register_legacy_command(registry, "getSandboxingConfig", category="sandbox", usage="/getSandboxingConfig", description="Show current sandbox configuration.")

    _register_legacy_command(registry, "skills", category="skills", usage="/skills [query]", description="List or search packaged skills.")
    _register_legacy_command(registry, "skill", category="skills", usage="/skill <name>", description="Show and load a packaged skill.")

    _register_legacy_command(registry, "startSearxng", category="web", usage="/startSearxng", description="Start the local SearXNG backend.")
    _register_legacy_command(registry, "stopSearxng", category="web", usage="/stopSearxng", description="Stop the local SearXNG backend.")

    _register_legacy_command(registry, "togglePanel", category="display", usage="/togglePanel <chat|children|system>", description="Show or hide a panel.")
    _register_legacy_command(registry, "toggleBorders", category="display", usage="/toggleBorders", description="Toggle panel borders.")
    _register_legacy_command(registry, "redraw", category="display", usage="/redraw", description="Redraw the static transcript.")
    _register_legacy_command(registry, "displayMode", category="display", usage="/displayMode <full-screen|inline>", description="Switch display mode.")
    _register_legacy_command(registry, "paste", category="input", usage="/paste", description="Paste clipboard content into the input panel.")
    _register_legacy_command(registry, "enterMode", category="input", usage="/enterMode <send|newline>", description="Set Enter key behavior.")

    _register_legacy_command(registry, "cost", category="diagnostics", usage="/cost", description="Show token usage and approximate cost.")
    _register_legacy_command(registry, "setContextLimit", category="diagnostics", usage="/setContextLimit [limit]", description="Set or show the thread context limit.")
    _register_legacy_command(registry, "setThreadPriority", category="diagnostics", usage="/setThreadPriority ...", description="Set thread scheduler settings.")

    return registry


def command_completion_names(registry: CommandRegistry | None = None) -> list[str]:
    registry = registry or create_default_command_registry()
    return [f"/{name}" for name in registry.names(include_aliases=True)]


def create_default_input_prefix_registry() -> InputPrefixRegistry:
    """Create built-in non-slash input-prefix handlers.

    Handlers are initially thin adapters to the existing UI methods. The
    execution plugin can migrate `$`/`$$` onto shared services in a later step.
    """

    registry = InputPrefixRegistry()

    def enqueue_bash(context: CommandContext, arg: str, *, hidden: bool) -> CommandResult:
        app = context.app
        if app is None:
            raise RuntimeError("Bash input prefixes require an app context")
        app.enqueue_bash_tool(arg.strip(), hidden=hidden)
        return CommandResult(clear_input=True)

    registry.register(
        InputPrefixSpec(
            prefix="$$",
            handler=lambda context, arg: enqueue_bash(context, arg, hidden=True),
            description="Run a hidden bash command; output is stored locally and hidden from the model.",
        )
    )
    registry.register(
        InputPrefixSpec(
            prefix="$",
            handler=lambda context, arg: enqueue_bash(context, arg, hidden=False),
            description="Run a bash command as a user-originated tool call.",
        )
    )
    return registry


SESSION_COMMAND_COMPLETIONS: List[str] = [
    '/sessionStatus',
    '/sessionOn',
    '/sessionOff',
    '/sessionStop',
    '/sessionReset',
    '/sessionCleanup',
    '/pythonRepl',
    '/bashRepl',
]

SESSION_ON_COMPLETIONS: List[str] = [
    'provider=docker',
    'provider=memory',
    'image=egg-rlm-session',
    'share_with_children=true',
    'share_with_children=false',
    'share_repl=true',
    'share_repl=false',
]

SESSION_TARGET_COMPLETIONS: List[str] = ['python', 'bash', 'all']

EGG_COMMAND_COMPLETIONS: List[str] = command_completion_names()

EGGW_COMMAND_COMPLETIONS: List[str] = [
    *EGG_COMMAND_COMPLETIONS,
    # Web-only aliases/options.
    '/spawn',
    '/theme',
]


__all__ = [
    'CommandContext',
    'CommandHandler',
    'CommandCompleter',
    'CommandRegistry',
    'CommandResult',
    'CommandSpec',
    'InputPrefixHandler',
    'InputPrefixRegistry',
    'InputPrefixSpec',
    'command_completion_names',
    'create_default_command_registry',
    'create_default_input_prefix_registry',
    'render_command_registry_help',
    'SESSION_COMMAND_COMPLETIONS',
    'SESSION_ON_COMPLETIONS',
    'SESSION_TARGET_COMPLETIONS',
    'EGG_COMMAND_COMPLETIONS',
    'EGGW_COMMAND_COMPLETIONS',
]
