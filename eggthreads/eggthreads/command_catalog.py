from __future__ import annotations

"""Shared UI command/autocomplete catalog for Egg frontends."""

import os
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from inspect import isawaitable, iscoroutinefunction
from typing import Any, Awaitable, Callable, Iterable, List, Mapping


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
    append_message: Callable[..., Any] | None = None
    create_snapshot: Callable[..., Any] | None = None
    approve_tool_calls: Callable[..., Any] | None = None
    models_path: str | Path | None = None
    all_models_path: str | Path | None = None
    image_generation_models_path: str | Path | None = None
    app: Any = None


CommandHandler = Callable[[CommandContext, str], CommandResult | None | Awaitable[CommandResult | None]]
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
        normalized = _normalize_command_name(name)
        spec = self.get(normalized)
        logged_messages: list[str] = []
        original_log_system = context.log_system

        def capture_log(message: str) -> None:
            text = str(message).strip()
            if text:
                logged_messages.append(text)
            if original_log_system is not None:
                original_log_system(message)

        run_context = replace(context, log_system=capture_log)
        raw_result = spec.handler(run_context, arg)
        if isawaitable(raw_result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                raw_result = asyncio.run(raw_result)
            else:
                raise RuntimeError(f"Command /{normalized} is async; use execute_async().")
        result = raw_result if isinstance(raw_result, CommandResult) else CommandResult()
        message = result.message.strip() if isinstance(result.message, str) else ""
        if not message:
            if logged_messages:
                message = "\n".join(logged_messages)
            elif result.exit_app:
                message = f"/{normalized} accepted."
            elif result.clear_input:
                message = f"/{normalized} completed."
            else:
                message = f"/{normalized} did not complete."
        return replace(result, message=message)

    async def execute_async(self, name: str, context: CommandContext, arg: str = "") -> CommandResult:
        normalized = _normalize_command_name(name)
        spec = self.get(normalized)
        logged_messages: list[str] = []
        original_log_system = context.log_system

        def capture_log(message: str) -> None:
            text = str(message).strip()
            if text:
                logged_messages.append(text)
            if original_log_system is not None:
                original_log_system(message)

        run_context = replace(context, log_system=capture_log)
        raw_result = spec.handler(run_context, arg)
        if isawaitable(raw_result):
            raw_result = await raw_result
        result = raw_result if isinstance(raw_result, CommandResult) else CommandResult()
        message = result.message.strip() if isinstance(result.message, str) else ""
        if not message:
            if logged_messages:
                message = "\n".join(logged_messages)
            elif result.exit_app:
                message = f"/{normalized} accepted."
            elif result.clear_input:
                message = f"/{normalized} completed."
            else:
                message = f"/{normalized} did not complete."
        return replace(result, message=message)

    def is_async(self, name: str) -> bool:
        """Return True when the command handler is declared async."""

        return iscoroutinefunction(self.get(name).handler)

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
        category = spec.category or "general"
        if category in {"threads", "subagents"} or spec.name in {"schedulers", "setThreadPriority"}:
            category = "threads/agents/subagents"
        categories.setdefault(category, []).append(spec)

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
    return CommandResult(clear_input=True, message="Help (see console for full).")


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

    if _subtree_has_active_stream(context.db, thread_id):
        if context.log_system is not None:
            context.log_system("/reload skipped: current thread or a subthread is streaming.")
        return CommandResult(clear_input=False, message="/reload skipped: current thread or a subthread is streaming.")

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


def _subtree_has_active_stream(db: Any, thread_id: str) -> bool:
    """Return True if the thread or any descendant has an active stream."""
    if db is None or not thread_id:
        return False
    try:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cur = db.conn.execute(
            """
            WITH RECURSIVE subtree(thread_id) AS (
                SELECT ?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN subtree s ON c.parent_id = s.thread_id
            )
            SELECT 1
            FROM open_streams o
            JOIN subtree s ON o.thread_id = s.thread_id
            WHERE o.lease_until > ?
            LIMIT 1
            """,
            (thread_id, now_iso),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def create_default_command_registry() -> CommandRegistry:
    """Create the built-in slash-command registry.

    Built-in feature plugins own command handlers; this registry wires their
    metadata and dispatch in deterministic order.
    """

    registry = CommandRegistry()

    registry.register(CommandSpec("help", _core_help_handler, category="core", usage="/help", description="Show available commands."))
    registry.register(CommandSpec("quit", _core_quit_handler, category="core", usage="/quit", description="Exit the application."))
    registry.register(CommandSpec("reload", _core_reload_handler, category="core", usage="/reload", description="Restart Egg and reopen the current thread."))

    from .builtin_plugins import AuthPlugin, ModelPlugin
    from .plugins import CommandPluginContext, register_plugins

    register_plugins(CommandPluginContext(command_registry=registry), [ModelPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [AuthPlugin()])

    from .builtin_plugins import DiagnosticsPlugin, OutputOptimizerAdminPlugin, ToolsAdminPlugin

    register_plugins(CommandPluginContext(command_registry=registry), [ToolsAdminPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [OutputOptimizerAdminPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [DiagnosticsPlugin()])

    from .builtin_plugins import AnswerUserPlugin, CompactionPlugin, DisplayInputPlugin, SandboxAdminPlugin, SessionPlugin, SkillsPlugin, SubagentsPlugin, ThreadUiPlugin, WebPlugin

    register_plugins(CommandPluginContext(command_registry=registry), [ThreadUiPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [AnswerUserPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [CompactionPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [SubagentsPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [SessionPlugin()])
    register_plugins(CommandPluginContext(command_registry=registry), [SandboxAdminPlugin()])

    register_plugins(CommandPluginContext(command_registry=registry), [SkillsPlugin()])

    register_plugins(CommandPluginContext(command_registry=registry), [WebPlugin()])

    register_plugins(CommandPluginContext(command_registry=registry), [DisplayInputPlugin()])

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

OUTPUT_OPTIMIZER_COMMAND_COMPLETIONS: List[str] = [
    '/outputOptimizerStatus',
    '/outputOptimizerOn',
    '/outputOptimizerOff',
    '/outputOptimizerMode',
]

OUTPUT_OPTIMIZER_MODE_COMPLETIONS: List[str] = ['conservative', 'balanced', 'aggressive']

EGG_COMMAND_COMPLETIONS: List[str] = command_completion_names()

EGGW_COMMAND_COMPLETIONS: List[str] = [
    *EGG_COMMAND_COMPLETIONS,
    '/attach',
    '/attachments',
    '/attachOutput',
    '/saveProviderArtifact',
    '/saveProviderOutput',
    '/clearAttachments',
    '/imageGenerate',
    '/editAnswer',
    '/editor',
    # Web-only options.
    '/rename',
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
    'OUTPUT_OPTIMIZER_COMMAND_COMPLETIONS',
    'OUTPUT_OPTIMIZER_MODE_COMPLETIONS',
    'EGG_COMMAND_COMPLETIONS',
    'EGGW_COMMAND_COMPLETIONS',
]
