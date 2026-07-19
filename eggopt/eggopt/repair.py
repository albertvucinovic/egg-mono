"""Dependency-free values for cumulative same-instance repair composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

__all__ = [
    "Accepted",
    "Inspection",
    "ItemFailure",
    "NeedsRepair",
    "RepairFeedback",
    "RepairInput",
]


@dataclass(frozen=True)
class RepairFeedback:
    """Concrete sanitized feedback for one expected invalid output."""

    text: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.text, "text")


@dataclass(frozen=True)
class Accepted(Generic[OutputT]):
    """Successful inspection carrying its typed accepted/normalized value."""

    value: OutputT


@dataclass(frozen=True)
class NeedsRepair:
    """Expected invalid production that should re-enter the same inner Producer."""

    feedback: RepairFeedback

    def __post_init__(self) -> None:
        if not isinstance(self.feedback, RepairFeedback):
            raise TypeError("feedback must be RepairFeedback")


Inspection: TypeAlias = Accepted[OutputT] | NeedsRepair


@dataclass(frozen=True)
class RepairInput(Generic[InputT]):
    """Original input plus cumulative feedback for the next repair attempt."""

    original: InputT
    feedback: tuple[RepairFeedback, ...] = ()

    def __post_init__(self) -> None:
        feedback = tuple(self.feedback)
        if not all(isinstance(item, RepairFeedback) for item in feedback):
            raise TypeError("feedback must contain only RepairFeedback values")
        object.__setattr__(self, "feedback", feedback)


@dataclass(frozen=True)
class ItemFailure:
    """Typed per-item terminal outcome that does not abort a containing batch."""

    kind: str
    reason: str
    attempts: int

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.kind, "kind")
        _validate_nonempty_string(self.reason, "reason")
        if not isinstance(self.attempts, int):
            raise TypeError("attempts must be an integer")
        if self.attempts < 0:
            raise ValueError("attempts must be nonnegative")


def _validate_nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
