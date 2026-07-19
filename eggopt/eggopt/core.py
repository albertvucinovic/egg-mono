"""Dependency-free values and producer contracts for optimization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Generic, Iterator, Protocol, TypeAlias, TypeVar, runtime_checkable

InputT = TypeVar("InputT", contravariant=True)
OutputT = TypeVar("OutputT", covariant=True)
IntermediateT = TypeVar("IntermediateT")
StateT = TypeVar("StateT")


@runtime_checkable
class Producer(Protocol[InputT, OutputT]):
    """A synchronous semantic role that produces one typed value from another."""

    def produce(self, value: InputT) -> OutputT:
        """Produce a result for ``value``."""


@dataclass(frozen=True)
class FunctionProducer(Generic[InputT, OutputT]):
    """A deterministic Producer backed by a plain function."""

    function: Callable[[InputT], OutputT]

    def __post_init__(self) -> None:
        if not callable(self.function):
            raise TypeError("function must be callable")

    def produce(self, value: InputT) -> OutputT:
        return self.function(value)

    def then(
        self, next_producer: Producer[OutputT, IntermediateT]
    ) -> FunctionProducer[InputT, IntermediateT]:
        """Return a Producer that feeds this result to ``next_producer``."""

        if not isinstance(next_producer, Producer):
            raise TypeError("next_producer must implement Producer")
        return FunctionProducer(
            lambda value: next_producer.produce(self.produce(value))
        )


@dataclass(frozen=True)
class Candidate:
    """Arbitrary candidate text whose meaning is supplied by a domain."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")


@dataclass(frozen=True)
class Metric:
    """One named numeric measurement."""

    name: str
    value: float

    def __post_init__(self) -> None:
        _require_nonempty_string(self.name, "name")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError("value must be a float")
        value = float(self.value)
        if not isfinite(value):
            raise ValueError("value must be finite")
        object.__setattr__(self, "value", value)


JSONValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JSONValue", ...]
    | Mapping[str, "JSONValue"]
)


@dataclass(frozen=True, eq=False)
class _FrozenMapping(Mapping[str, JSONValue]):
    """A value-based immutable mapping suitable for pickle-backed caches."""

    _items: tuple[tuple[str, JSONValue], ...] = ()

    def __getitem__(self, key: str) -> JSONValue:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True)
class Feedback:
    """Human-readable feedback with optional opaque, immutable structured data."""

    text: str
    data: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        frozen = _freeze_mapping(self.data, "data")
        object.__setattr__(self, "data", frozen)


@dataclass(frozen=True)
class CaseEvidence:
    """Metrics and feedback retained for one domain-defined case."""

    case_id: str
    metrics: tuple[Metric, ...] = ()
    feedback: tuple[Feedback, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_string(self.case_id, "case_id")
        object.__setattr__(
            self, "metrics", _typed_tuple(self.metrics, Metric, "metrics")
        )
        object.__setattr__(
            self, "feedback", _typed_tuple(self.feedback, Feedback, "feedback")
        )


@dataclass(frozen=True)
class Observation:
    """All aggregate and per-case evidence observed for a candidate."""

    candidate: Candidate
    cases: tuple[CaseEvidence, ...] = ()
    metrics: tuple[Metric, ...] = ()
    feedback: tuple[Feedback, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be a Candidate")
        object.__setattr__(
            self, "cases", _typed_tuple(self.cases, CaseEvidence, "cases")
        )
        object.__setattr__(
            self, "metrics", _typed_tuple(self.metrics, Metric, "metrics")
        )
        object.__setattr__(
            self, "feedback", _typed_tuple(self.feedback, Feedback, "feedback")
        )


@dataclass(frozen=True)
class StrategyInput(Generic[StateT]):
    """Current unconstrained strategy state and newly available observations."""

    state: StateT
    observations: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observations",
            _typed_tuple(self.observations, Observation, "observations"),
        )


@dataclass(frozen=True)
class Proposal:
    """A domain-neutral request to produce a candidate from selected context."""

    parents: tuple[Candidate, ...] = ()
    instruction: str = ""
    evidence: tuple[CaseEvidence, ...] = ()
    feedback: tuple[Feedback, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "parents", _typed_tuple(self.parents, Candidate, "parents")
        )
        if not isinstance(self.instruction, str):
            raise TypeError("instruction must be a string")
        object.__setattr__(
            self, "evidence", _typed_tuple(self.evidence, CaseEvidence, "evidence")
        )
        object.__setattr__(
            self, "feedback", _typed_tuple(self.feedback, Feedback, "feedback")
        )


@dataclass(frozen=True)
class Advance(Generic[StateT]):
    """A transition to new state with one or more proposals to pursue."""

    state: StateT
    proposals: tuple[Proposal, ...]

    def __post_init__(self) -> None:
        proposals = _typed_tuple(self.proposals, Proposal, "proposals")
        if not proposals:
            raise ValueError("Advance requires at least one proposal")
        object.__setattr__(self, "proposals", proposals)


@dataclass(frozen=True)
class Stop(Generic[StateT]):
    """A terminal transition retaining final state and a nonempty reason."""

    state: StateT
    reason: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.reason, "reason")


StrategyDecision: TypeAlias = Advance[StateT] | Stop[StateT]


def _require_nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _typed_tuple(values: object, item_type: type, name: str) -> tuple:
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable") from exc
    if not all(isinstance(value, item_type) for value in normalized):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")
    return normalized


def _freeze_mapping(value: object, path: str) -> _FrozenMapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be a mapping")
    frozen: list[tuple[str, JSONValue]] = []
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{path} keys must be strings")
        frozen.append((key, _freeze_json(item, f"{path}[{key!r}]")))
    return _FrozenMapping(tuple(frozen))


def _freeze_json(value: object, path: str) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} must not contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value, path)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{path} contains a non-JSON-like value")
