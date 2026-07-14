from __future__ import annotations

import asyncio
import copy
import inspect
import json
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping


_TOOL_SUMMARY_EMIT_INTERVAL_SEC = 5.0
_TOOL_DEADLINE_CLEANUP_GRACE_SEC = 0.25

TOOL_TIMEOUT_ARGUMENT_NAMES = (
    "timeout",
    "timeout_sec",
    "timeout_seconds",
    "timeout_secs",
    "timeout_s",
)
TOOL_INTERNAL_TIMEOUT_ARGUMENT_NAMES = ("_tool_timeout_sec", "_egg_tool_timeout_sec")
TOOL_TIMEOUT_SCHEMA: Dict[str, Any] = {
    "type": "number",
    "description": "Optional maximum seconds to allow this tool call to run.",
}


def _should_emit_tool_summary(
    last_summary_at: float | None,
    now: float,
    *,
    interval_sec: float = _TOOL_SUMMARY_EMIT_INTERVAL_SEC,
) -> bool:
    """Return True for the first summary, then only at a sparse cadence."""

    try:
        if last_summary_at is None:
            return True
        return (float(now) - float(last_summary_at)) >= max(0.0, float(interval_sec))
    except Exception:
        return True


def resolve_tool_timeout_arg(
    args: Dict[str, Any],
    *,
    config_key: str = "_tool_timeout_sec",
) -> float | None:
    """Resolve tool subprocess timeout from decoded tool arguments.

    ``timeout`` is the canonical public argument.  Legacy/public aliases such
    as ``timeout_sec`` and runner/bridge-internal keys are accepted so old
    transcripts, generated wrappers, and programmatic callers keep working.
    Invalid or non-positive values are treated as absent, so callers get a
    single, consistent interpretation across tool implementations.
    """
    keys = [*TOOL_TIMEOUT_ARGUMENT_NAMES]
    if config_key:
        keys.append(config_key)
    for key in TOOL_INTERNAL_TIMEOUT_ARGUMENT_NAMES:
        if key not in keys:
            keys.append(key)
    for key in keys:
        candidate = args.get(key)
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def with_canonical_timeout_parameter(parameters_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON schema copy with the canonical cross-cutting timeout arg.

    Tool implementations can still accept legacy aliases, but the schema shown
    to models and generated REPL wrappers should advertise one public name:
    ``timeout``.
    """

    schema = copy.deepcopy(parameters_schema) if isinstance(parameters_schema, dict) else {}
    schema.setdefault("type", "object")
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
        schema["properties"] = props
    legacy_timeout = props.pop("timeout_sec", None)
    props.setdefault("timeout", legacy_timeout if isinstance(legacy_timeout, dict) else TOOL_TIMEOUT_SCHEMA)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = ["timeout" if item == "timeout_sec" else item for item in required]
    return schema



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
    tool_call_id: str | None = None
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
    publication_presentation: Mapping[str, Any] = field(default_factory=dict)

    def presented_output(self) -> str:
        from .tool_output_presentation import apply_output_presentation

        return apply_output_presentation(self.output, self.publication_presentation)


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


class _ToolCancellationController:
    """Thread-safe local cancellation composed with an upstream callback."""

    def __init__(self, upstream: Callable[[], bool] | None = None):
        self._event = threading.Event()
        self._upstream = upstream

    def cancel(self) -> None:
        self._event.set()

    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._upstream is None:
            return False
        try:
            if self._upstream():
                self._event.set()
                return True
        except Exception:
            # A failed status probe does not establish cancellation.
            return False
        return False


def _consume_tool_task_result(task: "asyncio.Task[Any]") -> None:
    """Retrieve a detached task's exception so asyncio does not warn later."""

    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def _wait_for_tool_deadline(timeout_sec: float) -> None:
    """Wait on asyncio's monotonic event-loop clock for a tool deadline."""

    await asyncio.sleep(timeout_sec)


async def _finish_or_detach_tool_task(task: "asyncio.Task[Any]") -> Any:
    """Give cooperative cleanup a bound, then cancel/detach the await."""

    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=_TOOL_DEADLINE_CLEANUP_GRACE_SEC,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_tool_task_result)
        raise
    if task in done:
        try:
            return task.result()
        except (asyncio.CancelledError, Exception):
            return None

    task.cancel()
    task.add_done_callback(_consume_tool_task_result)
    # Let ordinary async ``finally`` blocks begin without waiting for a task
    # that suppresses cancellation. A to_thread await is cancelled here, not
    # the underlying arbitrary Python thread.
    await asyncio.sleep(0)
    return None


def _tool_timeout_result(
    timeout_sec: float,
    cleanup_result: Any = None,
) -> ToolExecutionResult:
    """Build the authority timeout, retaining useful cooperative output."""

    if isinstance(cleanup_result, ToolExecutionResult):
        return replace(cleanup_result, reason="timeout")
    limit = f"{float(timeout_sec):g}"
    return ToolExecutionResult(
        f"--- TIMEOUT ---\nTool execution timed out after {limit} seconds.",
        reason="timeout",
    )


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
        parameters_schema = with_canonical_timeout_parameter(parameters_schema)
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
        if not accepts_context and thread_id:
            # Context identity is authoritative for model/runner-originated
            # tool calls.  Do not let caller-supplied parent_thread_id or
            # _thread_id redirect a tool to another thread.
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

        raw_context = dict(context)
        raw_context["tool_registry"] = self

        tool_ctx = ToolContext(
            db=context.get("db"),
            thread_id=thread_id,
            invoke_id=context.get("invoke_id"),
            origin=context.get("origin"),
            initial_model_key=init_m,
            timeout_sec=resolve_tool_timeout_arg(timeout_args),
            cancel_check=cancel_check,
            working_dir=context.get("working_dir"),
            tool_call_id=context.get("tool_call_id"),
            raw=raw_context,
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
            return result.presented_output()
        return result

    async def execute_async(self, name: str, arguments: Any, **context: Any) -> Any:
        """Execute a tool under the registry's authoritative deadline.

        Async implementations are cancelled after a bounded cooperative cleanup
        window.  Sync implementations run through :func:`asyncio.to_thread`; the
        same cancellation signal asks them to stop, but Python cannot forcibly
        terminate an arbitrary executor thread.  If such a thread ignores the
        signal, its await is detached after the durable caller can safely treat
        this method's timeout result as terminal.
        """

        impl, args, tool_ctx = self._prepare_call(name, arguments, context)
        upstream_cancel_check = (
            tool_ctx.cancel_check
            if tool_ctx is not None
            else args.get("_cancel_check")
        )
        timeout_sec = (
            tool_ctx.timeout_sec
            if tool_ctx is not None
            else resolve_tool_timeout_arg(args)
        )
        controller = _ToolCancellationController(upstream_cancel_check)

        # Every implementation receives one composed signal.  It becomes true
        # for caller/lease cancellation and for this call's local deadline.
        if tool_ctx is not None:
            raw_context = dict(tool_ctx.raw)
            raw_context["cancel_check"] = controller.cancelled
            tool_ctx = replace(
                tool_ctx,
                cancel_check=controller.cancelled,
                raw=raw_context,
            )
        else:
            args["_cancel_check"] = controller.cancelled

        async def invoke() -> Any:
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
            return result

        implementation_task = asyncio.create_task(invoke())
        deadline_wait: asyncio.Task[Any] | None = None
        try:
            if timeout_sec is None:
                result = await asyncio.shield(implementation_task)
            else:
                deadline_wait = asyncio.create_task(_wait_for_tool_deadline(timeout_sec))
                done, _pending = await asyncio.wait(
                    {implementation_task, deadline_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if implementation_task in done:
                    # A completed result that is observable when the deadline
                    # wake-up is handled wins the race.
                    result = implementation_task.result()
                else:
                    controller.cancel()
                    cleanup_result = await _finish_or_detach_tool_task(implementation_task)
                    result = _tool_timeout_result(timeout_sec, cleanup_result)
        except asyncio.CancelledError:
            # Shielding/waiting keeps cancellation from reaching the
            # implementation before its cooperative signal is set.  Start
            # bounded cleanup without swallowing the caller's cancellation.
            controller.cancel()
            cleanup_task = asyncio.create_task(_finish_or_detach_tool_task(implementation_task))
            cleanup_task.add_done_callback(_consume_tool_task_result)
            raise
        finally:
            if deadline_wait is not None:
                deadline_wait.cancel()
                deadline_wait.add_done_callback(_consume_tool_task_result)

        if isinstance(result, ToolExecutionResult) and not context.get("preserve_tool_result"):
            return result.presented_output()
        return result


def create_tool_registry() -> ToolRegistry:
    """Create a plugin-populated ToolRegistry with Egg's built-in tools."""

    from .builtin_plugins import AnswerUserPlugin, AttachmentToolsPlugin, CompactionPlugin, ExecutionPlugin, ImageGenerationPlugin, LongOutputPlugin, SessionPlugin, SkillsPlugin, SubagentsPlugin, ToolHelpPlugin, ToolOutputExtractionPlugin, WebPlugin
    from .plugins import ToolPluginContext, register_plugins

    reg = ToolRegistry()
    register_plugins(
        ToolPluginContext(tool_registry=reg),
        [
            AnswerUserPlugin(),
            SkillsPlugin(),
            CompactionPlugin(),
            LongOutputPlugin(),
            ToolOutputExtractionPlugin(),
            ExecutionPlugin(),
            ImageGenerationPlugin(),
            AttachmentToolsPlugin(),
            ToolHelpPlugin(),
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
    - web_search: Provider-fallback web search (auto by default)
    - fetch_url: Provider-fallback URL fetch/extraction (auto by default)
    - wait: Synchronize on child thread completion

    Returns:
        ToolRegistry with default tools registered.
    """
    return create_tool_registry()

