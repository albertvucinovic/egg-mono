"""Dependency-free request values for domain-owned case evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from .core import Candidate

CaseT = TypeVar("CaseT")

__all__ = ["CaseRequest", "EvaluationRequest"]


@dataclass(frozen=True)
class CaseRequest(Generic[CaseT]):
    """One candidate paired with one domain-owned case or indivisible batch."""

    candidate: Candidate
    case: CaseT

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be a Candidate")


@dataclass(frozen=True)
class EvaluationRequest(Generic[CaseT]):
    """A candidate and ordered domain-owned cases to evaluate."""

    candidate: Candidate
    cases: tuple[CaseT, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be a Candidate")
        try:
            cases = tuple(self.cases)
        except TypeError as exc:
            raise TypeError("cases must be an iterable") from exc
        object.__setattr__(self, "cases", cases)
