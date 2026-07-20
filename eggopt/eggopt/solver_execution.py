"""Persistent Solver/Execution composition over Eggflow and Eggthreads."""

from __future__ import annotations

import hashlib
import inspect
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from eggflow import Task, TaskError
from eggflow.eggthreads_tasks import ContextLimitExceededError
from eggthreads import (
    ThreadRunner,
    ThreadsDB,
    append_message,
    build_tool_call_states,
    create_child_thread,
    create_default_tools,
    enqueue_user_tool_call,
    get_user_command_result,
    set_thread_model,
    set_thread_sandbox_config,
    set_thread_tool_allowlist,
    set_thread_tools_enabled,
    set_thread_working_directory,
)

from .core import Producer
from .repair import Accepted, ItemFailure, NeedsRepair, RepairFeedback

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

_PAIR_SCHEMA = b"eggopt.SolverExecutionThreads:v2\0"
_FEEDBACK_SCHEMA = b"eggopt.AppendSolverFeedback:v1\0"
_SOLVE_SCHEMA = b"eggopt.SolverAttempt:v2\0"
_EXECUTE_SCHEMA = b"eggopt.ExecutionAttempt:v2\0"
_COMPOSITION_SCHEMA = b"eggopt.SolverExecution:v2\0"

_DEFAULT_SANDBOX: tuple[tuple[str, object], ...] = (
    ("provider", "docker"),
    ("image", "egg-sandbox"),
    ("network", "none"),
    ("workspace", "/workspace"),
    ("extra_mounts", ()),
    ("extra_args", ("--cap-drop", "ALL")),
    ("user_control_enabled", False),
)

__all__ = [
    "Execution",
    "ExecutionInput",
    "ExecutionResult",
    "ExecutionSpec",
    "SolverExecution",
    "SolverExecutionRequest",
    "SolverInput",
    "SolverSpec",
    "ToolCall",
]


@dataclass(frozen=True)
class SolverSpec:
    """Pickle-safe configuration for one persistent Solver."""

    working_directory: str
    name: str = "Solver"
    system_prompt: str = ""
    model_key: str | None = None
    sandbox: tuple[tuple[str, object], ...] = _DEFAULT_SANDBOX
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _thread_spec(self, allow_arbitrary_tools=True)
        if not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string")
        if self.model_key is not None:
            _nonempty(self.model_key, "model_key")


@dataclass(frozen=True)
class ExecutionSpec:
    """Pickle-safe configuration for privileged Execution."""

    working_directory: str
    name: str = "Execution"
    sandbox: tuple[tuple[str, object], ...] = _DEFAULT_SANDBOX
    tools: tuple[str, ...] = ("python", "bash")

    def __post_init__(self) -> None:
        _thread_spec(self, allow_arbitrary_tools=False)


@dataclass(frozen=True)
class ToolCall:
    """One real Execution tool invocation."""

    tool: str
    script: str
    key: str
    cache_by: object
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.tool not in {"python", "bash"}:
            raise ValueError("tool must be 'python' or 'bash'")
        if not isinstance(self.script, str):
            raise TypeError("script must be a string")
        _nonempty(self.key, "key")
        _digest(self.cache_by, "cache_by")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a number")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True)
class SolverExecutionRequest(Generic[InputT]):
    """One caller-identified item to solve and execute."""

    item_id: str
    value: InputT

    def __post_init__(self) -> None:
        _nonempty(self.item_id, "item_id")


@dataclass(frozen=True)
class SolverInput(Generic[InputT]):
    """Input to a solver drive continuing one persistent conversation."""

    thread_id: str
    original: InputT
    feedback: tuple[RepairFeedback, ...] = ()
    attempt: int = 0
    trigger_message_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.thread_id, "thread_id")
        feedback = tuple(self.feedback)
        if not all(isinstance(item, RepairFeedback) for item in feedback):
            raise TypeError("feedback must contain only RepairFeedback values")
        _nonnegative(self.attempt, "attempt")
        if self.trigger_message_id is not None:
            _nonempty(self.trigger_message_id, "trigger_message_id")
        object.__setattr__(self, "feedback", feedback)


@dataclass(frozen=True)
class ExecutionInput(Generic[OutputT]):
    """Candidate plus persistent, attempt-bound Execution."""

    candidate: OutputT
    attempt: int
    execution: "Execution"

    def __post_init__(self) -> None:
        _nonnegative(self.attempt, "attempt")
        if not isinstance(self.execution, Execution):
            raise TypeError("execution must be Execution")

    @property
    def thread_id(self) -> str:
        return self.execution.thread_id


@dataclass(frozen=True)
class ExecutionResult:
    """Cached output from one real tool invocation."""

    thread_id: str
    tool_call_id: str
    output: str

    def __post_init__(self) -> None:
        _nonempty(self.thread_id, "thread_id")
        _nonempty(self.tool_call_id, "tool_call_id")
        if not isinstance(self.output, str):
            raise TypeError("output must be a string")


@dataclass(frozen=True)
class _Threads:
    solver_thread_id: str
    execution_thread_id: str


@dataclass
class _CreateThreads(Task):
    threads_db_path: str
    parent_thread_id: str
    item_id: str
    solver: SolverSpec
    execution: ExecutionSpec

    def get_cache_key(self) -> str:
        return _cache_key(
            _PAIR_SCHEMA,
            (self.threads_db_path, self.parent_thread_id, self.item_id, self.solver, self.execution),
        )

    def run(self) -> _Threads:
        db = _db(self.threads_db_path)
        try:
            solver_id = create_child_thread(db, self.parent_thread_id, name=self.solver.name)
            _configure_thread(db, solver_id, self.solver)
            if self.solver.system_prompt:
                append_message(db, solver_id, "system", self.solver.system_prompt)
            if self.solver.model_key is not None:
                set_thread_model(db, solver_id, self.solver.model_key)

            execution_id = create_child_thread(db, self.parent_thread_id, name=self.execution.name)
            _configure_thread(db, execution_id, self.execution)
            return _Threads(solver_id, execution_id)
        finally:
            db.conn.close()


@dataclass
class _AppendFeedback(Task):
    threads_db_path: str
    solver_thread_id: str
    item_digest: bytes
    attempt: int
    feedback: RepairFeedback

    def get_cache_key(self) -> str:
        return _cache_key(
            _FEEDBACK_SCHEMA,
            (self.threads_db_path, self.solver_thread_id, self.item_digest, self.attempt, self.feedback),
        )

    def run(self) -> str:
        db = _db(self.threads_db_path)
        try:
            return append_message(
                db,
                self.solver_thread_id,
                "user",
                self.feedback.text,
                extra={"eggopt_repair_feedback": True},
            )
        finally:
            db.conn.close()


@dataclass
class _SolverAttempt(Task, Generic[InputT, OutputT]):
    threads_db_path: str
    solver_thread_id: str
    solver: Producer[SolverInput[InputT], OutputT | Task]
    solver_identity: str
    original: InputT
    feedback: tuple[RepairFeedback, ...]
    attempt: int
    trigger_message_id: str | None

    def get_cache_key(self) -> str:
        return _cache_key(
            _SOLVE_SCHEMA,
            (
                self.threads_db_path,
                self.solver_thread_id,
                self.solver_identity,
                self.attempt,
                _digest(self.original, "request value"),
                self.feedback,
                self.trigger_message_id,
            ),
        )

    def run(self):
        result = self.solver.produce(
            SolverInput(
                self.solver_thread_id,
                self.original,
                self.feedback,
                self.attempt,
                self.trigger_message_id,
            )
        )
        if isinstance(result, Task) or inspect.iscoroutine(result):
            result = yield result
        return result


@dataclass
class _ExecutionAttempt(Task):
    threads_db_path: str
    execution_thread_id: str
    item_digest: bytes
    attempt: int
    call: ToolCall

    def get_cache_key(self) -> str:
        return _cache_key(
            _EXECUTE_SCHEMA,
            (
                self.threads_db_path,
                self.execution_thread_id,
                self.item_digest,
                self.attempt,
                self.call.key,
                self.call.tool,
                _digest(self.call.script, "tool script"),
                _digest(self.call.cache_by, "cache_by"),
                self.call.timeout_seconds,
            ),
        )

    async def run(self) -> ExecutionResult:
        tool_call_id = hashlib.sha256(
            pickle.dumps(
                (
                    self.execution_thread_id,
                    self.item_digest,
                    self.attempt,
                    self.call.key,
                    self.call.tool,
                    self.call.script,
                    _digest(self.call.cache_by, "cache_by"),
                ),
                protocol=5,
            )
        ).hexdigest()
        db = _db(self.threads_db_path)
        try:
            existing = build_tool_call_states(db, self.execution_thread_id).get(tool_call_id)
            if existing is None:
                enqueue_user_tool_call(
                    db,
                    self.execution_thread_id,
                    self.call.tool,
                    {"script": self.call.script, "timeout": self.call.timeout_seconds},
                    content=f"{self.call.tool}: {self.call.script}",
                    hidden=True,
                    tool_call_id=tool_call_id,
                )
            else:
                output = get_user_command_result(db, self.execution_thread_id, tool_call_id)
                if output is not None:
                    return ExecutionResult(self.execution_thread_id, tool_call_id, output)

            runner = ThreadRunner(
                db, self.execution_thread_id, llm=object(), tools=create_default_tools()
            )
            for _ in range(4):
                progressed = await runner.run_once()
                output = get_user_command_result(db, self.execution_thread_id, tool_call_id)
                if output is not None:
                    return ExecutionResult(self.execution_thread_id, tool_call_id, output)
                if not progressed:
                    break
            raise RuntimeError(f"Execution tool call {tool_call_id} did not publish an output")
        finally:
            db.conn.close()


@dataclass(frozen=True)
class Execution:
    """Attempt-bound real tool Producer in one persistent Execution thread."""

    threads_db_path: str
    thread_id: str
    item_digest: bytes
    attempt: int

    def produce(self, call: ToolCall) -> _ExecutionAttempt:
        if not isinstance(call, ToolCall):
            raise TypeError("Execution accepts ToolCall values")
        return _ExecutionAttempt(
            self.threads_db_path, self.thread_id, self.item_digest, self.attempt, call
        )

    def python(
        self,
        script: str,
        *,
        key: str,
        cache_by: object,
        timeout_seconds: float = 30.0,
    ) -> _ExecutionAttempt:
        return self.produce(ToolCall("python", script, key, cache_by, timeout_seconds))

    def bash(
        self,
        script: str,
        *,
        key: str,
        cache_by: object,
        timeout_seconds: float = 30.0,
    ) -> _ExecutionAttempt:
        return self.produce(ToolCall("bash", script, key, cache_by, timeout_seconds))


@dataclass
class _SolverExecutionTask(Task, Generic[InputT, OutputT]):
    composition: "SolverExecution[InputT, OutputT]"
    request: SolverExecutionRequest[InputT]

    def get_cache_key(self) -> str:
        c = self.composition
        return _cache_key(
            _COMPOSITION_SCHEMA,
            (
                c.threads_db_path,
                c.parent_thread_id,
                c.solver_spec,
                c.execution_spec,
                c.solver_identity,
                c.execution_identity,
                c.max_repairs,
                self.request.item_id,
                _digest(self.request.value, "request value"),
            ),
        )

    def run(self):
        c = self.composition
        pair = yield _CreateThreads(
            c.threads_db_path,
            c.parent_thread_id,
            self.request.item_id,
            c.solver_spec,
            c.execution_spec,
        )
        item_digest = _digest(self.request.value, "request value")
        feedback: tuple[RepairFeedback, ...] = ()
        trigger_message_id: str | None = None
        for attempt in range(c.max_repairs + 1):
            try:
                if feedback:
                    trigger_message_id = yield _AppendFeedback(
                        c.threads_db_path,
                        pair.solver_thread_id,
                        item_digest,
                        attempt,
                        feedback[-1],
                    )
                candidate = yield _SolverAttempt(
                    c.threads_db_path,
                    pair.solver_thread_id,
                    c.solver,
                    c.solver_identity,
                    self.request.value,
                    feedback,
                    attempt,
                    trigger_message_id,
                )
                execution = Execution(
                    c.threads_db_path, pair.execution_thread_id, item_digest, attempt
                )
                inspection = c.execution.produce(
                    ExecutionInput(candidate, attempt, execution)
                )
                if isinstance(inspection, Task) or inspect.iscoroutine(inspection):
                    inspection = yield inspection
            except TaskError as error:
                if error.is_terminal:
                    return ItemFailure("terminal", str(error), attempt + 1)
                raise
            except ContextLimitExceededError as error:
                return ItemFailure("terminal", str(error), attempt + 1)

            if isinstance(inspection, Accepted):
                return inspection.value
            if not isinstance(inspection, NeedsRepair):
                raise TypeError("execution must produce Accepted or NeedsRepair")
            if attempt == c.max_repairs:
                return ItemFailure("repair_exhausted", inspection.feedback.text, attempt + 1)
            feedback += (inspection.feedback,)
        raise AssertionError("repair loop exhausted unexpectedly")


@dataclass(frozen=True, kw_only=True)
class SolverExecution(Generic[InputT, OutputT]):
    """Compose one persistent Solver and one persistent Execution per item."""

    threads_db_path: str
    parent_thread_id: str
    solver: Producer[SolverInput[InputT], OutputT | Task]
    execution: Producer[
        ExecutionInput[OutputT], Accepted[OutputT] | NeedsRepair | Task
    ]
    solver_identity: str
    execution_identity: str
    solver_spec: SolverSpec
    execution_spec: ExecutionSpec
    max_repairs: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.threads_db_path, "threads_db_path")
        _nonempty(self.parent_thread_id, "parent_thread_id")
        if not isinstance(self.solver, Producer):
            raise TypeError("solver must implement Producer")
        if not isinstance(self.execution, Producer):
            raise TypeError("execution must implement Producer")
        _nonempty(self.solver_identity, "solver_identity")
        _nonempty(self.execution_identity, "execution_identity")
        if not isinstance(self.solver_spec, SolverSpec):
            raise TypeError("solver_spec must be SolverSpec")
        if not isinstance(self.execution_spec, ExecutionSpec):
            raise TypeError("execution_spec must be ExecutionSpec")
        _nonnegative(self.max_repairs, "max_repairs")

    def produce(
        self, request: SolverExecutionRequest[InputT]
    ) -> _SolverExecutionTask[InputT, OutputT]:
        if not isinstance(request, SolverExecutionRequest):
            raise TypeError("request must be SolverExecutionRequest")
        return _SolverExecutionTask(self, request)


def _configure_thread(db: ThreadsDB, thread_id: str, spec: object) -> None:
    working_directory = Path(spec.working_directory).resolve()  # type: ignore[attr-defined]
    working_directory.mkdir(parents=True, exist_ok=True)
    set_thread_working_directory(db, thread_id, str(working_directory))
    set_thread_tools_enabled(db, thread_id, bool(spec.tools))  # type: ignore[attr-defined]
    set_thread_tool_allowlist(db, thread_id, list(spec.tools))  # type: ignore[attr-defined]
    settings = _thaw(dict(spec.sandbox))  # type: ignore[attr-defined]
    if not isinstance(settings, dict):
        raise TypeError("sandbox must decode to a mapping")
    set_thread_sandbox_config(db, thread_id, enabled=True, settings=settings)


def _thread_spec(spec: object, *, allow_arbitrary_tools: bool) -> None:
    _nonempty(spec.working_directory, "working_directory")  # type: ignore[attr-defined]
    _nonempty(spec.name, "name")  # type: ignore[attr-defined]
    sandbox = _frozen_pairs(spec.sandbox, "sandbox")  # type: ignore[attr-defined]
    tools = tuple(spec.tools)  # type: ignore[attr-defined]
    if any(not isinstance(tool, str) or not tool for tool in tools):
        raise ValueError("tools must contain nonempty strings")
    if len(set(tools)) != len(tools):
        raise ValueError("tools must not contain duplicates")
    if not allow_arbitrary_tools and not set(tools).issubset({"python", "bash"}):
        raise ValueError("Execution supports only python and bash")
    object.__setattr__(spec, "sandbox", sandbox)
    object.__setattr__(spec, "tools", tools)


def _db(path: str) -> ThreadsDB:
    db = ThreadsDB(path)
    db.init_schema()
    return db


def _digest(value: object, name: str) -> bytes:
    try:
        return hashlib.sha256(pickle.dumps(value, protocol=5)).digest()
    except Exception as exc:
        raise TypeError(f"{name} must be pickleable for cache identity") from exc


def _cache_key(schema: bytes, values: tuple[object, ...]) -> str:
    try:
        serialized = pickle.dumps(values, protocol=5)
    except Exception as exc:
        raise TypeError("task cache-key values must be pickleable") from exc
    return hashlib.sha256(schema + serialized).hexdigest()


def _nonempty(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _nonnegative(value: object, name: str) -> None:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _frozen_pairs(value: object, name: str) -> tuple[tuple[str, object], ...]:
    try:
        pairs = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{name} must be key/value pairs") from exc
    result = []
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError(f"{name} must contain key/value pairs")
        key, item = pair
        _nonempty(key, f"{name} key")
        result.append((key, _freeze(item)))
    if len({key for key, _ in result}) != len(result):
        raise ValueError(f"{name} keys must be unique")
    return tuple(result)


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return tuple((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    try:
        pickle.dumps(value, protocol=5)
    except Exception as exc:
        raise TypeError("sandbox values must be pickleable") from exc
    return value


def _thaw(value: object) -> object:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            return {key: _thaw(item) for key, item in value}
        return [_thaw(item) for item in value]
    return value
