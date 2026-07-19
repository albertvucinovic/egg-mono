"""Dependency-free inputs and cached result values for strategy runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from .core import Candidate, CaseEvidence, Observation, Proposal
from .repair import ItemFailure

StateT = TypeVar("StateT")
CaseT = TypeVar("CaseT")
ValueT = TypeVar("ValueT")

__all__ = [
    "OperationContext",
    "OperationInput",
    "OperationResult",
    "ProposalResult",
    "StepResult",
    "StrategyRunInput",
    "StrategyRunResult",
]


@dataclass(frozen=True)
class StrategyRunInput(Generic[StateT, CaseT]):
    """Seed and domain-owned inputs for one complete strategy run."""

    state: StateT
    seed: Candidate
    cases: tuple[CaseT, ...] = ()
    max_steps: int = 0
    max_concurrent_cases: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.seed, Candidate):
            raise TypeError("seed must be a Candidate")
        try:
            cases = tuple(self.cases)
        except TypeError as exc:
            raise TypeError("cases must be an iterable") from exc
        object.__setattr__(self, "cases", cases)
        _require_nonnegative_integer(self.max_steps, "max_steps")
        _require_positive_integer(
            self.max_concurrent_cases, "max_concurrent_cases"
        )


@dataclass(frozen=True)
class OperationContext:
    """Explicit physical context for one authoritative domain operation."""

    thread_id: str
    semantic_name: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.thread_id, "thread_id")
        _require_nonempty_string(self.semantic_name, "semantic_name")


@dataclass(frozen=True)
class OperationInput(Generic[ValueT]):
    """A semantic role input paired with its authoritative operation context."""

    context: OperationContext
    value: ValueT

    def __post_init__(self) -> None:
        if not isinstance(self.context, OperationContext):
            raise TypeError("context must be an OperationContext")


@dataclass(frozen=True)
class OperationResult(Generic[ValueT]):
    """One authoritative domain operation value and its physical thread."""

    thread_id: str
    value: ValueT

    def __post_init__(self) -> None:
        _require_nonempty_string(self.thread_id, "thread_id")


@dataclass(frozen=True)
class ProposalResult:
    """Authoritative production and evaluation results for one proposal."""

    proposal_id: str
    proposal_thread_id: str
    evaluation_thread_id: str | None
    proposal: Proposal
    production: OperationResult[Candidate | ItemFailure]
    cases: tuple[OperationResult[CaseEvidence | ItemFailure], ...]
    aggregation: OperationResult[Observation] | None

    def __post_init__(self) -> None:
        _require_nonempty_string(self.proposal_id, "proposal_id")
        _require_nonempty_string(self.proposal_thread_id, "proposal_thread_id")
        if self.evaluation_thread_id is not None:
            _require_nonempty_string(
                self.evaluation_thread_id, "evaluation_thread_id"
            )
        if not isinstance(self.proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        if not isinstance(self.production, OperationResult):
            raise TypeError("production must be an OperationResult")
        if not isinstance(self.production.value, (Candidate, ItemFailure)):
            raise TypeError(
                "production value must be a Candidate or ItemFailure"
            )
        object.__setattr__(
            self,
            "cases",
            _typed_tuple(self.cases, OperationResult, "cases"),
        )
        for case in self.cases:
            if not isinstance(case.value, (CaseEvidence, ItemFailure)):
                raise TypeError(
                    "case value must be CaseEvidence or ItemFailure"
                )
        if isinstance(self.production.value, ItemFailure):
            if self.evaluation_thread_id is not None or self.cases:
                raise ValueError(
                    "failed production cannot have evaluation results"
                )
        elif self.evaluation_thread_id is None:
            raise ValueError(
                "successful production requires an evaluation thread"
            )
        if self.aggregation is not None:
            if not isinstance(self.aggregation, OperationResult):
                raise TypeError(
                    "aggregation must be an OperationResult or None"
                )
            if not isinstance(self.aggregation.value, Observation):
                raise TypeError("aggregation value must be an Observation")


@dataclass(frozen=True)
class StepResult(Generic[StateT]):
    """One serial strategy step and its ordered proposal results."""

    step_id: str
    step_thread_id: str
    transition: OperationResult[object] | None
    state: StateT
    proposals: tuple[ProposalResult, ...]
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty_string(self.step_id, "step_id")
        _require_nonempty_string(self.step_thread_id, "step_thread_id")
        if self.transition is not None and not isinstance(
            self.transition, OperationResult
        ):
            raise TypeError("transition must be an OperationResult or None")
        object.__setattr__(
            self,
            "proposals",
            _typed_tuple(self.proposals, ProposalResult, "proposals"),
        )
        if self.stop_reason is not None and not isinstance(self.stop_reason, str):
            raise TypeError("stop_reason must be a string or None")


@dataclass(frozen=True)
class StrategyRunResult(Generic[StateT]):
    """Cached authoritative hierarchy references and values for one run."""

    study_thread_id: str
    strategy_thread_id: str
    run_setup_thread_id: str
    steps: tuple[StepResult[StateT], ...]
    final_state: StateT

    def __post_init__(self) -> None:
        _require_nonempty_string(self.study_thread_id, "study_thread_id")
        _require_nonempty_string(self.strategy_thread_id, "strategy_thread_id")
        _require_nonempty_string(self.run_setup_thread_id, "run_setup_thread_id")
        steps = _typed_tuple(self.steps, StepResult, "steps")
        if not steps or steps[0].step_id != "S000":
            raise ValueError("steps must start with S000")
        object.__setattr__(self, "steps", steps)


def _typed_tuple(values: object, item_type: type, name: str) -> tuple:
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable") from exc
    if not all(isinstance(value, item_type) for value in normalized):
        raise TypeError(f"{name} must contain only {item_type.__name__}")
    return normalized


def _require_nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_nonnegative_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _require_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")
