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
        _validate_gepa_configuration(
            self.select_parents,
            self.select_evidence,
            self.instruction,
            self.proposals_per_parent,
        )

    def produce(
        self, value: StrategyInput[GEPAState]
    ) -> StrategyDecision[GEPAState]:
        """Produce one budgeted GEPA transition."""

        stop = _validate_gepa_input(value)
        if stop is not None:
            return stop
        parents = _typed_tuple(
            self.select_parents.produce(value.observations),
            Observation,
            "selected parents",
        )
        if not parents:
            return Stop(
                state=value.state,
                reason="parent selector chose no parents",
            )
        evidence = tuple(
            _typed_tuple(
                self.select_evidence.produce(parent),
                CaseEvidence,
                "selected evidence",
            )
            for parent in parents
        )
        return build_gepa_decision(
            value,
            parents,
            evidence,
            instruction=self.instruction,
            proposals_per_parent=self.proposals_per_parent,
        )


def build_gepa_decision(
    value: StrategyInput[GEPAState],
    parents: tuple[Observation, ...],
    evidence_by_parent: tuple[tuple[CaseEvidence, ...], ...],
    *,
    instruction: str = _DEFAULT_INSTRUCTION,
    proposals_per_parent: int = 1,
) -> StrategyDecision[GEPAState]:
    """Build one GEPA decision from already selected parents and evidence."""

    stop = _validate_gepa_input(value)
    if stop is not None:
        return stop
    parents = _typed_tuple(parents, Observation, "selected parents")
    if not parents:
        return Stop(
            state=value.state,
            reason="parent selector chose no parents",
        )
    try:
        evidence_by_parent = tuple(evidence_by_parent)
    except TypeError as exc:
        raise TypeError("evidence_by_parent must be an iterable") from exc
    if len(evidence_by_parent) != len(parents):
        raise ValueError("evidence_by_parent must align with selected parents")
    _validate_gepa_options(instruction, proposals_per_parent)

    proposals: list[Proposal] = []
    for parent, selected_evidence in zip(parents, evidence_by_parent):
        if not _contains_identity(value.observations, parent):
            raise ValueError("selected parents must come from input observations")
        evidence = _typed_tuple(
            selected_evidence,
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
            instruction=instruction,
            evidence=evidence,
            feedback=parent.feedback,
        )
        proposals.extend(proposal for _ in range(proposals_per_parent))

    next_state = GEPAState(
        generation=value.state.generation + 1,
        max_generations=value.state.max_generations,
    )
    return Advance(state=next_state, proposals=tuple(proposals))


def _validate_gepa_input(
    value: StrategyInput[GEPAState],
) -> Stop[GEPAState] | None:
    if not isinstance(value, StrategyInput):
        raise TypeError("value must be a StrategyInput")
    if not isinstance(value.state, GEPAState):
        raise TypeError("state must be a GEPAState")
    if value.state.generation >= value.state.max_generations:
        return Stop(state=value.state, reason="generation budget exhausted")
    if not value.observations:
        return Stop(state=value.state, reason="no observations available")
    return None


def _validate_gepa_configuration(
    select_parents: object,
    select_evidence: object,
    instruction: object,
    proposals_per_parent: object,
) -> None:
    if not isinstance(select_parents, Producer):
        raise TypeError("select_parents must implement Producer")
    if not isinstance(select_evidence, Producer):
        raise TypeError("select_evidence must implement Producer")
    _validate_gepa_options(instruction, proposals_per_parent)


def _validate_gepa_options(
    instruction: object, proposals_per_parent: object
) -> None:
    if not isinstance(instruction, str):
        raise TypeError("instruction must be a string")
    if not isinstance(proposals_per_parent, int):
        raise TypeError("proposals_per_parent must be an integer")
    if proposals_per_parent <= 0:
        raise ValueError("proposals_per_parent must be positive")


def _contains_identity(values: tuple[object, ...], selected: object) -> bool:
    return any(value is selected for value in values)


def _require_nonnegative_integer(value: object, name: str) -> None:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
