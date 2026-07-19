"""Deterministic strategy policies; effects are supplied by producers later."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .core import Candidate, Observation, Proposal, StrategyDecision, StrategyInput


def _ranked(observations: tuple[Observation, ...]) -> tuple[Observation, ...]:
    """Rank high scores first with stable, input-order tie breaking."""

    return tuple(
        sorted(observations, key=lambda observation: observation.score, reverse=True)
    )


@dataclass(frozen=True)
class GEPAState:
    generation: int = 0
    max_generations: int = 1
    proposals_per_generation: int = 1

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.max_generations < 0:
            raise ValueError("max_generations must be non-negative")
        if self.proposals_per_generation < 1:
            raise ValueError("proposals_per_generation must be positive")


@dataclass(frozen=True)
class GEPAStrategy:
    """Select the best observed candidate and request reflective mutations."""

    def advance(
        self, observations: tuple[Observation, ...], state: GEPAState
    ) -> StrategyDecision[GEPAState]:
        if state.generation >= state.max_generations:
            return StrategyDecision(
                state=state, stop=True, reason="generation budget exhausted"
            )
        if not observations:
            return StrategyDecision(state=state, stop=True, reason="no observations")

        parent = _ranked(observations)[0]
        feedback = parent.feedback or f"observed score: {parent.score:g}"
        prompt = (
            "Improve the candidate using this evaluation feedback. "
            f"Return only the revised candidate text.\n\n{feedback}"
        )
        proposals = tuple(
            Proposal(prompt=prompt, parent=parent.candidate)
            for _ in range(state.proposals_per_generation)
        )
        return StrategyDecision(
            state=replace(state, generation=state.generation + 1),
            proposals=proposals,
        )

    def produce(self, value: StrategyInput[GEPAState]) -> StrategyDecision[GEPAState]:
        return self.advance(value.observations, value.state)


@dataclass(frozen=True)
class PhysicsState:
    round: int = 0
    max_rounds: int = 1
    physical_root: Candidate | None = None
    agreement_tolerance: float = 0.0

    def __post_init__(self) -> None:
        if self.round < 0:
            raise ValueError("round must be non-negative")
        if self.max_rounds < 0:
            raise ValueError("max_rounds must be non-negative")
        if self.agreement_tolerance < 0:
            raise ValueError("agreement_tolerance must be non-negative")


@dataclass(frozen=True)
class PhysicsStrategy:
    """Retain the best theory and ask for a discriminating physical test."""

    def advance(
        self, observations: tuple[Observation, ...], state: PhysicsState
    ) -> StrategyDecision[PhysicsState]:
        if state.round >= state.max_rounds:
            return StrategyDecision(
                state=state, stop=True, reason="experiment budget exhausted"
            )
        if not observations:
            return StrategyDecision(state=state, stop=True, reason="no observations")

        ranked = _ranked(observations)
        physical_root = state.physical_root or Candidate("physical evidence")
        next_state = replace(
            state,
            round=state.round + 1,
            physical_root=physical_root,
        )
        if len(ranked) < 2:
            prompt = (
                "Propose a competing explanation and the cheapest observation that could "
                "distinguish it from the physical root."
            )
        elif abs(ranked[0].score - ranked[1].score) <= state.agreement_tolerance:
            prompt = (
                "Design the cheapest discriminating observation for these competing "
                f"explanations:\nA: {ranked[0].candidate.text}\nB: {ranked[1].candidate.text}"
            )
        else:
            prompt = (
                "Revise the weaker explanation to account for the strongest conflicting "
                f"evidence.\n\n{ranked[1].feedback or ranked[0].feedback}"
            )

        return StrategyDecision(
            state=next_state,
            proposals=(Proposal(prompt=prompt, parent=physical_root),),
        )

    def produce(
        self, value: StrategyInput[PhysicsState]
    ) -> StrategyDecision[PhysicsState]:
        return self.advance(value.observations, value.state)
