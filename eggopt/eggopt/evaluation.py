from __future__ import annotations

import inspect
import pickle
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, Task

from ._identity import canonical_candidate, canonical_json, digest_payload

ExampleT = TypeVar("ExampleT")
OutputT = TypeVar("OutputT")

_EVALUATION_OPERATION = "eggopt.evaluate.v1"


@dataclass(frozen=True)
class Evaluation(Generic[OutputT]):
    """The useful truth produced by one program run."""

    score: float
    output: OutputT | None = None
    feedback: str = ""
    objectives: Mapping[str, float] | None = None
    evidence: Any = None

    def __post_init__(self) -> None:
        score = _score(self.score, "score")
        if not isinstance(self.feedback, str):
            raise TypeError("feedback must be a string")
        objectives = None
        if self.objectives is not None:
            if not isinstance(self.objectives, Mapping):
                raise TypeError("objectives must be a mapping or None")
            objectives = {
                str(name): _score(value, str(name)) for name, value in self.objectives.items()
            }
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "objectives", objectives)
        try:
            pickle.dumps(self)
        except Exception as exc:
            raise TypeError("evaluation values must be pickle-safe") from exc


@dataclass(frozen=True)
class EvaluationKey:
    evaluator: str
    candidate: tuple[tuple[str, str], ...]
    example: str
    operation: str = _EVALUATION_OPERATION

    def digest(self) -> str:
        return digest_payload(
            self.operation,
            {
                "operation": self.operation,
                "evaluator": self.evaluator,
                "candidate": self.candidate,
                "example": self.example,
            },
        )


@dataclass
class Evaluate(Task, Generic[ExampleT, OutputT]):
    """A durable metric call."""

    key: EvaluationKey
    candidate: dict[str, str]
    example: ExampleT
    metric: Callable[[Mapping[str, str], ExampleT], Evaluation[OutputT] | float]

    def get_cache_key(self) -> str:
        return self.key.digest()

    async def run(self) -> Evaluation[OutputT]:
        value = self.metric(dict(self.candidate), self.example)
        if inspect.isawaitable(value):
            value = await value
        return as_evaluation(value)


def evaluation_task(
    candidate: Mapping[str, str],
    example: ExampleT,
    *,
    metric: Callable[[Mapping[str, str], ExampleT], Evaluation[OutputT] | float],
    metric_identity: Any,
    example_identity: Any,
) -> Evaluate[ExampleT, OutputT]:
    """Build the one primitive both GEPA implementations cache through Eggflow."""

    normalized = dict(canonical_candidate(candidate))
    return Evaluate(
        key=EvaluationKey(
            evaluator=canonical_json(metric_identity, what="metric identity"),
            candidate=tuple(normalized.items()),
            example=canonical_json(example_identity, what="example identity"),
        ),
        candidate=normalized,
        example=example,
        metric=metric,
    )


async def evaluate_all(
    executor: FlowExecutor,
    candidate: Mapping[str, str],
    examples: list[ExampleT],
    *,
    metric: Callable[[Mapping[str, str], ExampleT], Evaluation[OutputT] | float],
    metric_identity: Any,
    example_id: Callable[[ExampleT], Any],
    max_concurrency: int | None = None,
) -> tuple[list[Evaluation[OutputT]], int]:
    tasks = [
        evaluation_task(
            candidate,
            example,
            metric=metric,
            metric_identity=metric_identity,
            example_identity=example_id(example),
        )
        for example in examples
    ]
    calls = sum(
        1
        for task in tasks
        if (row := executor.store.get(task.get_cache_key())) is None or row["status"] != "COMPLETED"
    )
    if not tasks:
        return [], calls
    if max_concurrency is None:
        values = await executor.run(tasks)
    else:
        values = []
        for start in range(0, len(tasks), max_concurrency):
            values.extend(await executor.run(tasks[start : start + max_concurrency]))
    return [as_evaluation(value) for value in values], calls


def as_evaluation(value: Evaluation[OutputT] | float) -> Evaluation[OutputT]:
    if isinstance(value, Evaluation):
        return value
    return Evaluation(score=_score(value, "metric score"))


def _score(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be a finite number")
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise ValueError(f"{what} must be finite")
    return number
