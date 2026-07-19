"""Deterministic, domain-neutral GEPA strategy transition."""

from __future__ import annotations

from dataclasses import dataclass

from .core import (
    Advance,
    CaseEvidence,
    Observation,
    Producer,
    Proposal,
    Stop,
    StrategyDecision,
    StrategyInput,
    _typed_tuple,
)

_DEFAULT_INSTRUCTION = "Revise the candidate using the selected evidence."


@dataclass(frozen=True)
class GEPAState:
    """Generation budget state for a deterministic GEPA transition."""

    generation: int = 0
    max_generations: int = 1

    def __post_init__(self) -> None:
        _require_nonnegative_integer(self.generation, "generation")
        _require_nonnegative_integer(self.max_generations, "max_generations")


@dataclass(frozen=True)
class GEPAStrategy:
    """Build reflective proposals using injected parent and evidence selection."""

    select_parents: Producer[
        tuple[Observation, ...], tuple[Observation, ...]
    ]
    select_evidence: Producer[Observation, tuple[CaseEvidence, ...]]
    instruction: str = _DEFAULT_INSTRUCTION
    proposals_per_parent: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.select_parents, Producer):
            raise TypeError("select_parents must implement Producer")
        if not isinstance(self.select_evidence, Producer):
            raise TypeError("select_evidence must implement Producer")
        if not isinstance(self.instruction, str):
            raise TypeError("instruction must be a string")
        if not isinstance(self.proposals_per_parent, int):
            raise TypeError("proposals_per_parent must be an integer")
        if self.proposals_per_parent <= 0:
            raise ValueError("proposals_per_parent must be positive")

    def produce(
        self, value: StrategyInput[GEPAState]
    ) -> StrategyDecision[GEPAState]:
        """Produce one budgeted GEPA transition."""

        if not isinstance(value, StrategyInput):
            raise TypeError("value must be a StrategyInput")
        if not isinstance(value.state, GEPAState):
            raise TypeError("state must be a GEPAState")

        state = value.state
        if state.generation >= state.max_generations:
            return Stop(state=state, reason="generation budget exhausted")
        if not value.observations:
            return Stop(state=state, reason="no observations available")

        parents = _typed_tuple(
            self.select_parents.produce(value.observations),
            Observation,
            "selected parents",
        )
        if not parents:
            return Stop(state=state, reason="parent selector chose no parents")

        proposals: list[Proposal] = []
        for parent in parents:
            if not _contains_identity(value.observations, parent):
                raise ValueError(
                    "selected parents must come from input observations"
                )
            evidence = _typed_tuple(
                self.select_evidence.produce(parent),
                CaseEvidence,
                "selected evidence",
            )
            if any(
                not _contains_identity(parent.cases, case) for case in evidence
            ):
                raise ValueError(
                    "selected evidence must come from its parent observation"
                )
            proposal = Proposal(
                parents=(parent.candidate,),
                instruction=self.instruction,
                evidence=evidence,
                feedback=parent.feedback,
            )
            proposals.extend(proposal for _ in range(self.proposals_per_parent))

        next_state = GEPAState(
            generation=state.generation + 1,
            max_generations=state.max_generations,
        )
        return Advance(state=next_state, proposals=tuple(proposals))


def _contains_identity(values: tuple[object, ...], selected: object) -> bool:
    return any(value is selected for value in values)


def _require_nonnegative_integer(value: object, name: str) -> None:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
