from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, Task

from ._identity import canonical_candidate, canonical_json, digest_payload
from .evaluation import Evaluation, evaluate_all
from .runtime import Metric, Reflection, Runtime

ExampleT = TypeVar("ExampleT")
OutputT = TypeVar("OutputT")
Candidate = dict[str, str]


@dataclass(frozen=True)
class NativeGEPAResult(Generic[OutputT]):
    """A small, complete account of one native search."""

    candidates: tuple[Candidate, ...]
    scores: tuple[float, ...]
    parents: tuple[int | None, ...]
    outputs: tuple[tuple[OutputT | None, ...], ...]
    metric_calls: int
    best_index: int

    @property
    def best_candidate(self) -> Candidate:
        return dict(self.candidates[self.best_index])

    @property
    def best_score(self) -> float:
        return self.scores[self.best_index]


@dataclass
class _NativeSearch(Task, Generic[ExampleT, OutputT]):
    cacheable = False

    flow: FlowExecutor
    seed: Candidate
    trainset: list[ExampleT]
    valset: list[ExampleT]
    metric: Metric[ExampleT, OutputT]
    metric_identity: Any
    example_id: Callable[[ExampleT], Any]
    reflector: Any
    generations: int
    proposals_per_generation: int
    max_metric_calls: int | None
    max_concurrency: int | None

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.v1",
            {
                "seed": canonical_candidate(self.seed),
                "trainset": [self.example_id(example) for example in self.trainset],
                "valset": [self.example_id(example) for example in self.valset],
                "metric": self.metric_identity,
                "generations": self.generations,
                "proposals_per_generation": self.proposals_per_generation,
                "max_metric_calls": self.max_metric_calls,
            },
        )

    def run(self):
        seed = dict(canonical_candidate(self.seed))
        first = yield _EvaluateCandidate(
            self.flow,
            seed,
            self.valset,
            self.metric,
            self.metric_identity,
            self.example_id,
            self.max_concurrency,
        )
        candidates = [seed]
        scores = [first.score]
        parents: list[int | None] = [None]
        outputs = [first.outputs]
        calls = first.metric_calls
        best = 0

        for _ in range(self.generations):
            evidence = yield _EvaluateCandidate(
                self.flow,
                candidates[best],
                self.trainset,
                self.metric,
                self.metric_identity,
                self.example_id,
                self.max_concurrency,
            )
            proposals = yield _Reflect(
                self.reflector,
                [
                    (
                        candidates[best],
                        _reflective_dataset(evidence.evaluations),
                        list(candidates[best]),
                    )
                    for _ in range(self.proposals_per_generation)
                ],
            )
            proposed = [dict(proposal.new_texts) for proposal, _ in proposals]
            if not proposed:
                break
            remaining = None if self.max_metric_calls is None else self.max_metric_calls - calls
            if remaining is not None:
                proposed = proposed[: max(0, remaining // max(1, len(self.valset)))]
            if not proposed:
                break
            evaluated = yield [
                _EvaluateCandidate(
                    self.flow,
                    candidate,
                    self.valset,
                    self.metric,
                    self.metric_identity,
                    self.example_id,
                    self.max_concurrency,
                )
                for candidate in proposed
            ]
            for candidate, result in zip(proposed, evaluated, strict=True):
                candidates.append(candidate)
                scores.append(result.score)
                parents.append(best)
                outputs.append(result.outputs)
                calls += result.metric_calls
            best = max(range(len(scores)), key=scores.__getitem__)

        return NativeGEPAResult(
            candidates=tuple(candidates),
            scores=tuple(scores),
            parents=tuple(parents),
            outputs=tuple(outputs),
            metric_calls=calls,
            best_index=best,
        )


@dataclass(frozen=True)
class _CandidateEvaluation(Generic[OutputT]):
    score: float
    evaluations: tuple[Evaluation[OutputT], ...]
    outputs: tuple[OutputT | None, ...]
    metric_calls: int


@dataclass
class _Reflect(Task):
    cacheable = False

    reflector: Any
    jobs: list[Any]

    async def run(self) -> Any:
        return await self.reflector.reflect_many_async(self.jobs)


@dataclass
class _EvaluateCandidate(Task, Generic[ExampleT, OutputT]):
    cacheable = False

    flow: FlowExecutor
    candidate: Candidate
    examples: list[ExampleT]
    metric: Metric[ExampleT, OutputT]
    metric_identity: Any
    example_id: Callable[[ExampleT], Any]
    max_concurrency: int | None

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.evaluate-candidate.v1",
            {
                "candidate": canonical_candidate(self.candidate),
                "examples": [self.example_id(example) for example in self.examples],
                "metric": self.metric_identity,
            },
        )

    async def run(self) -> _CandidateEvaluation[OutputT]:
        evaluations, calls = await evaluate_all(
            self.flow,
            self.candidate,
            self.examples,
            metric=self.metric,
            metric_identity=self.metric_identity,
            example_id=self.example_id,
            max_concurrency=self.max_concurrency,
        )
        return _CandidateEvaluation(
            score=fmean(item.score for item in evaluations) if evaluations else 0.0,
            evaluations=tuple(evaluations),
            outputs=tuple(item.output for item in evaluations),
            metric_calls=calls,
        )


class NativeGEPA(Generic[ExampleT, OutputT]):
    """Eggflow-native GEPA: evaluate, reflect, keep the best, repeat."""

    def __init__(
        self,
        *,
        metric: Metric[ExampleT, OutputT],
        reflection: Reflection,
        run_dir: str | Path,
        generations: int = 3,
        proposals_per_generation: int = 1,
        max_metric_calls: int | None = None,
        metric_identity: Any | None = None,
        example_id: Callable[[ExampleT], Any] | None = None,
        max_concurrent_evaluations: int | None = None,
    ) -> None:
        if generations < 0:
            raise ValueError("generations must be non-negative")
        if proposals_per_generation < 1:
            raise ValueError("proposals_per_generation must be positive")
        self.metric = metric
        self.reflection = reflection
        self.run_dir = Path(run_dir)
        self.generations = generations
        self.proposals_per_generation = proposals_per_generation
        self.max_metric_calls = max_metric_calls
        self.metric_identity = metric_identity or _callable_identity(metric)
        self.example_id = example_id or _example_identity
        self.max_concurrent_evaluations = max_concurrent_evaluations

    def compile(
        self,
        student: Mapping[str, str] | str,
        *,
        trainset: Sequence[ExampleT],
        valset: Sequence[ExampleT] | None = None,
    ) -> NativeGEPAResult[OutputT]:
        seed = _candidate(student)
        with Runtime.open(self.run_dir, self.reflection) as runtime:
            runtime.reflection.resume_uncommitted()
            return _sync(
                runtime.flow.run(
                    _NativeSearch(
                        runtime.flow,
                        seed,
                        list(trainset),
                        list(valset if valset is not None else trainset),
                        self.metric,
                        self.metric_identity,
                        self.example_id,
                        runtime.reflection,
                        self.generations,
                        self.proposals_per_generation,
                        self.max_metric_calls,
                        self.max_concurrent_evaluations,
                    )
                )
            )


def _reflective_dataset(
    evaluations: Sequence[Evaluation[Any]],
) -> Mapping[str, list[Mapping[str, Any]]]:
    records = [
        {
            "Inputs": item.evidence,
            "Generated Outputs": item.output,
            "Feedback": item.feedback,
            "Score": item.score,
        }
        for item in evaluations
    ]
    return {"candidate": records}


def _candidate(student: Mapping[str, str] | str) -> Candidate:
    if isinstance(student, str):
        return {"prompt": student}
    return dict(canonical_candidate(student))


def _callable_identity(function: Any) -> Mapping[str, str]:
    return {
        "module": getattr(function, "__module__", ""),
        "name": getattr(function, "__qualname__", function.__class__.__qualname__),
    }


def _example_identity(example: ExampleT) -> Any:
    try:
        canonical_json(example, what="example")
    except TypeError:
        if hasattr(example, "__dict__"):
            return vars(example)
        raise TypeError("provide example_id for non-JSON examples") from None
    return example


def _sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("NativeGEPA.compile() cannot run inside an active asyncio loop")
