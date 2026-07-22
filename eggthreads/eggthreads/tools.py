from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping


_TOOL_SUMMARY_EMIT_INTERVAL_SEC = 5.0
_TOOL_DEADLINE_CLEANUP_GRACE_SEC = 0.25
_SYNC_TOOL_ADMISSION_WAIT_SEC = 1.0


def _sync_tool_thread_limit() -> int:
    configured = os.environ.get("EGG_MAX_LIVE_SYNC_TOOL_CALLS")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    # Enough headroom for ordinary parallel tool batches while still bounding
    # permanently detached native threads on small and large machines.
    return max(8, min(32, (os.cpu_count() or 1) * 2))


_MAX_LIVE_SYNC_TOOL_CALLS = _sync_tool_thread_limit()

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
    # Some wrapper tools execute work under a stricter thread policy than the
    # thread that ultimately receives the result.  Persist this bit through the
    # normal TC4/TC5 publication path so provider-bound sanitization cannot
    # accidentally use only the receiving thread's less restrictive policy.
    force_provider_output_masking: bool = False
    # A wrapper remains the protocol-visible tool name, but its result may carry
    # canonical content parts produced by another registered tool (for example,
    # generate_image executed in a descendant).  This optional name is used only
    # to decode those validated content parts for the local transcript.
    transcript_content_tool_name: str | None = None

    def presented_output(self) -> str:
        from .tool_output_presentation import apply_output_presentation

        return apply_output_presentation(self.output, self.publication_presentation)


@dataclass(frozen=True)
class ToolCapabilities:
    """Metadata describing optional tool execution capabilities."""

    supports_streaming: bool = False
    supports_cancellation: bool = False
    resumes_after_lease_loss: bool = False
    supports_cross_thread_execution: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: "ToolCapabilities | Mapping[str, Any] | None") -> "ToolCapabilities":
        if isinstance(value, ToolCapabilities):
            return value
        if not value:
            return cls()
        known = {
            "supports_streaming",
            "supports_cancellation",
            "resumes_after_lease_loss",
            "supports_cross_thread_execution",
        }
        return cls(
            supports_streaming=bool(value.get("supports_streaming", False)),
            supports_cancellation=bool(value.get("supports_cancellation", False)),
            resumes_after_lease_loss=bool(value.get("resumes_after_lease_loss", False)),
            supports_cross_thread_execution=bool(
                value.get("supports_cross_thread_execution", False)
            ),
            metadata={k: v for k, v in value.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.metadata)
        data["supports_streaming"] = self.supports_streaming
        data["supports_cancellation"] = self.supports_cancellation
        if self.resumes_after_lease_loss:
            data["resumes_after_lease_loss"] = True
        if self.supports_cross_thread_execution:
            data["supports_cross_thread_execution"] = True
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
    except BaseException:
        pass


class _SyncToolAdmission:
    """Bound native sync workers with condition-based admission waiting."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._condition = threading.Condition()
        self._live = 0
        self._detached = 0

    def try_acquire(self) -> bool:
        with self._condition:
            if self._live >= self.limit:
                return False
            self._live += 1
            return True

    def acquire(
        self,
        *,
        timeout: float | None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        wait_limit = timeout if timeout is not None else _SYNC_TOOL_ADMISSION_WAIT_SEC
        deadline = time.monotonic() + max(0.0, wait_limit)
        with self._condition:
            while self._live >= self.limit:
                if cancel_check is not None and cancel_check():
                    return "cancelled"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout" if timeout is not None else "overloaded"
                self._condition.wait(
                    min(remaining, 0.05 if cancel_check is not None else remaining)
                )
            self._live += 1
            return "admitted"

    def mark_detached(self) -> None:
        with self._condition:
            self._detached += 1

    def completed(self, *, was_detached: bool) -> None:
        with self._condition:
            self._live = max(0, self._live - 1)
            if was_detached:
                self._detached = max(0, self._detached - 1)
            self._condition.notify()

    def counts(self) -> tuple[int, int]:
        with self._condition:
            return self._live, self._detached


_SYNC_TOOL_ADMISSION = _SyncToolAdmission(_MAX_LIVE_SYNC_TOOL_CALLS)


class _SyncToolAdmissionDenied(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _DaemonThreadCall:
    """One admitted sync call in a context-propagating daemon thread.

    Admission bounds all live threads because every live call could become
    detached. Detached work retains its admission slot until it really exits,
    preventing unlimited native threads/contexts. Python still cannot kill it or
    prevent arbitrary late side effects.
    """

    def __init__(
        self,
        callback: Callable[[], Any],
        *,
        admission: _SyncToolAdmission,
    ):
        # The exact admission object travels with the call so test/config swaps
        # cannot debit one budget and credit another. Every construction step
        # after admission is inside the rollback guard.
        self._admission = admission
        try:
            self._done = threading.Event()
            self._lock = threading.Lock()
            self._callbacks: list[Callable[["_DaemonThreadCall"], None]] = []
            self._value: Any = None
            self._error: BaseException | None = None
            self._detached = False
            copied_context = contextvars.copy_context()
            self._thread = threading.Thread(
                target=self._run,
                args=(copied_context, callback),
                name="egg-tool-sync",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            self._admission.completed(was_detached=False)
            raise

    @property
    def daemon(self) -> bool:
        return self._thread.daemon

    def mark_detached(self) -> None:
        with self._lock:
            if self._done.is_set() or self._detached:
                return
            self._detached = True
            self._admission.mark_detached()

    def _run(
        self,
        copied_context: contextvars.Context,
        callback: Callable[[], Any],
    ) -> None:
        try:
            value = copied_context.run(callback)
            error = None
        except BaseException as exc:
            value = None
            error = exc
        with self._lock:
            self._value = value
            self._error = error
            callbacks = list(self._callbacks)
            self._callbacks.clear()
            self._done.set()
            was_detached = self._detached
        self._admission.completed(was_detached=was_detached)
        for done_callback in callbacks:
            try:
                done_callback(self)
            except BaseException:
                pass

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def result(self) -> Any:
        if not self._done.is_set():
            raise RuntimeError("Tool thread result is not ready")
        if self._error is not None:
            raise self._error
        return self._value

    def add_done_callback(
        self,
        callback: Callable[["_DaemonThreadCall"], None],
    ) -> None:
        with self._lock:
            if not self._done.is_set():
                self._callbacks.append(callback)
                return
        callback(self)

    def remove_done_callback(
        self,
        callback: Callable[["_DaemonThreadCall"], None],
    ) -> None:
        with self._lock:
            self._callbacks = [item for item in self._callbacks if item is not callback]


async def _acquire_sync_tool_admission(
    admission: _SyncToolAdmission,
    *,
    timeout: float | None,
    cancel_check: Callable[[], bool] | None,
) -> str:
    """Wait for a worker slot without blocking the asyncio event loop."""

    wait_limit = timeout if timeout is not None else _SYNC_TOOL_ADMISSION_WAIT_SEC
    deadline = time.monotonic() + max(0.0, wait_limit)
    while not admission.try_acquire():
        if cancel_check is not None and cancel_check():
            return "cancelled"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout" if timeout is not None else "overloaded"
        await asyncio.sleep(min(remaining, 0.01))
    return "admitted"


async def _run_sync_in_daemon(
    callback: Callable[[], Any],
    *,
    admission_timeout: float | None,
    cancel_check: Callable[[], bool] | None,
) -> Any:
    """Await one admitted daemon thread without using asyncio's executor."""

    admission = _SYNC_TOOL_ADMISSION
    admission_result = await _acquire_sync_tool_admission(
        admission,
        timeout=admission_timeout,
        cancel_check=cancel_check,
    )
    if admission_result != "admitted":
        raise _SyncToolAdmissionDenied(admission_result)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    call = _DaemonThreadCall(callback, admission=admission)

    def completed(completed_call: _DaemonThreadCall) -> None:
        try:
            value = completed_call.result()
            error = None
        except BaseException as exc:
            value = None
            error = exc

        def publish(value: Any = value, error: BaseException | None = error) -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        try:
            loop.call_soon_threadsafe(publish)
        except RuntimeError:
            pass

    call.add_done_callback(completed)
    try:
        return await future
    except asyncio.CancelledError:
        call.mark_detached()
        raise
    finally:
        call.remove_done_callback(completed)


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
    # This cancels only the await. A hostile daemon thread can keep executing
    # arbitrary Python and producing side effects; Python provides no safe kill.
    await asyncio.sleep(0)
    return None


def _tool_timeout_result(
    timeout_sec: float,
    cleanup_result: Any = None,
) -> ToolExecutionResult:
    """Build one unambiguous authority timeout result."""

    limit = f"{float(timeout_sec):g}"
    header = f"--- TIMEOUT ---\nTool execution timed out after {limit} seconds."
    if isinstance(cleanup_result, ToolExecutionResult):
        if cleanup_result.reason == "timeout" and "--- INTERRUPTED ---" not in cleanup_result.output:
            return cleanup_result
        if cleanup_result.reason != "interrupted" and "--- INTERRUPTED ---" not in cleanup_result.output:
            return replace(
                cleanup_result,
                output=f"{header}\n\n{cleanup_result.output}" if cleanup_result.output else header,
                reason="timeout",
            )
        # Interrupted cleanup text describes the composed cancellation signal,
        # not the authority outcome. Retain only any useful payload after its
        # interruption preamble so reason/output cannot contradict each other.
        useful_output = ""
        if "\n\n" in cleanup_result.output:
            useful_output = cleanup_result.output.split("\n\n", 1)[1].strip()
        return ToolExecutionResult(
            f"{header}\n\n{useful_output}" if useful_output else header,
            reason="timeout",
            streamed=cleanup_result.streamed,
            publication_presentation=cleanup_result.publication_presentation,
            force_provider_output_masking=cleanup_result.force_provider_output_masking,
            transcript_content_tool_name=cleanup_result.transcript_content_tool_name,
        )
    return ToolExecutionResult(header, reason="timeout")


def _tool_overloaded_result() -> ToolExecutionResult:
    return ToolExecutionResult(
        "--- OVERLOADED ---\nToo many synchronous tool calls are still running; try again after they finish.",
        reason="overloaded",
    )


def _memory_threads_db_error(tool_ctx: ToolContext | None) -> ToolExecutionResult | None:
    """Reject timed worker execution that would cross an in-memory DB owner."""

    if tool_ctx is None or tool_ctx.db is None:
        return None
    try:
        from .db import ThreadsDB
    except Exception:
        return None
    if type(tool_ctx.db) is ThreadsDB and str(getattr(tool_ctx.db, "path", "")) == ":memory:":
        return ToolExecutionResult(
            "--- UNSUPPORTED ---\nTimed context-aware tool execution cannot move an in-memory ThreadsDB connection across threads.",
            reason="unsupported",
        )
    return None


def _clone_tool_context_db_for_worker(
    tool_ctx: ToolContext | None,
) -> tuple[ToolContext | None, Callable[[], None]]:
    """Clone a file-backed ThreadsDB for one worker and return its cleanup.

    Only Egg's concrete ThreadsDB is cloned. Custom DB/context objects and
    in-memory SQLite databases preserve their existing semantics/identity.
    """

    if tool_ctx is None or tool_ctx.db is None:
        return tool_ctx, lambda: None
    try:
        from .db import ThreadsDB
    except Exception:
        return tool_ctx, lambda: None
    if type(tool_ctx.db) is not ThreadsDB:
        return tool_ctx, lambda: None
    db_path = getattr(tool_ctx.db, "path", None)
    if db_path is None or str(db_path) == ":memory:":
        return tool_ctx, lambda: None

    worker_db = ThreadsDB(db_path)
    worker_ctx = replace(tool_ctx, db=worker_db)
    raw_context = dict(worker_ctx.raw)
    if raw_context.get("db") is tool_ctx.db:
        raw_context["db"] = worker_db
    worker_ctx = replace(worker_ctx, raw=raw_context)

    def close() -> None:
        try:
            worker_db.conn.close()
        except Exception:
            pass

    return worker_ctx, close


class ToolRegistry:
    """Simple registry for OpenAI function-call compatible tools.

    - tools_spec() returns the JSON schema list to pass to the LLM
    - execute(name, arguments) dispatches to the registered callable
    - execute_async(name, arguments) dispatches and awaits async callables
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def resolve_name(self, name: str) -> str:
        """Resolve a historical default-tool name without shadowing exact tools."""

        if name in self._tools:
            return name
        from .tools_config import canonical_tool_name

        canonical = canonical_tool_name(name)
        return canonical if canonical in self._tools else name

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
        entry = self._tools.get(self.resolve_name(name))
        if not entry:
            raise KeyError(f"Unknown tool: {name}")
        return entry["capabilities"]

    def is_registered(self, name: str) -> bool:
        """Return whether *name* resolves to a registered tool."""

        return self.resolve_name(name) in self._tools

    def is_local_only(self, name: str) -> bool:
        """Return registry visibility metadata for a registered tool."""

        entry = self._tools.get(self.resolve_name(name))
        if entry is None:
            raise KeyError(f"Unknown tool: {name}")
        return bool(entry.get("local_only"))

    async def execute_in_thread_context(
        self,
        name: str,
        arguments: Any,
        *,
        thread_id: str,
        **context: Any,
    ) -> Any:
        """Execute using an explicit authoritative thread identity.

        Authorization belongs to the higher-level caller (for example the
        descendant-only wrapper).  This small registry primitive centralizes
        identity precedence so neither context-aware nor legacy argument-aware
        tools can retain a caller-supplied ``_thread_id``.
        """

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id is required")
        if isinstance(arguments, dict):
            arguments = dict(arguments)
            arguments.pop("_thread_id", None)
        return await self.execute_async(
            name,
            arguments,
            **{**context, "thread_id": thread_id.strip()},
        )

    def _prepare_call(
        self,
        name: str,
        arguments: Any,
        context: Mapping[str, Any],
    ) -> tuple[Callable[..., Any], Dict[str, Any], ToolContext | None]:
        entry = self._tools.get(self.resolve_name(name))
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

    @staticmethod
    def _implementation_call(
        impl: Callable[..., Any],
        args: Dict[str, Any],
        tool_ctx: ToolContext | None,
    ) -> Any:
        if tool_ctx is not None:
            return impl(args, tool_ctx)
        return impl(args)

    @classmethod
    def _implementation_call_in_worker(
        cls,
        impl: Callable[..., Any],
        args: Dict[str, Any],
        tool_ctx: ToolContext | None,
    ) -> Any:
        worker_ctx, close_worker_db = _clone_tool_context_db_for_worker(tool_ctx)
        try:
            return cls._implementation_call(impl, args, worker_ctx)
        finally:
            close_worker_db()

    @staticmethod
    def _compose_call_cancellation(
        args: Dict[str, Any],
        tool_ctx: ToolContext | None,
    ) -> tuple[ToolContext | None, _ToolCancellationController]:
        upstream_cancel_check = (
            tool_ctx.cancel_check
            if tool_ctx is not None
            else args.get("_cancel_check")
        )
        controller = _ToolCancellationController(upstream_cancel_check)
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
        return tool_ctx, controller

    @staticmethod
    def _call_timeout_sec(
        args: Dict[str, Any],
        tool_ctx: ToolContext | None,
    ) -> float | None:
        if tool_ctx is not None:
            return tool_ctx.timeout_sec
        return resolve_tool_timeout_arg(args)

    @staticmethod
    def _present_result(result: Any, context: Mapping[str, Any]) -> Any:
        if isinstance(result, ToolExecutionResult) and not context.get("preserve_tool_result"):
            return result.presented_output()
        return result

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

    @staticmethod
    async def _await_with_authority(
        awaitable: Any,
        controller: _ToolCancellationController,
        timeout_sec: float | None,
    ) -> Any:
        implementation_task = asyncio.create_task(awaitable)
        deadline_wait: asyncio.Task[Any] | None = None
        try:
            if timeout_sec is None:
                return await asyncio.shield(implementation_task)

            deadline_wait = asyncio.create_task(_wait_for_tool_deadline(timeout_sec))
            done, _pending = await asyncio.wait(
                {implementation_task, deadline_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if implementation_task in done:
                # A completed result observable when the deadline wake-up is
                # handled wins the race.
                return implementation_task.result()

            controller.cancel()
            cleanup_result = await _finish_or_detach_tool_task(implementation_task)
            return _tool_timeout_result(timeout_sec, cleanup_result)
        except asyncio.CancelledError:
            controller.cancel()
            cleanup_task = asyncio.create_task(
                _finish_or_detach_tool_task(implementation_task)
            )
            cleanup_task.add_done_callback(_consume_tool_task_result)
            raise
        finally:
            if deadline_wait is not None:
                deadline_wait.cancel()
                deadline_wait.add_done_callback(_consume_tool_task_result)

    async def _execute_prepared_async(
        self,
        impl: Callable[..., Any],
        args: Dict[str, Any],
        tool_ctx: ToolContext | None,
        controller: _ToolCancellationController,
        timeout_sec: float | None,
    ) -> Any:
        async def invoke() -> Any:
            if inspect.iscoroutinefunction(impl):
                result = self._implementation_call(impl, args, tool_ctx)
            else:
                result = await _run_sync_in_daemon(
                    lambda: self._implementation_call_in_worker(impl, args, tool_ctx),
                    admission_timeout=timeout_sec,
                    cancel_check=controller.cancelled,
                )
            if inspect.isawaitable(result):
                result = await result
            return result

        return await self._await_with_authority(invoke(), controller, timeout_sec)

    def execute(self, name: str, arguments: Any, **context: Any) -> Any:
        """Execute a tool synchronously under the authoritative contract."""

        impl, args, tool_ctx = self._prepare_call(name, arguments, context)
        tool_ctx, controller = self._compose_call_cancellation(args, tool_ctx)
        timeout_sec = self._call_timeout_sec(args, tool_ctx)

        # Preserve the long-standing active-loop error before starting an async
        # implementation or creating its coroutine.
        if inspect.iscoroutinefunction(impl):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError(
                    "Cannot synchronously execute an async tool while an event loop is running; use execute_async()."
                )
            result = asyncio.run(
                self._execute_prepared_async(
                    impl,
                    args,
                    tool_ctx,
                    controller,
                    timeout_sec,
                )
            )
            return self._present_result(result, context)

        memory_db_error = _memory_threads_db_error(tool_ctx)
        if timeout_sec is not None and memory_db_error is not None:
            return self._present_result(memory_db_error, context)

        # Preserve caller-thread affinity when no deadline is active. This is
        # important for direct context-aware tools holding SQLite connections.
        if timeout_sec is None:
            result = self._implementation_call(impl, args, tool_ctx)
            if inspect.isawaitable(result):
                result = self._run_awaitable_sync(result)
            return self._present_result(result, context)

        deadline = time.monotonic() + timeout_sec
        admission = _SYNC_TOOL_ADMISSION
        admission_result = admission.acquire(
            timeout=max(0.0, deadline - time.monotonic()),
            cancel_check=controller.cancelled,
        )
        if admission_result == "timeout":
            controller.cancel()
            return self._present_result(_tool_timeout_result(timeout_sec), context)
        if admission_result == "cancelled":
            return self._present_result(
                ToolExecutionResult(
                    "--- INTERRUPTED ---\nTool execution was cancelled while waiting for capacity.",
                    reason="interrupted",
                ),
                context,
            )
        if admission_result != "admitted":
            return self._present_result(_tool_overloaded_result(), context)
        try:
            call = _DaemonThreadCall(
                lambda: self._implementation_call_in_worker(impl, args, tool_ctx),
                admission=admission,
            )
        except BaseException:
            raise
        remaining = max(0.0, deadline - time.monotonic())
        if not call.wait(remaining):
            controller.cancel()
            if call.wait(_TOOL_DEADLINE_CLEANUP_GRACE_SEC):
                try:
                    cleanup_result = call.result()
                except BaseException:
                    cleanup_result = None
            else:
                call.mark_detached()
                cleanup_result = None
            return self._present_result(
                _tool_timeout_result(timeout_sec, cleanup_result),
                context,
            )

        result = call.result()
        if inspect.isawaitable(result):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise RuntimeError(
                    "Cannot synchronously execute an async tool while an event loop is running; use execute_async()."
                )
            result = asyncio.run(
                self._await_with_authority(
                    self._await_result(result),
                    controller,
                    remaining,
                )
            )
        return self._present_result(result, context)

    async def execute_async(self, name: str, arguments: Any, **context: Any) -> Any:
        """Execute a tool asynchronously under the authoritative deadline.

        Async implementations are cancelled after a bounded cooperative cleanup
        window. Sync implementations use dedicated context-propagating daemon
        threads, never asyncio's shared default executor. A timeout can detach a
        hostile thread so it cannot block loop shutdown or starve later tools,
        but Python cannot forcibly stop that thread or prevent arbitrary late
        side effects.
        """

        impl, args, tool_ctx = self._prepare_call(name, arguments, context)
        tool_ctx, controller = self._compose_call_cancellation(args, tool_ctx)
        timeout_sec = self._call_timeout_sec(args, tool_ctx)
        memory_db_error = (
            _memory_threads_db_error(tool_ctx)
            if timeout_sec is not None and not inspect.iscoroutinefunction(impl)
            else None
        )
        if memory_db_error is not None:
            return self._present_result(memory_db_error, context)
        try:
            result = await self._execute_prepared_async(
                impl,
                args,
                tool_ctx,
                controller,
                timeout_sec,
            )
        except _SyncToolAdmissionDenied as exc:
            if exc.reason == "timeout" and timeout_sec is not None:
                controller.cancel()
                result = _tool_timeout_result(timeout_sec)
            elif exc.reason == "cancelled":
                result = ToolExecutionResult(
                    "--- INTERRUPTED ---\nTool execution was cancelled while waiting for capacity.",
                    reason="interrupted",
                )
            else:
                result = _tool_overloaded_result()
        return self._present_result(result, context)


def create_tool_registry() -> ToolRegistry:
    """Create a plugin-populated ToolRegistry with Egg's built-in tools."""

    from .builtin_plugins import (
        AnswerUserPlugin,
        AttachmentToolsPlugin,
        CompactionPlugin,
        CrossThreadExecutionPlugin,
        ExecutionPlugin,
        ImageGenerationPlugin,
        LongOutputPlugin,
        SessionPlugin,
        SkillsPlugin,
        SubagentsPlugin,
        ToolHelpPlugin,
        ToolOutputExtractionPlugin,
        WebPlugin,
    )
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
            CrossThreadExecutionPlugin(),
            WebPlugin(),
        ],
    )
    return reg


# Default tools similar to chat.sh
def create_default_tools() -> ToolRegistry:
    """Create a ToolRegistry with the default set of tools.

    Returns a registry pre-populated with common tools:
    - bash: Execute shell commands
    - python_exec: Execute Python code in the current working directory
    - spawn_agent: Create child threads for delegation
    - spawn_agent_auto: Create auto-approved child threads
    - execute_tool_in_other_thread: Run an opted-in tool in a descendant context
    - web_search: Provider-fallback web search (auto by default)
    - fetch_url: Provider-fallback URL fetch/extraction (auto by default)
    - wait: Synchronize on child thread completion

    Returns:
        ToolRegistry with default tools registered.
    """
    return create_tool_registry()

