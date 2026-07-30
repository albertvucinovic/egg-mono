from __future__ import annotations

import asyncio
import inspect
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from operator import ge, gt
from pathlib import Path
from statistics import fmean
from typing import Any, Generic, Literal, TypeVar

from eggflow import FlowExecutor, Task
from eggthreads import ThreadsDB

from ..context import _evaluation_scope
from ..identity import canonical_json, canonical_value, digest_payload
from .evaluation import (
    _completed_evaluator_calls,
    _EvaluateCandidate,
    _feedback,
    _new_call_count,
)
from .mutation import Mutate, MutatorInput
from .runtime import Runtime

CaseT = TypeVar("CaseT")
OutputT = TypeVar("OutputT")
Candidate = Any
MinibatchAcceptance = Literal["strict_improvement", "improvement_or_equal"]
ProgressCallback = Callable[[Mapping[str, Any]], None]

_MINIBATCH_ACCEPTANCE = {
    "strict_improvement": gt,
    "improvement_or_equal": ge,
}


@dataclass(frozen=True)
class GEPAConfig:
    """The few controls that materially change a GEPA study."""

    max_evaluator_calls: int = 100
    max_candidates: int = 10
    mutation_minibatch_size: int = 3
    parents_per_candidate: int = 1
    minibatch_acceptance: MinibatchAcceptance = "strict_improvement"
    seed: int = 0
    run_dir: str | Path = ".eggopt/gepa"
    mutator: Any | None = None
    mutator_context_limit: int | None = None
    evaluator_identity: Any | None = None
    case_id: Callable[[Any], Any] | None = field(
        default=None, repr=False, compare=False
    )
    max_concurrent_evaluations: int | None = 1
    evaluator_context_limit: int | None = None
    progress: ProgressCallback | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "max_evaluator_calls",
            "max_candidates",
            "mutation_minibatch_size",
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
        if self.evaluator_context_limit is not None and (
            isinstance(self.evaluator_context_limit, bool)
            or not isinstance(self.evaluator_context_limit, int)
            or self.evaluator_context_limit < 1
        ):
            raise ValueError("evaluator_context_limit must be positive or None")
        if self.mutator_context_limit is not None and (
            isinstance(self.mutator_context_limit, bool)
            or not isinstance(self.mutator_context_limit, int)
            or self.mutator_context_limit < 1
        ):
            raise ValueError("mutator_context_limit must be positive or None")
        if self.minibatch_acceptance not in _MINIBATCH_ACCEPTANCE:
            raise ValueError(
                "minibatch_acceptance must be 'strict_improvement' or "
                "'improvement_or_equal'"
            )
        if self.evaluator_identity is not None:
            canonical_json(self.evaluator_identity, what="evaluator identity")
        if self.mutator is not None and not (
            callable(self.mutator) or isinstance(self.mutator, Task)
        ):
            raise TypeError("mutator must be callable or an Eggflow Task")
        if self.progress is not None and not callable(self.progress):
            raise TypeError("progress must be callable or None")


@dataclass(frozen=True)
class GEPAResult(Generic[OutputT]):
    """The winning candidate and the inspectable search that produced it.

    ``per_validation_case_best_candidate_indices`` preserves the valset order.
    Each value contains every tied-best zero-based index into ``candidates`` for
    that validation case.
    """

    candidates: tuple[Candidate, ...]
    scores: tuple[float, ...]
    case_scores: tuple[tuple[float, ...], ...]
    parents: tuple[tuple[int, ...], ...]
    outputs: tuple[tuple[OutputT | None, ...], ...]
    feedback: tuple[tuple[Any, ...], ...]
    evaluator_calls: int
    generated_candidates: int
    best_index: int
    per_validation_case_best_candidate_indices: tuple[tuple[Any, tuple[int, ...]], ...]

    @property
    def best_candidate(self) -> Candidate:
        return _candidate(self.candidates[self.best_index])

    @property
    def best_score(self) -> float:
        return self.scores[self.best_index]

    @property
    def metric_calls(self) -> int:
        """Return the number of evaluator calls used."""

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
    mutation_minibatch_size: int = 3,
    parents_per_candidate: int = 1,
    completed_candidates: int = 0,
    completed_evaluator_calls: int = 0,
) -> OptimizationPlan:
    """Estimate total and incremental work without opening a study."""

    for name, value, minimum in (
        ("dataset_size", dataset_size, 1),
        ("max_candidates", max_candidates, 1),
        ("max_evaluator_calls", max_evaluator_calls, 1),
        ("mutation_minibatch_size", mutation_minibatch_size, 1),
        ("parents_per_candidate", parents_per_candidate, 1),
        ("completed_candidates", completed_candidates, 0),
        ("completed_evaluator_calls", completed_evaluator_calls, 0),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            qualifier = "positive" if minimum else "non-negative"
            raise ValueError(f"{name} must be a {qualifier} integer")
    validation = dataset_size if valset_size is None else valset_size
    if (
        isinstance(validation, bool)
        or not isinstance(validation, int)
        or validation < 1
    ):
        raise ValueError("valset_size must be a positive integer")
    batch = min(mutation_minibatch_size, dataset_size)

    # Seed: one full validation. Each proposal evaluates every selected parent
    # on a reflection minibatch, then the child on that minibatch and, when
    # accepted, on the full validation set. Validation and reflection are
    # deliberately different cache/thread scopes, even when dataset == valset.
    reflection = batch * (parents_per_candidate + 1)
    generated = min(
        max_candidates,
        max(
            0,
            (max_evaluator_calls - validation) // max(1, reflection + validation),
        ),
    )
    total_calls = min(
        max_evaluator_calls, validation + generated * (reflection + validation)
    )
    return OptimizationPlan(
        max_candidates=max_candidates,
        max_evaluator_calls=max_evaluator_calls,
        generated_candidates=generated,
        full_evaluations=1 + generated,
        minibatch_evaluations=generated * (parents_per_candidate + 1),
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
            "eggopt.gepa.select-parents.v1",
            {
                "scores": self.scores,
                "count": self.count,
                "seed": self.seed,
                "generation": self.generation,
            },
        )

    def run(self) -> tuple[int, ...]:
        frequencies = _case_front_frequencies(self.scores)
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
    """Invoke one domain Mutator inside its durable mutation workspace."""

    runtime_key: str
    study_id: str
    mutation_id: str
    workspace: str
    mutator: Any = field(repr=False, compare=False)
    context: MutatorInput
    context_limit: int | None = None

    def get_cache_key(self) -> str:
        return self._mutation().get_cache_key()

    def run(self):
        scope = {
            "evaluation_thread_id": self.mutation_id,
            "outer_context": self.workspace,
            "inner_context": self.workspace,
            "_runtime_key": self.runtime_key,
            "_evaluation_key": self.get_cache_key(),
            "_context_limit": self.context_limit,
        }
        with _evaluation_scope(scope):
            return (yield self._mutation())

    def _mutation(self) -> Mutate:
        return Mutate(self.mutator, self.context)


@dataclass
class _NativeSearch(Task, Generic[CaseT, OutputT]):
    cacheable = False

    flow: FlowExecutor = field(repr=False, compare=False)
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    validation_id: str
    mutation_id: str
    reflection_id: str
    seed_candidate: Candidate
    dataset: list[CaseT] = field(repr=False, compare=False)
    dataset_ids: tuple[Any, ...]
    valset: list[CaseT] = field(repr=False, compare=False)
    valset_ids: tuple[Any, ...]
    evaluator: Any = field(repr=False, compare=False)
    evaluator_identity: Any
    objective: str
    config: GEPAConfig = field(repr=False, compare=False)
    runtime_key: str

    def run(self):
        seed = _candidate(self.seed_candidate)
        seed_needed = _new_call_count(
            self.flow,
            seed,
            self.valset,
            self.valset_ids,
            self.evaluator,
            self.evaluator_identity,
            "full",
        )
        if (
            _completed_evaluator_calls(self.flow) + seed_needed
            > self.config.max_evaluator_calls
        ):
            raise ValueError(
                "max_evaluator_calls must cover the seed's full valset evaluation"
            )
        first = yield self._evaluate(
            seed,
            self.valset,
            self.valset_ids,
            stage="full",
            label="Candidate 1 Evaluation",
            candidate_number=1,
            evaluation_role="candidate_validation",
        )
        candidates = [seed]
        case_scores = [first.scores]
        outputs = [first.outputs]
        feedback = [first.feedback]
        parents: list[tuple[int, ...]] = [()]
        candidate_generations: list[int | None] = [None]
        calls = _completed_evaluator_calls(self.flow)
        generated = 0
        last_candidate_result = None

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
                self.config.mutation_minibatch_size,
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
                    "minibatch",
                )
                if calls + needed > self.config.max_evaluator_calls:
                    return _result(
                        candidates,
                        case_scores,
                        self.valset_ids,
                        parents,
                        outputs,
                        feedback,
                        calls,
                        generated,
                    )
                evaluated = yield self._evaluate(
                    candidates[parent_id],
                    batch,
                    batch_ids,
                    stage="minibatch",
                    label=f"Candidate {parent_id + 1} Reflection for Proposal {generation + 1}",
                    proposal_number=generation + 1,
                    candidate_number=parent_id + 1,
                    evaluation_role="parent_reflection",
                )
                calls = _completed_evaluator_calls(self.flow)
                parent_evaluations.append(evaluated)

            validation_scores = _full_validation_scores(
                case_scores, candidate_generations
            )
            evidence = tuple(
                _generation_evidence(
                    parent_id,
                    result,
                    batch_ids,
                    _selection_reason(parent_id, case_scores),
                )
                for parent_id, result in zip(
                    parent_ids, parent_evaluations, strict=True
                )
            )
            selected = tuple(candidates[index] for index in parent_ids)
            generation_task = GenerateCandidate(
                self.runtime_key,
                self.study_id,
                self.mutation_id,
                str(Path(self.config.run_dir).resolve() / "workspaces" / "mutation"),
                self.config.mutator,
                MutatorInput(
                    selected,
                    evidence,
                    self.objective,
                    generation,
                    validation_scores,
                    last_candidate_result,
                ),
                self.config.mutator_context_limit,
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
                "minibatch",
            )
            if calls + child_needed > self.config.max_evaluator_calls:
                return _result(
                    candidates,
                    case_scores,
                    self.valset_ids,
                    parents,
                    outputs,
                    feedback,
                    calls,
                    generated,
                )
            child_batch = yield self._evaluate(
                child,
                batch,
                batch_ids,
                stage="minibatch",
                label=f"Proposal {generated} Minibatch",
                proposal_number=generated,
                evaluation_role="proposal_minibatch",
            )
            calls = _completed_evaluator_calls(self.flow)
            # Multiple selected parents may specialize on different cases;
            # compare the child with the strongest per-case parent envelope.
            accepted = _accept_minibatch(
                child_batch.scores,
                parent_evaluations,
                self.config.minibatch_acceptance,
            )
            last_candidate_result = _minibatch_candidate_result(
                generation,
                child_batch.scores,
                parent_evaluations,
                self.config.minibatch_acceptance,
                accepted,
            )
            if not accepted:
                continue

            full_needed = _new_call_count(
                self.flow,
                child,
                self.valset,
                self.valset_ids,
                self.evaluator,
                self.evaluator_identity,
                "full",
            )
            if calls + full_needed > self.config.max_evaluator_calls:
                last_candidate_result = {
                    **last_candidate_result,
                    "outcome": "full_validation_not_run_evaluator_budget",
                }
                continue
            candidate_number = len(candidates) + 1
            full = yield self._evaluate(
                child,
                self.valset,
                self.valset_ids,
                stage="full",
                label=f"Proposal {generated} → Candidate {candidate_number} Validation",
                proposal_number=generated,
                candidate_number=candidate_number,
                evaluation_role="candidate_validation",
            )
            calls = _completed_evaluator_calls(self.flow)
            candidates.append(child)
            case_scores.append(full.scores)
            outputs.append(full.outputs)
            feedback.append(full.feedback)
            parents.append(parent_ids)
            candidate_generations.append(generation + 1)
            last_candidate_result = {
                **last_candidate_result,
                "outcome": "full_validation_completed_and_added",
                "full_validation": {
                    "candidate_index": len(candidates) - 1,
                    "candidate_number": len(candidates),
                    "aggregate_score": fmean(full.scores),
                    "case_count": len(full.scores),
                },
            }

        return _result(
            candidates,
            case_scores,
            self.valset_ids,
            parents,
            outputs,
            feedback,
            calls,
            generated,
        )

    def _evaluate(
        self,
        candidate,
        cases,
        case_ids,
        *,
        stage,
        label,
        proposal_number=None,
        candidate_number=None,
        evaluation_role,
    ):
        parent_id = self.validation_id if stage == "full" else self.reflection_id
        return _EvaluateCandidate(
            self.flow,
            self.threads,
            parent_id,
            Path(self.config.run_dir).resolve(),
            candidate,
            cases,
            case_ids,
            self.evaluator,
            self.evaluator_identity,
            self.config.max_concurrent_evaluations,
            self.config.evaluator_context_limit,
            self.config.progress,
            stage,
            label,
            proposal_number,
            candidate_number,
            evaluation_role,
        )


class GEPA(Generic[CaseT, OutputT]):
    """The GEPA optimizer."""

    def __init__(
        self,
        *,
        evaluator: Any | None = None,
        metric: Any | None = None,
        objective: str = "Improve the candidate.",
        config: GEPAConfig | None = None,
        **legacy: Any,
    ) -> None:
        self.evaluator = evaluator or metric
        if self.evaluator is None:
            raise TypeError("evaluator is required")
        self.objective = objective
        if config is None:
            config = GEPAConfig(
                max_candidates=int(legacy.pop("generations", 3)),
                max_evaluator_calls=legacy.pop("max_metric_calls", 100),
                run_dir=legacy.pop("run_dir", ".eggopt/gepa"),
                mutator=legacy.pop("mutator", None),
                evaluator_identity=legacy.pop("metric_identity", None),
                case_id=legacy.pop("example_id", None),
                max_concurrent_evaluations=legacy.pop("max_concurrent_evaluations", 1),
            )
        if legacy:
            raise TypeError(f"unknown GEPA options: {sorted(legacy)}")
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
    seed_candidate: Any,
    *,
    evaluator: Any,
    dataset: Sequence[CaseT],
    valset: Sequence[CaseT] | None = None,
    objective: str,
    config: GEPAConfig | None = None,
) -> GEPAResult[Any]:
    """Optimize an opaque finite-JSON candidate with case-wise Pareto search."""

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
    config = config or GEPAConfig()
    if config.mutator is None:
        raise TypeError("config.mutator is required")

    case_id = config.case_id or _case_identity
    dataset_ids = _case_ids(data, case_id, "dataset")
    validation_ids = _case_ids(validation, case_id, "valset")
    evaluator_identity = config.evaluator_identity or _callable_identity(evaluator)
    with Runtime.open(config.run_dir) as runtime:
        return _sync(
            runtime.flow.run(
                _NativeSearch(
                    runtime.flow,
                    runtime.threads,
                    runtime.study_id,
                    runtime.validation_id,
                    runtime.mutation_id,
                    runtime.reflection_id,
                    _candidate(seed_candidate),
                    data,
                    dataset_ids,
                    validation,
                    validation_ids,
                    evaluator,
                    evaluator_identity,
                    objective.strip(),
                    config,
                    runtime.runtime_key,
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
    parent_total = sum(_parent_envelope_scores(parent_evaluations, len(child_scores)))
    child_total = sum(child_scores)
    return _MINIBATCH_ACCEPTANCE[criterion](child_total, parent_total)


def _parent_envelope_scores(parent_evaluations, case_count):
    return tuple(
        max(item.scores[case] for item in parent_evaluations)
        for case in range(case_count)
    )


def _minibatch_candidate_result(
    generation,
    child_scores,
    parent_evaluations,
    acceptance_policy,
    accepted,
):
    parent_scores = _parent_envelope_scores(parent_evaluations, len(child_scores))
    return {
        "mutation_generation": generation + 1,
        "outcome": (
            "advanced_to_full_validation" if accepted else "rejected_on_minibatch"
        ),
        "minibatch": {
            "aggregate_score": fmean(child_scores),
            "case_count": len(child_scores),
            "parent_envelope_aggregate_score": fmean(parent_scores),
            "acceptance_policy": acceptance_policy,
            "accepted": accepted,
        },
        "full_validation": None,
    }


def _case_fronts(scores: Sequence[Sequence[float]]) -> tuple[tuple[int, ...], ...]:
    if not scores:
        return ()
    return tuple(
        tuple(index for index, row in enumerate(scores) if row[case] == best)
        for case in range(len(scores[0]))
        for best in [max(row[case] for row in scores)]
    )


def _case_front_frequencies(scores: Sequence[Sequence[float]]) -> Counter[int]:
    return Counter(index for front in _case_fronts(scores) for index in front)


def _result(
    candidates,
    case_scores,
    validation_case_ids,
    parents,
    outputs,
    feedback,
    calls,
    generated,
):
    aggregates = tuple(fmean(scores) for scores in case_scores)
    best = max(range(len(aggregates)), key=aggregates.__getitem__)
    return GEPAResult(
        candidates=tuple(_candidate(candidate) for candidate in candidates),
        scores=aggregates,
        case_scores=tuple(tuple(scores) for scores in case_scores),
        parents=tuple(parents),
        outputs=tuple(outputs),
        feedback=tuple(feedback),
        evaluator_calls=calls,
        generated_candidates=generated,
        best_index=best,
        per_validation_case_best_candidate_indices=tuple(
            zip(validation_case_ids, _case_fronts(case_scores), strict=True)
        ),
    )


def _full_validation_scores(case_scores, candidate_generations):
    return tuple(
        {
            "candidate_index": index,
            "candidate_number": index + 1,
            "mutation_generation": generation,
            "aggregate_score": fmean(scores),
            "case_count": len(scores),
        }
        for index, (scores, generation) in enumerate(
            zip(case_scores, candidate_generations, strict=True)
        )
    )


def _selection_reason(parent_id, case_scores):
    frequency = _case_front_frequencies(case_scores)[parent_id]
    return (
        "Selected from the full-validation Pareto pool by deterministic weighted "
        f"sampling; Candidate {parent_id + 1} was best or tied-best on "
        f"{frequency} of {len(case_scores[0])} validation cases."
    )


def _generation_evidence(parent_id, evaluation, case_ids, selection_reason):
    return {
        "parent_index": parent_id,
        "selection_reason": selection_reason,
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


def _candidate(value: Any) -> Candidate:
    return canonical_value(value, what="candidate")


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


def _case_ids(cases: Sequence[Any], identify: Callable[[Any], Any], role: str):
    identities = tuple(identify(case) for case in cases)
    canonical = tuple(
        canonical_json(identity, what=f"{role} case identity")
        for identity in identities
    )
    if len(set(canonical)) != len(canonical):
        raise ValueError(f"{role} case identities must be unique")
    return identities


def _sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("optimize_anything() cannot run inside an active asyncio loop")


__all__ = [
    "GEPA",
    "GEPAConfig",
    "GEPAResult",
    "GenerateCandidate",
    "OptimizationPlan",
    "SelectParents",
    "optimize_anything",
    "plan_optimization",
]
