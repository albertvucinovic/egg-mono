from __future__ import annotations

import asyncio
import inspect
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
    stream: "ToolStreamContext | None" = None


@dataclass(frozen=True)
class ToolStreamContext:
    """Live stream/audit hooks exposed to streaming tool implementations."""

    db: Any
    thread_id: str
    invoke_id: str
    tool_call_id: str
    tool_name: str
    current_model: str | None = None
    heartbeat: Callable[[], bool] | None = None
    emit_delta: Callable[[str], bool] | None = None
    emit_summary: Callable[[str], None] | None = None

    def stream_delta(self, text: str) -> bool:
        if self.emit_delta is None:
            return True
        return self.emit_delta(text)

    def summary(self, text: str) -> None:
        if self.emit_summary is not None:
            self.emit_summary(text)


@dataclass(frozen=True)
class ToolExecutionResult:
    """Structured result for tools that can report execution metadata."""

    output: str
    reason: str = "success"
    streamed: bool = False


@dataclass(frozen=True)
class ToolCapabilities:
    """Metadata describing optional tool execution capabilities."""

    supports_streaming: bool = False
    supports_cancellation: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: "ToolCapabilities | Mapping[str, Any] | None") -> "ToolCapabilities":
        if isinstance(value, ToolCapabilities):
            return value
        if not value:
            return cls()
        known = {"supports_streaming", "supports_cancellation"}
        return cls(
            supports_streaming=bool(value.get("supports_streaming", False)),
            supports_cancellation=bool(value.get("supports_cancellation", False)),
            metadata={k: v for k, v in value.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.metadata)
        data["supports_streaming"] = self.supports_streaming
        data["supports_cancellation"] = self.supports_cancellation
        return data


class ToolRegistry:
    """Simple registry for OpenAI function-call compatible tools.

    - tools_spec() returns the JSON schema list to pass to the LLM
    - execute(name, arguments) dispatches to the registered callable
    - execute_async(name, arguments) dispatches and awaits async callables
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
        capabilities: ToolCapabilities | Mapping[str, Any] | None = None,
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
            capabilities: Optional metadata describing execution features such
                as live streaming or cancellation support. This is registry
                metadata only; it is not exposed in the LLM tool schema.
        """
        tool_capabilities = ToolCapabilities.from_value(capabilities)
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
            "capabilities": tool_capabilities,
        }

    def tools_spec(self) -> List[Dict[str, Any]]:
        """Return the list of tool specs to expose to the LLM.

        Tools marked local_only=True are omitted so they can be used by the
        UI (RA3 user commands, etc.) without being surfaced as model tools.
        """
        return [d["spec"] for d in self._tools.values() if not d.get("local_only")]

    def capabilities(self, name: str) -> ToolCapabilities:
        entry = self._tools.get(name)
        if not entry:
            raise KeyError(f"Unknown tool: {name}")
        return entry["capabilities"]

    def _prepare_call(
        self,
        name: str,
        arguments: Any,
        context: Mapping[str, Any],
    ) -> tuple[Callable[..., Any], Dict[str, Any], ToolContext | None]:
        entry = self._tools.get(name)
        if not entry:
            raise KeyError(f"Unknown tool: {name}")
        impl = entry["impl"]
        accepts_context = bool(entry.get("accepts_context"))
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
        if not accepts_context and thread_id and "parent_thread_id" not in args and "_thread_id" not in args:
            args["_thread_id"] = thread_id

        # Similarly, propagate the calling thread's model key so other
        # tools can inherit or inspect it when needed. Spawn tools no
        # longer use this implicit override for model selection; child
        # threads should inherit from their parent thread directly.
        init_m = context.get("initial_model_key")
        if not accepts_context and init_m and "initial_model_key" not in args and "_initial_model_key" not in args:
            args["_initial_model_key"] = init_m

        # Propagate tool timeout for subprocess-based tools
        tool_timeout = context.get("tool_timeout_sec")
        repl_tool_timeout = args.pop("_egg_tool_timeout_sec", None)
        if repl_tool_timeout is not None:
            tool_timeout = repl_tool_timeout
        if not accepts_context and tool_timeout is not None and "_tool_timeout_sec" not in args:
            args["_tool_timeout_sec"] = tool_timeout

        # Propagate cancel check callback for interruptible tools
        cancel_check = context.get("cancel_check")
        if not accepts_context and cancel_check is not None and "_cancel_check" not in args:
            args["_cancel_check"] = cancel_check

        timeout_args = dict(args)
        if tool_timeout is not None and "_tool_timeout_sec" not in timeout_args:
            timeout_args["_tool_timeout_sec"] = tool_timeout

        tool_ctx = ToolContext(
            db=context.get("db"),
            thread_id=thread_id,
            invoke_id=context.get("invoke_id"),
            origin=context.get("origin"),
            initial_model_key=init_m,
            timeout_sec=resolve_tool_timeout_arg(timeout_args),
            cancel_check=cancel_check,
            working_dir=context.get("working_dir"),
            raw=dict(context),
            stream=context.get("stream"),
        )

        return impl, args, tool_ctx if accepts_context else None

    def _call(self, name: str, arguments: Any, context: Mapping[str, Any]) -> Any:
        impl, args, tool_ctx = self._prepare_call(name, arguments, context)
        if tool_ctx is not None:
            return impl(args, tool_ctx)
        return impl(args)

    @staticmethod
    async def _await_result(result: Any) -> Any:
        return await result

    @classmethod
    def _run_awaitable_sync(cls, result: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(cls._await_result(result))
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise RuntimeError("Cannot synchronously execute an async tool while an event loop is running; use execute_async().")

    def execute(self, name: str, arguments: Any, **context: Any) -> Any:
        result = self._call(name, arguments, context)
        if inspect.isawaitable(result):
            result = self._run_awaitable_sync(result)
        if isinstance(result, ToolExecutionResult) and not context.get("preserve_tool_result"):
            return result.output
        return result

    async def execute_async(self, name: str, arguments: Any, **context: Any) -> Any:
        impl, args, tool_ctx = self._prepare_call(name, arguments, context)
        if inspect.iscoroutinefunction(impl):
            result = impl(args, tool_ctx) if tool_ctx is not None else impl(args)
        else:
            result = await asyncio.to_thread(
                impl,
                args,
                tool_ctx,
            ) if tool_ctx is not None else await asyncio.to_thread(impl, args)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolExecutionResult) and not context.get("preserve_tool_result"):
            return result.output
        return result


def create_tool_registry() -> ToolRegistry:
    """Create a plugin-populated ToolRegistry with Egg's built-in tools."""

    from .builtin_plugins import CompactionPlugin, ExecutionPlugin, SessionPlugin, SkillsPlugin, SubagentsPlugin, WebPlugin
    from .plugins import ToolPluginContext, register_plugins

    reg = ToolRegistry()
    register_plugins(
        ToolPluginContext(tool_registry=reg),
        [
            SkillsPlugin(),
            CompactionPlugin(),
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

