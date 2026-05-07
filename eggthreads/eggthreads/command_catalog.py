from __future__ import annotations

"""Shared UI command/autocomplete catalog for Egg frontends."""

from dataclasses import dataclass
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
    app: Any = None


CommandHandler = Callable[[CommandContext, str], CommandResult | None]
CommandCompleter = Callable[[CommandContext, str], Iterable[str | Mapping[str, Any]]]


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

    _register_legacy_command(registry, "help", category="core", usage="/help", description="Show available commands.")
    _register_legacy_command(registry, "quit", category="core", usage="/quit", description="Exit the application.")
    _register_legacy_command(registry, "reload", category="core", usage="/reload", description="Restart Egg and reopen the current thread.")

    _register_legacy_command(registry, "model", category="model", usage="/model <key>", description="Set or display the active model.")
    _register_legacy_command(registry, "updateAllModels", category="model", usage="/updateAllModels <provider>", description="Refresh a provider model catalog.")
    _register_legacy_command(registry, "login", category="auth", usage="/login", description="Start ChatGPT OAuth login.")
    _register_legacy_command(registry, "logout", category="auth", usage="/logout", description="Clear ChatGPT OAuth tokens.")
    _register_legacy_command(registry, "authStatus", category="auth", usage="/authStatus", description="Show ChatGPT OAuth status.")

    _register_legacy_command(registry, "toolsOn", category="tools", usage="/toolsOn", description="Enable LLM tool calls for this thread.")
    _register_legacy_command(registry, "toolsOff", category="tools", usage="/toolsOff", description="Disable LLM tool calls for this thread.")
    _register_legacy_command(registry, "disableTool", category="tools", usage="/disableTool <name>", description="Disable a specific tool for this thread.")
    _register_legacy_command(registry, "enableTool", category="tools", usage="/enableTool <name>", description="Enable a specific tool for this thread.")
    _register_legacy_command(registry, "toolsStatus", category="tools", usage="/toolsStatus", description="Show tool configuration and availability.")
    _register_legacy_command(registry, "toolInfo", category="tools", usage="/toolInfo <name>", description="Show a tool schema and metadata.")
    _register_legacy_command(registry, "toolsSecrets", category="tools", usage="/toolsSecrets <on|off>", description="Toggle raw tool output in local UI.")
    _register_legacy_command(registry, "toggleAutoApproval", category="tools", usage="/toggleAutoApproval", description="Toggle global tool auto-approval.")
    _register_legacy_command(registry, "schedulers", category="tools", usage="/schedulers", description="List active schedulers.")

    _register_legacy_command(registry, "threads", category="threads", usage="/threads", description="List threads.")
    _register_legacy_command(registry, "thread", category="threads", usage="/thread <selector>", description="Switch to a thread.")
    _register_legacy_command(registry, "newThread", category="threads", usage="/newThread <name>", description="Create a new root thread.")
    _register_legacy_command(registry, "deleteThread", category="threads", usage="/deleteThread <selector>", description="Delete a thread.")
    _register_legacy_command(registry, "duplicateThread", category="threads", usage="/duplicateThread <name> [msg_id]", description="Duplicate the current thread.")
    _register_legacy_command(registry, "parentThread", category="threads", usage="/parentThread", description="Switch to the parent thread.")
    _register_legacy_command(registry, "listChildren", category="threads", usage="/listChildren", description="List child threads.")
    _register_legacy_command(registry, "continue", category="threads", usage="/continue [msg_id=<id>]", description="Continue a thread from a specific point.")

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
    'command_completion_names',
    'create_default_command_registry',
    'SESSION_COMMAND_COMPLETIONS',
    'SESSION_ON_COMPLETIONS',
    'SESSION_TARGET_COMPLETIONS',
    'EGG_COMMAND_COMPLETIONS',
    'EGGW_COMMAND_COMPLETIONS',
]
