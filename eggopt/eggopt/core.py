"""Small, dependency-free contracts shared by optimization strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Generic, Mapping, Protocol, TypeVar, runtime_checkable

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
MiddleT = TypeVar("MiddleT")
StateT = TypeVar("StateT")


@dataclass(frozen=True)
class Candidate:
    """A domain-neutral optimization candidate."""

    text: str


@runtime_checkable
class Producer(Protocol[InputT, OutputT]):
    """Something that produces one typed value from another."""

    def produce(self, value: InputT) -> OutputT:
        ...


@dataclass(frozen=True)
class FunctionProducer(Generic[InputT, OutputT]):
    """Adapt a deterministic function to the Producer contract."""

    function: Callable[[InputT], OutputT]

    def produce(self, value: InputT) -> OutputT:
        return self.function(value)

    def then(
        self, next_producer: Producer[OutputT, MiddleT]
    ) -> FunctionProducer[InputT, MiddleT]:
        """Compose producers without introducing an orchestration dependency."""

        def composed(value: InputT) -> MiddleT:
            return next_producer.produce(self.produce(value))

        return FunctionProducer(composed)


@dataclass(frozen=True)
class Observation:
    """Evaluator evidence about one candidate."""

    candidate: Candidate
    score: float
    feedback: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True)
class Proposal:
    """A requested candidate production, optionally rooted at a parent."""

    prompt: str
    parent: Candidate | None = None


@dataclass(frozen=True)
class StrategyDecision(Generic[StateT]):
    """The complete result of one strategy advance transition."""

    state: StateT
    proposals: tuple[Proposal, ...] = ()
    stop: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.stop and self.proposals:
            raise ValueError("a stopping decision cannot contain proposals")
        if self.stop and self.reason is None:
            raise ValueError("a stopping decision must include a reason")
        if not self.stop and not self.proposals:
            raise ValueError("an advancing decision must contain proposals")


@dataclass(frozen=True)
class StrategyInput(Generic[StateT]):
    """Inputs supplied to one strategy transition."""

    observations: tuple[Observation, ...]
    state: StateT


@runtime_checkable
class Strategy(Producer[StrategyInput[StateT], StrategyDecision[StateT]], Protocol[StateT]):
    """A producer with one observations + state -> decision transition."""

    def advance(
        self, observations: tuple[Observation, ...], state: StateT
    ) -> StrategyDecision[StateT]:
        ...

    def produce(self, value: StrategyInput[StateT]) -> StrategyDecision[StateT]:
        ...
