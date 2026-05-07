from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping


def resolve_tool_timeout_arg(
    args: Dict[str, Any],
    *,
    config_key: str = "_tool_timeout_sec",
) -> float | None:
    """Resolve tool subprocess timeout from decoded tool arguments.

    Priority matches the runner helper: LLM/tool-call ``timeout_sec`` first,
    then the runner-injected config/default timeout under ``config_key``.
    Invalid or non-positive values are treated as absent, so callers get a
    single, consistent interpretation across tool implementations.
    """
    for candidate in (args.get('timeout_sec'), args.get(config_key)):
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None



@dataclass(frozen=True)
class ToolContext:
    """Execution context for context-aware tool implementations.

    Existing tools still receive only their decoded argument dict. New tools can
    opt in to receiving this object by registering with ``accepts_context=True``.
    """

    db: Any = None
    thread_id: str | None = None
    invoke_id: str | None = None
    origin: str | None = None
    initial_model_key: str | None = None
    timeout_sec: float | None = None
    cancel_check: Callable[[], bool] | None = None
    working_dir: Any = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Simple registry for OpenAI function-call compatible tools.

    - tools_spec() returns the JSON schema list to pass to the LLM
    - execute(name, arguments) dispatches to the registered callable
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters_schema: Dict[str, Any],
        impl: Callable[..., Any],
        local_only: bool = False,
        accepts_context: bool = False,
    ):
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
            accepts_context: If True, impl is called as ``impl(args, ctx)``
                where ctx is a ToolContext. Existing tools should leave this
                False and continue to receive only ``args``.
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
            "accepts_context": accepts_context,
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
        repl_tool_timeout = args.pop("_egg_tool_timeout_sec", None)
        if repl_tool_timeout is not None:
            tool_timeout = repl_tool_timeout
        if tool_timeout is not None and "_tool_timeout_sec" not in args:
            args["_tool_timeout_sec"] = tool_timeout

        # Propagate cancel check callback for interruptible tools
        cancel_check = context.get("cancel_check")
        if cancel_check is not None and "_cancel_check" not in args:
            args["_cancel_check"] = cancel_check

        tool_ctx = ToolContext(
            db=context.get("db"),
            thread_id=thread_id,
            invoke_id=context.get("invoke_id"),
            origin=context.get("origin"),
            initial_model_key=init_m,
            timeout_sec=resolve_tool_timeout_arg(args),
            cancel_check=cancel_check,
            working_dir=context.get("working_dir"),
            raw=dict(context),
        )

        if entry.get("accepts_context"):
            return impl(args, tool_ctx)
        return impl(args)


def create_tool_registry() -> ToolRegistry:
    """Create a plugin-populated ToolRegistry with Egg's built-in tools."""

    from .builtin_plugins import ExecutionPlugin, SessionPlugin, SkillsPlugin, SubagentsPlugin, WebPlugin
    from .plugins import ToolPluginContext, register_plugins

    reg = ToolRegistry()
    register_plugins(
        ToolPluginContext(tool_registry=reg),
        [
            SkillsPlugin(),
            ExecutionPlugin(),
            SessionPlugin(),
            SubagentsPlugin(),
            WebPlugin(),
        ],
    )
    return reg


# Default tools similar to chat.sh
def create_default_tools() -> ToolRegistry:
    """Create a ToolRegistry with the default set of tools.

    Returns a registry pre-populated with common tools:
    - bash: Execute shell commands
    - python: Execute Python scripts
    - spawn_agent: Create child threads for delegation
    - spawn_agent_auto: Create auto-approved child threads
    - web_search: Web search via the configured backend (SearXNG by default)
    - fetch_url: Fetch and extract readable markdown for a URL
    - wait: Synchronize on child thread completion

    Returns:
        ToolRegistry with default tools registered.
    """
    return create_tool_registry()

