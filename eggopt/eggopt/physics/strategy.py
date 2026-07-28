from __future__ import annotations

import hashlib
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, keyed
from eggthreads import (
    create_child_thread,
    create_root_thread,
    list_children_with_meta,
    list_root_threads,
    list_threads,
    record_synthetic_user_tool_call,
    set_thread_working_directory,
)

from ..context import _current_operation, _operation_runtime, _operation_scope
from ..identity import digest_payload
from ..runtime import Runtime, sync

TaskFactory = Callable[..., Task]


@dataclass(frozen=True)
class PhysicsResult:
    """Operational result of one durable observe-think-act loop."""

    timeline: tuple[Any, ...]
    hypotheses: Any
    evidence: Any
    stopping_reason: str
    cycles: int
    actions: int
    physics_thread_id: str
    environment_thread_id: str
    hypotheses_thread_id: str
    plan_thread_id: str


@dataclass
class PhysicsEffect(Task):
    """Run an effect Task and record it on the shared Environment thread."""

    effect: Task = field(repr=False, compare=False)
    thread_id: str
    name: str
    arguments: Any
    origin: str = "eggopt.physics"

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.physics.effect.v1",
            {
                "thread": self.thread_id,
                "name": self.name,
                "arguments": _identity(self.arguments),
                "effect": self.effect.get_cache_key(),
                "origin": self.origin,
            },
        )

    def run(self):
        output = yield self.effect
        context = _current_operation()
        record_synthetic_user_tool_call(
            _operation_runtime(str(context["_runtime_key"])),
            self.thread_id,
            self.name,
            self.arguments,
            repr(output),
            origin=self.origin,
            tool_call_id=self.get_cache_key().rsplit(":", 1)[-1],
        )
        return output

    async def recover(self) -> bool:
        recovered = self.effect.recover()
        if hasattr(recovered, "__await__"):
            recovered = await recovered
        return bool(recovered)


@dataclass(frozen=True)
class PhysicsStrategy:
    """Scientific-method orchestration whose five roles construct Eggflow Tasks."""

    observe: TaskFactory = field(repr=False, compare=False)
    hypothesize: TaskFactory = field(repr=False, compare=False)
    test: TaskFactory = field(repr=False, compare=False)
    deliberate: TaskFactory = field(repr=False, compare=False)
    execute: TaskFactory = field(repr=False, compare=False)
    identity: Any

    def __post_init__(self) -> None:
        for name in ("observe", "hypothesize", "test", "deliberate", "execute"):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must construct an Eggflow Task")
        digest_payload("eggopt.physics.identity.v1", self.identity)

    def run(
        self,
        *,
        run_dir: str | Path = ".eggopt/physics",
        max_actions: int = 100,
        max_cycles: int = 100,
    ) -> PhysicsResult:
        """Run or resume this strategy in one run-owned ``.egg`` directory."""

        for name, value in (("max_actions", max_actions), ("max_cycles", max_cycles)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        with Runtime.open(run_dir) as runtime:
            threads = sync(
                runtime.flow.run(_EnsurePhysicsThreads(runtime.threads)),
                operation="PhysicsStrategy",
            )
            task = _PhysicsLoop(
                self,
                runtime.runtime_key,
                str(runtime.root),
                threads,
                max_actions,
                max_cycles,
            )
            return sync(runtime.flow.run(task), operation="PhysicsStrategy")


def run_physics(
    strategy: PhysicsStrategy,
    *,
    run_dir: str | Path = ".eggopt/physics",
    max_actions: int = 100,
    max_cycles: int = 100,
) -> PhysicsResult:
    """Run or resume ``strategy`` with a compact functional API."""

    if not isinstance(strategy, PhysicsStrategy):
        raise TypeError("strategy must be a PhysicsStrategy")
    return strategy.run(
        run_dir=run_dir,
        max_actions=max_actions,
        max_cycles=max_cycles,
    )


@dataclass
class _EnsurePhysicsThreads(Task):
    threads: Any = field(repr=False, compare=False)

    def get_cache_key(self) -> str:
        return digest_payload("eggopt.physics.create-study.v1", {})

    def run(self) -> tuple[str, str, str, str]:
        roots = [
            thread_id
            for thread_id, name, *_ in _root_threads(self.threads)
            if name == "Physics"
        ]
        if len(roots) > 1:
            raise RuntimeError("Physics run has multiple root threads")
        physics_id = (
            roots[0] if roots else create_root_thread(self.threads, name="Physics")
        )
        children = {
            name: thread_id
            for thread_id, name, *_ in list_children_with_meta(self.threads, physics_id)
        }
        required = ("Environment", "Hypotheses", "Plan")
        for name in required:
            if name not in children:
                children[name] = create_child_thread(
                    self.threads,
                    physics_id,
                    name=name,
                    inherit_tools_config=False,
                )
        return physics_id, *(children[name] for name in required)


def _root_threads(db: Any) -> list[tuple[str, str, str, str]]:
    # There is intentionally no storage query here. Root IDs and public thread
    # metadata come from Eggthreads' API.
    root_ids = set(list_root_threads(db))
    return [
        (thread.thread_id, thread.name, thread.short_recap, thread.created_at)
        for thread in list_threads(db)
        if thread.thread_id in root_ids
    ]


@dataclass
class _PhysicsLoop(Task):
    cacheable = False

    strategy: PhysicsStrategy = field(repr=False, compare=False)
    runtime_key: str
    run_dir: str
    thread_ids: tuple[str, str, str, str]
    max_actions: int
    max_cycles: int

    def run(self):
        _, environment_id, hypotheses_id, plan_id = self.thread_ids
        timeline: tuple[Any, ...] = ()
        hypotheses: Any = None
        evidence: Any = None
        actions = 0
        cycles = 0

        initial = yield self._task(
            "observe",
            self.strategy.observe(
                timeline=timeline,
                thread_id=environment_id,
                workspace=self._workspace("environment"),
            ),
            timeline,
        )
        timeline += (initial,)

        while cycles < self.max_cycles and actions < self.max_actions:
            cycles += 1
            hypotheses = yield self._task(
                "hypothesize",
                self.strategy.hypothesize(
                    timeline=timeline,
                    hypotheses=hypotheses,
                    evidence=evidence,
                    thread_id=hypotheses_id,
                    workspace=self._workspace("hypotheses"),
                ),
                timeline,
                hypotheses,
                evidence,
                operation_context=True,
            )
            feedback = yield self._task(
                "test",
                self.strategy.test(
                    hypotheses=hypotheses,
                    timeline=timeline,
                    commitment=None,
                    thread_id=hypotheses_id,
                    workspace=self._workspace("hypotheses"),
                ),
                hypotheses,
                timeline,
                None,
            )
            if feedback is not None:
                evidence = feedback
                continue
            evidence = None
            commitment = yield self._task(
                "deliberate",
                self.strategy.deliberate(
                    timeline=timeline,
                    hypotheses=hypotheses,
                    evidence=evidence,
                    thread_id=plan_id,
                    workspace=self._workspace("plan"),
                ),
                hypotheses,
                evidence,
                timeline,
            )
            if commitment is None:
                return self._result(
                    timeline,
                    hypotheses,
                    evidence,
                    "deliberated",
                    cycles,
                    actions,
                )
            if isinstance(commitment, (str, bytes)):
                raise TypeError("deliberate must return an iterable of intents or None")
            intents = tuple(commitment)
            if not intents:
                raise ValueError("deliberate returned an empty commitment")

            contradicted = False
            for position, intent in enumerate(intents):
                if actions >= self.max_actions:
                    break
                transition = yield self._task(
                    "execute",
                    self.strategy.execute(
                        timeline=timeline,
                        intent=intent,
                        thread_id=environment_id,
                        workspace=self._workspace("environment"),
                    ),
                    timeline,
                    intent,
                    position,
                )
                timeline += (transition,)
                actions += 1
                feedback = yield self._task(
                    "test",
                    self.strategy.test(
                        hypotheses=hypotheses,
                        timeline=timeline,
                        commitment=intent,
                        thread_id=hypotheses_id,
                        workspace=self._workspace("hypotheses"),
                    ),
                    hypotheses,
                    timeline,
                    intent,
                )
                if feedback is not None:
                    evidence = feedback
                    contradicted = True
                    break
            if contradicted:
                continue

        reason = "max_actions" if actions >= self.max_actions else "max_cycles"
        return self._result(
            timeline,
            hypotheses,
            evidence,
            reason,
            cycles,
            actions,
        )

    def _task(
        self,
        role: str,
        task: Any,
        *dependencies: Any,
        operation_context: bool = False,
    ) -> Task:
        if not isinstance(task, Task):
            raise TypeError(f"{role} must construct an Eggflow Task")
        dependency_key = digest_payload(
            f"eggopt.physics.{role}.operation.v1",
            [_identity(value) for value in dependencies],
        )
        context = self._context(role)
        if operation_context:
            context["_evaluation_key"] = dependency_key
        return _OperationTask(
            task,
            self.runtime_key,
            context,
            dependency_key,
        )

    def _context(self, role: str) -> dict[str, Any]:
        physics_id, environment_id, hypotheses_id, plan_id = self.thread_ids
        thread_id = {
            "observe": environment_id,
            "execute": environment_id,
            "hypothesize": hypotheses_id,
            "test": hypotheses_id,
            "deliberate": plan_id,
        }[role]
        workspace = self._workspace(
            {
                "observe": "environment",
                "execute": "environment",
                "hypothesize": "hypotheses",
                "test": "hypotheses",
                "deliberate": "plan",
            }[role]
        )
        return {
            "operation_thread_id": thread_id,
            "evaluation_thread_id": thread_id,
            "physics_thread_id": physics_id,
            "environment_thread_id": environment_id,
            "hypotheses_thread_id": hypotheses_id,
            "plan_thread_id": plan_id,
            "outer_context": workspace,
            "inner_context": str(Path(workspace) / "innerContext"),
            "_runtime_key": self.runtime_key,
            "_evaluation_key": f"eggopt.physics.{role}.v1",
            "_context_limit": None,
        }

    def _workspace(self, name: str) -> str:
        return str(Path(self.run_dir) / "workspaces" / name)

    def _result(self, timeline, hypotheses, evidence, reason, cycles, actions):
        return PhysicsResult(
            timeline=timeline,
            hypotheses=hypotheses,
            evidence=evidence,
            stopping_reason=reason,
            cycles=cycles,
            actions=actions,
            physics_thread_id=self.thread_ids[0],
            environment_thread_id=self.thread_ids[1],
            hypotheses_thread_id=self.thread_ids[2],
            plan_thread_id=self.thread_ids[3],
        )


@dataclass
class _OperationTask(Task):
    task: Task = field(repr=False, compare=False)
    runtime_key: str
    context: dict[str, Any]
    dependency_key: str

    def get_cache_key(self) -> str:
        task_key = self.task.get_cache_key()
        return digest_payload(
            "eggopt.physics.operation.v1",
            {"task": task_key, "dependency": self.dependency_key},
        )

    def run(self):
        workspace = Path(self.context["outer_context"])
        Path(self.context["inner_context"]).mkdir(parents=True, exist_ok=True)
        set_thread_working_directory(
            _operation_runtime(self.runtime_key),
            self.context["operation_thread_id"],
            str(workspace),
            reason="PhysicsStrategy operation workspace",
        )
        with _operation_scope(self.context):
            return (yield keyed(self.task, self.dependency_key))

    async def recover(self) -> bool:
        with _operation_scope(self.context):
            recovered = self.task.recover()
            if hasattr(recovered, "__await__"):
                recovered = await recovered
            return bool(recovered)


def _identity(value: Any) -> str:
    try:
        return digest_payload("eggopt.physics.value.v1", value)
    except TypeError:
        try:
            payload = pickle.dumps(value)
        except Exception as exc:
            raise TypeError("Physics values must have durable identities") from exc
        try:
            if pickle.dumps(pickle.loads(payload)) != payload:
                raise TypeError("Physics values must pickle deterministically")
        except Exception as exc:
            raise TypeError("Physics values must have durable identities") from exc
        return "pickle:" + hashlib.sha256(payload).hexdigest()


__all__ = ["PhysicsEffect", "PhysicsResult", "PhysicsStrategy", "run_physics"]
