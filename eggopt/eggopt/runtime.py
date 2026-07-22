from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar
from uuid import uuid4

from eggflow import FlowExecutor, TaskStore
from eggthreads import RunnerConfig, ThreadsDB, ToolRegistry, create_root_thread

from .evaluation import Evaluation
from .gepa.production_drive import (
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
        max_correction_turns: int = 0,
        context_ceiling_tokens: int | None = None,
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
                max_correction_turns=max_correction_turns,
                context_ceiling_tokens=context_ceiling_tokens,
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
        threads = ThreadsDB(egg / "threads.sqlite")
        threads.init_schema()
        study_id = _study_id(threads)
        if study_id is None:
            workspace = Path(
                reflection.workspace
                or default_workspace
                or root / "workspaces" / "mutation"
            )
            name = study_name or reflection.study_name
            if getattr(reflection.drive, "requires_study_thread", False):
                study_id, _ = create_solver_safe_study(
                    threads,
                    workspace=workspace,
                    model_key=reflection.model_key,
                    models_path=reflection.models_path,
                    name=name,
                    allowed_tools=reflection.allowed_tools,
                )
            else:
                study_id = create_root_thread(threads, name=name)
            threads.append_event(
                event_id=uuid4().hex,
                thread_id=study_id,
                type_="eggopt.study",
                payload={"study_id": study_id},
            )
            threads.conn.commit()
        flow = FlowExecutor(store)
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


def _study_id(threads: ThreadsDB) -> str | None:
    row = threads.conn.execute(
        "SELECT json_extract(payload_json, '$.study_id') "
        "FROM events WHERE type='eggopt.study' ORDER BY event_seq LIMIT 1"
    ).fetchone()
    return str(row[0]) if row and row[0] else None
