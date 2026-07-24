from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, Task, TaskStore
from eggthreads import (
    RunnerConfig,
    ThreadsDB,
    ToolRegistry,
    create_root_thread,
    get_context_limit,
    list_root_threads,
    set_context_limit,
)

from .evaluation import Evaluation
from ._identity import digest_payload
from .gepa.production_drive import (
    DEFAULT_MUTATION_SYSTEM_PROMPT,
    EggthreadsReflectionDrive,
    create_solver_safe_study,
    default_solver_safe_tools,
)
from .gepa.reflection import EggthreadsReflectionLM, ReflectionDrive

ExampleT = TypeVar("ExampleT")
OutputT = TypeVar("OutputT")
Metric = Callable[[Mapping[str, str], ExampleT], Evaluation[OutputT] | float]


@dataclass(frozen=True)
class Reflection:
    """How mutation should think; persistence and recovery remain Eggopt's job."""

    drive: ReflectionDrive
    identity: Mapping[str, Any]
    instruction: str = "Reflect on the evidence and improve the requested components."
    workspace: str | Path | None = None
    model_key: str | None = None
    models_path: str = "models.json"
    study_name: str = "GEPA Study"

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        """Explicit GEPA capability set, or ``None`` for non-production drives."""

        return getattr(self.drive, "allowed_tools", None)

    @classmethod
    def eggthreads(
        cls,
        *,
        llm: Any,
        tools: ToolRegistry | None = None,
        allowed_tools: set[str] | frozenset[str] | None = None,
        identity: Mapping[str, Any],
        instruction: str = "Reflect on the evidence and improve the requested components.",
        workspace: str | Path | None = None,
        model_key: str | None = None,
        models_path: str = "models.json",
        runner_config: RunnerConfig | None = None,
        auto_approve_tools: bool = False,
        max_runner_steps: int | float = math.inf,
        max_correction_turns: int = 0,
        context_limit: int | None = None,
        system_prompt: str = DEFAULT_MUTATION_SYSTEM_PROMPT,
    ) -> Reflection:
        if tools is None:
            tools = default_solver_safe_tools()
        return cls(
            drive=EggthreadsReflectionDrive(
                llm=llm,
                tools=tools,
                allowed_tools=allowed_tools,
                drive_identity=identity,
                runner_config=runner_config,
                models_path=models_path,
                auto_approve_tools=auto_approve_tools,
                max_runner_steps=max_runner_steps,
                max_correction_turns=max_correction_turns,
                context_limit=context_limit,
                system_prompt=system_prompt,
            ),
            identity=identity,
            instruction=instruction,
            workspace=workspace,
            model_key=model_key,
            models_path=models_path,
        )


@dataclass
class Runtime(Generic[ExampleT, OutputT]):
    root: Path
    store: TaskStore
    flow: FlowExecutor
    threads: ThreadsDB
    study_id: str
    reflection: EggthreadsReflectionLM

    @classmethod
    def open(
        cls,
        root: str | Path,
        reflection: Reflection,
        *,
        study_name: str | None = None,
        default_workspace: str | Path | None = None,
    ) -> Runtime[Any, Any]:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        egg = root / ".egg"
        egg.mkdir(exist_ok=True)
        store = TaskStore(str(root / "flow.db"))
        flow = FlowExecutor(store)
        threads = ThreadsDB(egg / "threads.sqlite")
        threads.init_schema()
        legacy_id = _legacy_study_id(threads)
        roots = list_root_threads(threads)
        if legacy_id is None and len(roots) > 1:
            raise RuntimeError("Eggopt run directory contains multiple root threads")
        study_id = _sync(
            flow.run(
                _CreateStudy(
                    threads,
                    reflection,
                    Path(
                        reflection.workspace
                        or default_workspace
                        or root / "workspaces" / "mutation"
                    ),
                    study_name or reflection.study_name,
                    legacy_id or (roots[0] if roots else None),
                )
            )
        )
        ensure_system_prompt = getattr(reflection.drive, "ensure_system_prompt", None)
        if callable(ensure_system_prompt):
            ensure_system_prompt(threads, study_id)
        context_limit = getattr(reflection.drive, "context_limit", None)
        if (
            context_limit is not None
            and get_context_limit(threads, study_id) != context_limit
        ):
            set_context_limit(
                threads,
                study_id,
                context_limit,
                reason="Eggopt reflection context budget",
            )
        reflector = EggthreadsReflectionLM(
            flow,
            threads,
            drive=reflection.drive,
            reflector_id="eggopt.reflection",
            reflector_version="1",
            reflector_config=reflection.identity,
            study_thread_id=study_id,
            reflection_instruction=reflection.instruction,
        )
        return cls(root, store, flow, threads, study_id, reflector)

    def close(self) -> None:
        self.threads.conn.close()
        self.store.conn.close()

    def __enter__(self) -> Runtime[ExampleT, OutputT]:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class _CreateStudy(Task):
    threads: ThreadsDB
    reflection: Reflection
    workspace: Path
    name: str
    legacy_id: str | None = None

    def get_cache_key(self) -> str:
        return digest_payload("eggopt.runtime.create-study.v1", {})

    def run(self) -> str:
        if self.legacy_id is not None:
            return self.legacy_id
        if getattr(self.reflection.drive, "requires_study_thread", False):
            thread_id, _profile = create_solver_safe_study(
                self.threads,
                workspace=self.workspace,
                model_key=self.reflection.model_key,
                models_path=self.reflection.models_path,
                name=self.name,
                allowed_tools=self.reflection.allowed_tools,
            )
            return thread_id
        return create_root_thread(self.threads, name=self.name)


def _legacy_study_id(threads: ThreadsDB) -> str | None:
    """Read compatibility for studies created before the cached setup Task."""

    row = threads.conn.execute(
        "SELECT json_extract(payload_json, '$.study_id') "
        "FROM events WHERE type='eggopt.study' ORDER BY event_seq LIMIT 1"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("Runtime.open() cannot run inside an active asyncio loop")
