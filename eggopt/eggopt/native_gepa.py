from __future__ import annotations

import asyncio
import inspect
import json
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from operator import ge, gt
from pathlib import Path
from statistics import fmean
from typing import Any, Generic, Literal, TypeVar

from eggflow import FlowExecutor, Task
from eggthreads import ThreadsDB, append_message

from ._identity import canonical_candidate, canonical_json, digest_payload
from ._native_evaluation import (
    _EvaluateCandidate,
    _completed_evaluator_calls,
    _feedback,
    _has_message,
    _json_value,
    _new_call_count,
)
from .gepa.reflection import CandidateMutation
from .runtime import Reflection, Runtime

CaseT = TypeVar("CaseT")
OutputT = TypeVar("OutputT")
Candidate = dict[str, str]
MinibatchAcceptance = Literal["strict_improvement", "improvement_or_equal"]

_GENERATION = "eggopt.native-gepa.generate.v1"
_MINIBATCH_ACCEPTANCE = {
    "strict_improvement": gt,
    "improvement_or_equal": ge,
}


@dataclass(frozen=True)
class NativeGEPAConfig:
    """The few controls that materially change a native GEPA study."""

    max_evaluator_calls: int = 100
    max_candidates: int = 10
    reflection_minibatch_size: int = 3
    parents_per_candidate: int = 1
    minibatch_acceptance: MinibatchAcceptance = "strict_improvement"
    seed: int = 0
    run_dir: str | Path = ".eggopt/native-gepa"
    reflection: Reflection | None = None
    generator: Any | None = None
    evaluator_identity: Any | None = None
    case_id: Callable[[Any], Any] | None = field(default=None, repr=False, compare=False)
    max_concurrent_evaluations: int | None = 1

    def __post_init__(self) -> None:
        for name in (
            "max_evaluator_calls",
            "max_candidates",
            "reflection_minibatch_size",
            "parents_per_candidate",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_concurrent_evaluations is not None and (
            isinstance(self.max_concurrent_evaluations, bool)
            or not isinstance(self.max_concurrent_evaluations, int)
            or self.max_concurrent_evaluations < 1
        ):
            raise ValueError("max_concurrent_evaluations must be positive or None")
        if self.minibatch_acceptance not in _MINIBATCH_ACCEPTANCE:
            raise ValueError(
                "minibatch_acceptance must be 'strict_improvement' or "
                "'improvement_or_equal'"
            )
        if self.evaluator_identity is not None:
            canonical_json(self.evaluator_identity, what="evaluator identity")
        if self.generator is not None and not (
            callable(self.generator) or isinstance(self.generator, Task)
        ):
            raise TypeError("generator must be callable, an Eggflow Task, or None")


@dataclass(frozen=True)
class NativeGEPAResult(Generic[OutputT]):
    """The winning candidate and the inspectable search that produced it."""

    candidates: tuple[Candidate, ...]
    scores: tuple[float, ...]
    case_scores: tuple[tuple[float, ...], ...]
    parents: tuple[tuple[int, ...], ...]
    outputs: tuple[tuple[OutputT | None, ...], ...]
    feedback: tuple[tuple[Any, ...], ...]
    evaluator_calls: int
    generated_candidates: int
    best_index: int
    pareto_front: tuple[int, ...]

    @property
    def best_candidate(self) -> Candidate:
        return dict(self.candidates[self.best_index])

    @property
    def best_score(self) -> float:
        return self.scores[self.best_index]

    @property
    def metric_calls(self) -> int:
        """Read compatibility for the first NativeGEPA scaffold."""

        return self.evaluator_calls


@dataclass(frozen=True)
class OptimizationPlan:
    """A conservative cost sketch assuming every proposal is accepted."""

    max_candidates: int
    max_evaluator_calls: int
    generated_candidates: int
    full_evaluations: int
    minibatch_evaluations: int
    minibatch_size: int
    evaluator_calls: int
    additional_generated_candidates: int
    additional_evaluator_calls: int


def plan_optimization(
    *,
    dataset_size: int,
    valset_size: int | None = None,
    max_candidates: int = 10,
    max_evaluator_calls: int = 100,
    reflection_minibatch_size: int = 3,
    completed_candidates: int = 0,
    completed_evaluator_calls: int = 0,
) -> OptimizationPlan:
    """Estimate total and incremental work without opening a study."""

    for name, value, minimum in (
        ("dataset_size", dataset_size, 1),
        ("max_candidates", max_candidates, 1),
        ("max_evaluator_calls", max_evaluator_calls, 1),
        ("reflection_minibatch_size", reflection_minibatch_size, 1),
        ("completed_candidates", completed_candidates, 0),
        ("completed_evaluator_calls", completed_evaluator_calls, 0),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            qualifier = "positive" if minimum else "non-negative"
            raise ValueError(f"{name} must be a {qualifier} integer")
    validation = dataset_size if valset_size is None else valset_size
    if isinstance(validation, bool) or not isinstance(validation, int) or validation < 1:
        raise ValueError("valset_size must be a positive integer")
    batch = min(reflection_minibatch_size, dataset_size)

    # Seed: one full evaluation. Each proposal: one minibatch check and, when
    # accepted, one full evaluation. Cache overlap can make physical cost lower.
    generated = min(
        max_candidates,
        max(0, (max_evaluator_calls - validation) // max(1, batch + validation)),
    )
    total_calls = min(max_evaluator_calls, validation + generated * (batch + validation))
    return OptimizationPlan(
        max_candidates=max_candidates,
        max_evaluator_calls=max_evaluator_calls,
        generated_candidates=generated,
        full_evaluations=1 + generated,
        minibatch_evaluations=generated,
        minibatch_size=batch,
        evaluator_calls=total_calls,
        additional_generated_candidates=max(0, generated - completed_candidates),
        additional_evaluator_calls=max(0, total_calls - completed_evaluator_calls),
    )


@dataclass
class SelectParents(Task):
    """Replaceable boundary selecting distinct Pareto parent indices."""

    scores: tuple[tuple[float, ...], ...]
    count: int
    seed: int
    generation: int

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.select-parents.v1",
            {
                "scores": self.scores,
                "count": self.count,
                "seed": self.seed,
                "generation": self.generation,
            },
        )

    def run(self) -> tuple[int, ...]:
        fronts = _case_fronts(self.scores)
        frequencies = Counter(index for front in fronts for index in front)
        available = sorted(frequencies)
        rng = random.Random(f"{self.seed}:{self.generation}")
        chosen: list[int] = []
        while available and len(chosen) < self.count:
            weights = [frequencies[index] for index in available]
            selected = rng.choices(available, weights=weights, k=1)[0]
            chosen.append(selected)
            available.remove(selected)
        return tuple(chosen)


@dataclass
class GenerateCandidate(Task):
    """Replaceable boundary: selected parents in, exactly one candidate out."""

    reflector: Any = field(repr=False, compare=False)
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    parents: tuple[Candidate, ...]
    evidence: tuple[Mapping[str, Any], ...]
    objective: str
    generation: int

    def get_cache_key(self) -> str:
        return digest_payload(
            _GENERATION,
            {
                "parents": [canonical_candidate(parent) for parent in self.parents],
                "evidence": _json_value(self.evidence, "generation evidence"),
                "objective": self.objective,
                "generation": self.generation,
            },
        )

    async def run(self) -> Candidate:
        lead = self.parents[0]
        dataset = {
            "candidate": [
                {
                    "Objective": self.objective,
                    "Selected Parents": [dict(parent) for parent in self.parents],
                    "Evaluation Evidence": list(self.evidence),
                }
            ]
        }
        mutation = await self.reflector.reflect_in_study_async(
            lead, dataset, list(lead)
        )
        candidate = _apply_mutation(lead, mutation.updates)
        _record_generation(
            self.threads,
            self.study_id,
            self.get_cache_key(),
            self.parents,
            candidate,
            self.generation,
        )
        return candidate


@dataclass
class _CustomGenerateCandidate(Task):
    generator: Any = field(repr=False, compare=False)
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    parents: tuple[Candidate, ...]
    evidence: tuple[Mapping[str, Any], ...]
    objective: str
    generation: int

    def get_cache_key(self) -> str:
        return digest_payload(
            _GENERATION,
            {
                "generator": _callable_identity(self.generator),
                "parents": [canonical_candidate(parent) for parent in self.parents],
                "evidence": _json_value(self.evidence, "generation evidence"),
                "objective": self.objective,
                "generation": self.generation,
            },
        )

    def run(self):
        if isinstance(self.generator, Task):
            value = yield self.generator
        else:
            value = self.generator(self.parents, self.evidence, self.objective)
        if isinstance(value, Task):
            value = yield value
        elif inspect.isawaitable(value):
            value = yield _Await(value)
        candidate = _candidate(value)
        _record_generation(
            self.threads,
            self.study_id,
            self.get_cache_key(),
            self.parents,
            candidate,
            self.generation,
        )
        return candidate


@dataclass
class _NativeSearch(Task, Generic[CaseT, OutputT]):
    cacheable = False

    flow: FlowExecutor = field(repr=False, compare=False)
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    seed_candidate: Candidate
    dataset: list[CaseT] = field(repr=False, compare=False)
    dataset_ids: tuple[Any, ...]
    valset: list[CaseT] = field(repr=False, compare=False)
    valset_ids: tuple[Any, ...]
    evaluator: Any = field(repr=False, compare=False)
    evaluator_identity: Any
    objective: str
    config: NativeGEPAConfig = field(repr=False, compare=False)
    reflector: Any = field(repr=False, compare=False)

    def run(self):
        seed = dict(canonical_candidate(self.seed_candidate))
        seed_needed = _new_call_count(
            self.flow,
            seed,
            self.valset,
            self.valset_ids,
            self.evaluator,
            self.evaluator_identity,
        )
        if _completed_evaluator_calls(self.flow) + seed_needed > self.config.max_evaluator_calls:
            raise ValueError(
                "max_evaluator_calls must cover the seed's full valset evaluation"
            )
        first = yield self._evaluate(seed, self.valset, self.valset_ids)
        candidates = [seed]
        case_scores = [first.scores]
        outputs = [first.outputs]
        feedback = [first.feedback]
        parents: list[tuple[int, ...]] = [()]
        calls = _completed_evaluator_calls(self.flow)
        generated = 0

        while generated < self.config.max_candidates:
            generation = generated
            parent_ids = yield SelectParents(
                tuple(case_scores),
                self.config.parents_per_candidate,
                self.config.seed,
                generation,
            )
            batch_indices = _minibatch_indices(
                len(self.dataset),
                self.config.reflection_minibatch_size,
                self.config.seed,
                generation,
            )
            batch = [self.dataset[index] for index in batch_indices]
            batch_ids = tuple(self.dataset_ids[index] for index in batch_indices)

            parent_evaluations = []
            for parent_id in parent_ids:
                needed = _new_call_count(
                    self.flow,
                    candidates[parent_id],
                    batch,
                    batch_ids,
                    self.evaluator,
                    self.evaluator_identity,
                )
                if calls + needed > self.config.max_evaluator_calls:
                    return _result(
                        candidates, case_scores, parents, outputs, feedback, calls, generated
                    )
                evaluated = yield self._evaluate(candidates[parent_id], batch, batch_ids)
                calls = _completed_evaluator_calls(self.flow)
                parent_evaluations.append(evaluated)

            evidence = tuple(
                _generation_evidence(parent_id, result, batch_ids)
                for parent_id, result in zip(parent_ids, parent_evaluations, strict=True)
            )
            selected = tuple(candidates[index] for index in parent_ids)
            generation_task: Task
            if self.config.generator is None:
                generation_task = GenerateCandidate(
                    self.reflector,
                    self.threads,
                    self.study_id,
                    selected,
                    evidence,
                    self.objective,
                    generation,
                )
            else:
                generation_task = _CustomGenerateCandidate(
                    self.config.generator,
                    self.threads,
                    self.study_id,
                    selected,
                    evidence,
                    self.objective,
                    generation,
                )
            child = yield generation_task
            generated = generation + 1

            child_needed = _new_call_count(
                self.flow,
                child,
                batch,
                batch_ids,
                self.evaluator,
                self.evaluator_identity,
            )
            if calls + child_needed > self.config.max_evaluator_calls:
                return _result(
                    candidates, case_scores, parents, outputs, feedback, calls, generated
                )
            child_batch = yield self._evaluate(child, batch, batch_ids)
            calls = _completed_evaluator_calls(self.flow)
            # Multiple selected parents may specialize on different cases;
            # compare the child with the strongest per-case parent envelope.
            if not _accept_minibatch(
                child_batch.scores,
                parent_evaluations,
                self.config.minibatch_acceptance,
            ):
                continue

            full_needed = _new_call_count(
                self.flow,
                child,
                self.valset,
                self.valset_ids,
                self.evaluator,
                self.evaluator_identity,
            )
            if calls + full_needed > self.config.max_evaluator_calls:
                continue
            full = yield self._evaluate(child, self.valset, self.valset_ids)
            calls = _completed_evaluator_calls(self.flow)
            candidates.append(child)
            case_scores.append(full.scores)
            outputs.append(full.outputs)
            feedback.append(full.feedback)
            parents.append(parent_ids)

        return _result(candidates, case_scores, parents, outputs, feedback, calls, generated)

    def _evaluate(self, candidate, cases, case_ids):
        return _EvaluateCandidate(
            self.flow,
            self.threads,
            self.study_id,
            Path(self.config.run_dir).resolve(),
            candidate,
            cases,
            case_ids,
            self.evaluator,
            self.evaluator_identity,
            self.config.max_concurrent_evaluations,
        )


class NativeGEPA(Generic[CaseT, OutputT]):
    """Compatibility wrapper around :func:`optimize_anything`."""

    def __init__(
        self,
        *,
        evaluator: Any | None = None,
        metric: Any | None = None,
        objective: str = "Improve the candidate.",
        config: NativeGEPAConfig | None = None,
        **legacy: Any,
    ) -> None:
        self.evaluator = evaluator or metric
        if self.evaluator is None:
            raise TypeError("evaluator is required")
        self.objective = objective
        if config is None:
            config = NativeGEPAConfig(
                max_candidates=int(legacy.pop("generations", 3)),
                max_evaluator_calls=legacy.pop("max_metric_calls", 100),
                run_dir=legacy.pop("run_dir", ".eggopt/native-gepa"),
                reflection=legacy.pop("reflection", None),
                evaluator_identity=legacy.pop("metric_identity", None),
                case_id=legacy.pop("example_id", None),
                max_concurrent_evaluations=legacy.pop(
                    "max_concurrent_evaluations", 1
                ),
            )
        if legacy:
            raise TypeError(f"unknown NativeGEPA options: {sorted(legacy)}")
        self.config = config

    def compile(self, student, *, trainset, valset=None):
        return optimize_anything(
            student,
            evaluator=self.evaluator,
            dataset=trainset,
            valset=valset,
            objective=self.objective,
            config=self.config,
        )


def optimize_anything(
    seed_candidate: str | Mapping[str, str],
    *,
    evaluator: Any,
    dataset: Sequence[CaseT],
    valset: Sequence[CaseT] | None = None,
    objective: str,
    config: NativeGEPAConfig | None = None,
) -> NativeGEPAResult[Any]:
    """Optimize a text candidate with Eggflow-native, case-wise Pareto search."""

    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective must be a non-empty string")
    if not (callable(evaluator) or callable(getattr(evaluator, "task", None))):
        raise TypeError("evaluator must be callable or expose task(candidate, case)")
    data = list(dataset)
    validation = list(data if valset is None else valset)
    if not data:
        raise ValueError("dataset must not be empty")
    if not validation:
        raise ValueError("valset must not be empty")
    config = config or NativeGEPAConfig()
    if config.reflection is None and config.generator is None:
        raise TypeError("config.reflection or config.generator is required")

    case_id = config.case_id or _case_identity
    evaluator_identity = config.evaluator_identity or _callable_identity(evaluator)
    reflection = config.reflection or _placeholder_reflection(config.generator)
    with Runtime.open(
        config.run_dir,
        reflection,
        study_name="Mutation",
        default_workspace=Path(config.run_dir).resolve(),
    ) as runtime:
        runtime.reflection.resume_uncommitted_in_study()
        return _sync(
            runtime.flow.run(
                _NativeSearch(
                    runtime.flow,
                    runtime.threads,
                    runtime.study_id,
                    _candidate(seed_candidate),
                    data,
                    tuple(case_id(case) for case in data),
                    validation,
                    tuple(case_id(case) for case in validation),
                    evaluator,
                    evaluator_identity,
                    objective.strip(),
                    config,
                    runtime.reflection,
                )
            )
        )

def _minibatch_indices(size: int, batch_size: int, seed: int, generation: int):
    order = list(range(size))
    epoch_size = max(1, (size + batch_size - 1) // batch_size)
    epoch, chunk = divmod(generation, epoch_size)
    random.Random(f"{seed}:{epoch}").shuffle(order)
    padding = (-len(order)) % batch_size
    padded = order + [order[index % size] for index in range(padding)]
    start = chunk * batch_size
    return tuple(padded[start : start + min(batch_size, size)])


def _accept_minibatch(child_scores, parent_evaluations, criterion):
    parent_total = sum(
        max(item.scores[case] for item in parent_evaluations)
        for case in range(len(child_scores))
    )
    child_total = sum(child_scores)
    return _MINIBATCH_ACCEPTANCE[criterion](child_total, parent_total)


def _case_fronts(scores: Sequence[Sequence[float]]) -> tuple[tuple[int, ...], ...]:
    if not scores:
        return ()
    return tuple(
        tuple(index for index, row in enumerate(scores) if row[case] == best)
        for case in range(len(scores[0]))
        for best in [max(row[case] for row in scores)]
    )


def _pareto_candidates(scores: Sequence[Sequence[float]]) -> tuple[int, ...]:
    return tuple(sorted({index for front in _case_fronts(scores) for index in front}))


def _result(candidates, case_scores, parents, outputs, feedback, calls, generated):
    aggregates = tuple(fmean(scores) for scores in case_scores)
    best = max(range(len(aggregates)), key=aggregates.__getitem__)
    return NativeGEPAResult(
        candidates=tuple(dict(candidate) for candidate in candidates),
        scores=aggregates,
        case_scores=tuple(tuple(scores) for scores in case_scores),
        parents=tuple(parents),
        outputs=tuple(outputs),
        feedback=tuple(feedback),
        evaluator_calls=calls,
        generated_candidates=generated,
        best_index=best,
        pareto_front=_pareto_candidates(case_scores),
    )


def _generation_evidence(parent_id, evaluation, case_ids):
    return {
        "parent_index": parent_id,
        "candidate_evaluation_thread_id": evaluation.candidate_thread_id,
        "cases": [
            {
                "case": case_id,
                "score": item.score,
                "feedback": _feedback(item),
                "evaluation_thread_id": case_thread_id,
            }
            for case_id, case_thread_id, item in zip(
                case_ids,
                evaluation.case_thread_ids,
                evaluation.evaluations,
                strict=True,
            )
        ],
    }


def _apply_mutation(parent: Candidate, updates: Mapping[str, str]) -> Candidate:
    unknown = set(updates) - set(parent)
    if unknown:
        raise ValueError(f"generator added unknown candidate components: {sorted(unknown)}")
    return dict(canonical_candidate({**parent, **updates}))


def _candidate(value: Any) -> Candidate:
    if isinstance(value, CandidateMutation):
        value = value.updates
    if isinstance(value, str):
        return {"prompt": value}
    if not isinstance(value, Mapping):
        raise TypeError("candidate must be a string or mapping of strings")
    return dict(canonical_candidate(value))


def _callable_identity(function: Any) -> Mapping[str, str]:
    identity = {
        "module": getattr(function, "__module__", ""),
        "name": getattr(function, "__qualname__", function.__class__.__qualname__),
    }
    if not identity["module"] or identity["name"] == "<lambda>":
        raise TypeError("provide config.evaluator_identity for anonymous evaluators")
    return identity


def _case_identity(case: Any) -> Any:
    try:
        canonical_json(case, what="case")
        return case
    except TypeError:
        if hasattr(case, "__dict__"):
            return vars(case)
        raise TypeError("provide config.case_id for non-JSON cases") from None


def _record_generation(
    db: ThreadsDB,
    study_id: str,
    semantic_key: str,
    parents: tuple[Candidate, ...],
    candidate: Candidate,
    generation: int,
) -> None:
    if _has_message(db, study_id, semantic_key):
        return
    payload = {
        "generation": generation,
        "selected_parents": [dict(parent) for parent in parents],
        "candidate": candidate,
    }
    append_message(
        db,
        study_id,
        "system",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        extra={
            "eggopt_kind": "eggopt.native-gepa.generation-result.v1",
            "semantic_key": semantic_key,
        },
    )


class _NoReflectionDrive:
    def start(self, *_args, **_kwargs):
        raise RuntimeError("custom generation should not invoke reflection")

    resume = start


def _placeholder_reflection(generator: Any) -> Reflection:
    return Reflection(
        _NoReflectionDrive(),
        {"custom_generator": _callable_identity(generator)},
    )


def _sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("optimize_anything() cannot run inside an active asyncio loop")


__all__ = [
    "GenerateCandidate",
    "NativeGEPA",
    "NativeGEPAConfig",
    "NativeGEPAResult",
    "OptimizationPlan",
    "SelectParents",
    "optimize_anything",
    "plan_optimization",
]
