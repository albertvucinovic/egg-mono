from __future__ import annotations

import asyncio
import inspect
import json
import math
import pickle
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, Task

try:
    from gepa import EvaluationBatch
except ImportError:  # NativeGEPA remains usable without the optional upstream package.
    EvaluationBatch = Any

from ._identity import canonical_candidate, canonical_json, digest_payload

ExampleT = TypeVar("ExampleT")
OutputT = TypeVar("OutputT")

_EVALUATION_OPERATION = "eggopt.gepa.evaluate-example.v1"


@dataclass(frozen=True)
class ReflectionEvidence:
    """Typed evidence that is converted to GEPA's reflective dataset."""

    inputs: Mapping[str, Any]
    generated_outputs: Any
    feedback: str

    def __post_init__(self) -> None:
        if not isinstance(self.feedback, str):
            raise TypeError("evidence feedback must be a string")
        inputs = _json_mapping(self.inputs, "evidence inputs")
        generated = _json_value(self.generated_outputs, "generated outputs")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "generated_outputs", generated)

    def as_reflective_record(self, score: float) -> dict[str, Any]:
        return {
            "Inputs": dict(self.inputs),
            "Generated Outputs": self.generated_outputs,
            "Feedback": self.feedback,
            "Score": score,
        }


@dataclass(frozen=True)
class ExampleEvaluation(Generic[OutputT]):
    """Authoritative, pickle-safe result of one candidate/example evaluation."""

    output: OutputT
    score: float
    evidence: ReflectionEvidence
    objective_scores: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        score = _finite_score(self.score, "score")
        if not isinstance(self.evidence, ReflectionEvidence):
            raise TypeError("evidence must be ReflectionEvidence")
        objectives: dict[str, float] | None = None
        if self.objective_scores is not None:
            if not isinstance(self.objective_scores, Mapping):
                raise TypeError("objective_scores must be a mapping or None")
            objectives = {}
            for name, value in self.objective_scores.items():
                if not isinstance(name, str) or not name:
                    raise TypeError("objective score names must be non-empty strings")
                objectives[name] = _finite_score(value, f"objective score {name!r}")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "objective_scores", objectives)
        try:
            pickle.dumps(self)
        except Exception as exc:
            raise TypeError("evaluation output and evidence must be pickle-safe") from exc


@dataclass(frozen=True)
class EvaluationSemanticKey:
    """Explicit semantic identity for one durable evaluation task."""

    evaluator_id: str
    evaluator_version: str
    evaluator_config_json: str
    candidate: tuple[tuple[str, str], ...]
    example_identity_json: str
    operation: str = _EVALUATION_OPERATION

    def digest(self) -> str:
        return digest_payload(
            self.operation,
            {
                "operation": self.operation,
                "evaluator": {
                    "id": self.evaluator_id,
                    "version": self.evaluator_version,
                    "config": self.evaluator_config_json,
                },
                "candidate": self.candidate,
                "example_identity": self.example_identity_json,
            },
        )


def semantic_workspace_path(
    root: str | Path,
    *,
    candidate_name: str,
    candidate_digest: str,
    case_name: str,
    case_digest: str,
) -> Path:
    """Return a readable, collision-resistant candidate/case workspace path."""

    return (
        Path(root)
        / "candidates"
        / f"{_path_segment(candidate_name, 'candidate')}-{_short_digest(candidate_digest)}"
        / "cases"
        / f"{_path_segment(case_name, 'case')}-{_short_digest(case_digest)}"
    )


@dataclass
class _EvaluateExampleTask(Task, Generic[ExampleT, OutputT]):
    key: EvaluationSemanticKey
    candidate: dict[str, str]
    example: ExampleT
    evaluator: Callable[[Mapping[str, str], ExampleT], ExampleEvaluation[OutputT]]

    def get_cache_key(self) -> str:
        # Deliberately excludes the callable and every executor/store resource.
        return self.key.digest()

    async def run(self) -> ExampleEvaluation[OutputT]:
        value = self.evaluator(dict(self.candidate), self.example)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, ExampleEvaluation):
            raise TypeError("evaluator must return ExampleEvaluation")
        # __post_init__ validated construction; repeat serialization at the task
        # boundary in case a mutable output was changed by evaluator code.
        try:
            pickle.dumps(value)
        except Exception as exc:
            raise TypeError("evaluation output and evidence must be pickle-safe") from exc
        return value


class EggflowGEPAAdapter(Generic[ExampleT, OutputT]):
    """GEPA adapter whose candidate/example executions are Eggflow tasks."""

    propose_new_texts = None

    def __init__(
        self,
        executor: FlowExecutor,
        *,
        evaluator: Callable[[Mapping[str, str], ExampleT], ExampleEvaluation[OutputT]],
        evaluator_id: str,
        evaluator_version: str,
        evaluator_config: Mapping[str, Any],
        example_id: Callable[[ExampleT], Any],
        max_concurrent_evaluations: int | None = None,
    ) -> None:
        if not isinstance(evaluator_id, str) or not evaluator_id:
            raise ValueError("evaluator_id must be a non-empty string")
        if not isinstance(evaluator_version, str) or not evaluator_version:
            raise ValueError("evaluator_version must be a non-empty string")
        if max_concurrent_evaluations is not None:
            if (
                isinstance(max_concurrent_evaluations, bool)
                or not isinstance(max_concurrent_evaluations, int)
                or max_concurrent_evaluations < 1
            ):
                raise ValueError(
                    "max_concurrent_evaluations must be a positive integer or None"
                )
        self.executor = executor
        self.evaluator = evaluator
        self.evaluator_id = evaluator_id
        self.evaluator_version = evaluator_version
        self._evaluator_config_json = canonical_json(
            evaluator_config, what="evaluator_config"
        )
        self.example_id = example_id
        self.max_concurrent_evaluations = max_concurrent_evaluations

    def semantic_key(
        self, candidate: Mapping[str, str], example: ExampleT
    ) -> EvaluationSemanticKey:
        return EvaluationSemanticKey(
            evaluator_id=self.evaluator_id,
            evaluator_version=self.evaluator_version,
            evaluator_config_json=self._evaluator_config_json,
            candidate=canonical_candidate(candidate),
            example_identity_json=canonical_json(
                self.example_id(example), what="example identity"
            ),
        )

    def evaluate(
        self,
        batch: list[ExampleT],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> EvaluationBatch[ReflectionEvidence, OutputT]:
        records, metric_calls = _run_sync(self._evaluate_each(batch, candidate))
        objective_scores: list[dict[str, float]] | None = None
        if any(record.objective_scores is not None for record in records):
            objective_scores = [dict(record.objective_scores or {}) for record in records]
        return EvaluationBatch(
            outputs=[record.output for record in records],
            scores=[record.score for record in records],
            trajectories=[record.evidence for record in records] if capture_traces else None,
            objective_scores=objective_scores,
            num_metric_calls=metric_calls,
        )

    def batch_evaluate(
        self, items: list[tuple[dict[str, str], list[ExampleT]]]
    ) -> list[EvaluationBatch[ReflectionEvidence, OutputT]]:
        return [
            self.evaluate(batch, candidate, capture_traces=True)
            for candidate, batch in items
        ]

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[ReflectionEvidence, OutputT],
        components_to_update: list[str],
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        del candidate
        trajectories = eval_batch.trajectories
        if trajectories is None or len(trajectories) != len(eval_batch.scores):
            raise ValueError("captured ReflectionEvidence must align with scores")
        records = [
            evidence.as_reflective_record(score)
            for evidence, score in zip(trajectories, eval_batch.scores, strict=True)
        ]
        return {
            component: [dict(record) for record in records]
            for component in components_to_update
        }

    async def _evaluate_each(
        self, batch: list[ExampleT], candidate: Mapping[str, str]
    ) -> tuple[list[ExampleEvaluation[OutputT]], int]:
        normalized_candidate = dict(canonical_candidate(candidate))
        records: list[ExampleEvaluation[OutputT]] = []
        metric_calls = 0
        tasks = [
            _EvaluateExampleTask(
                key=self.semantic_key(normalized_candidate, example),
                candidate=normalized_candidate,
                example=example,
                evaluator=self.evaluator,
            )
            for example in batch
        ]
        metric_calls = sum(
            1
            for task in tasks
            if (row := self.executor.store.get(task.get_cache_key())) is None
            or row["status"] != "COMPLETED"
        )
        if not tasks:
            return [], metric_calls
        if self.max_concurrent_evaluations is None:
            values = await self.executor.run(tasks)
        else:
            values = []
            limit = self.max_concurrent_evaluations
            for start in range(0, len(tasks), limit):
                # Each bounded Eggflow batch is parallel internally and returns
                # values in declared order. Chunk concatenation preserves that
                # order without sharing one executor context across coroutines.
                batch_values = await self.executor.run(tasks[start : start + limit])
                values.extend(batch_values)
        for value in values:
            if not isinstance(value, ExampleEvaluation):
                raise TypeError("evaluation task must return ExampleEvaluation")
            records.append(value)
        return records, metric_calls


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError(
        "Eggopt's synchronous GEPA adapter cannot run inside an active asyncio loop"
    )


def _finite_score(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{what} must be finite")
    return result


def _path_segment(value: object, fallback: str) -> str:
    text = "-".join(
        part
        for part in "".join(
            char.casefold() if char.isalnum() else " " for char in str(value or "")
        ).split()
        if part
    )
    return text[:48] or fallback


def _short_digest(value: object) -> str:
    text = str(value or "").strip().casefold()
    if len(text) < 8 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("workspace digest must be at least eight hexadecimal characters")
    return text[:8]


def _json_value(value: Any, what: str) -> Any:
    try:
        return json.loads(canonical_json(value, what=what))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{what} must be canonical JSON data") from exc


def _json_mapping(value: Mapping[str, Any], what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a JSON object")
    result = _json_value(value, what)
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise TypeError(f"{what} must be a JSON object with string keys")
    return result
