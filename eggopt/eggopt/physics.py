"""Deterministic, domain-neutral explanatory strategy transition."""

from __future__ import annotations

from dataclasses import dataclass

from .core import (
    Advance,
    Observation,
    Producer,
    Proposal,
    Stop,
    StrategyDecision,
    StrategyInput,
    _typed_tuple,
)


@dataclass(frozen=True)
class PhysicsState:
    """Round budget state for a deterministic PhysicsStrategy transition."""

    round: int = 0
    max_rounds: int = 1

    def __post_init__(self) -> None:
        _require_nonnegative_integer(self.round, "round")
        _require_nonnegative_integer(self.max_rounds, "max_rounds")


@dataclass(frozen=True)
class PhysicsStrategy:
    """Choose a credible plan, discriminating experiment, or model revision."""

    select_hypotheses: Producer[
        tuple[Observation, ...], tuple[Observation, ...]
    ]
    propose_plan: Producer[tuple[Observation, ...], Proposal | None]
    propose_experiment: Producer[tuple[Observation, ...], Proposal]
    revise_hypotheses: Producer[
        tuple[Observation, ...], tuple[Proposal, ...]
    ]

    def __post_init__(self) -> None:
        for name in (
            "select_hypotheses",
            "propose_plan",
            "propose_experiment",
            "revise_hypotheses",
        ):
            if not isinstance(getattr(self, name), Producer):
                raise TypeError(f"{name} must implement Producer")

    def produce(
        self, value: StrategyInput[PhysicsState]
    ) -> StrategyDecision[PhysicsState]:
        """Produce one budgeted explanatory-strategy transition."""

        if not isinstance(value, StrategyInput):
            raise TypeError("value must be a StrategyInput")
        if not isinstance(value.state, PhysicsState):
            raise TypeError("state must be a PhysicsState")

        state = value.state
        if state.round >= state.max_rounds:
            return Stop(state=state, reason="round budget exhausted")

        hypotheses = _typed_tuple(
            self.select_hypotheses.produce(value.observations),
            Observation,
            "selected hypotheses",
        )
        if any(
            not _contains_identity(value.observations, hypothesis)
            for hypothesis in hypotheses
        ):
            raise ValueError(
                "selected hypotheses must come from input observations"
            )

        if not hypotheses:
            revisions = _typed_tuple(
                self.revise_hypotheses.produce(value.observations),
                Proposal,
                "hypothesis revisions",
            )
            if not revisions:
                return Stop(
                    state=state,
                    reason="no consistent hypotheses or revisions available",
                )
            return self._advance(state, revisions)

        plan = self.propose_plan.produce(hypotheses)
        if plan is not None:
            if not isinstance(plan, Proposal):
                raise TypeError("propose_plan must produce a Proposal or None")
            return self._advance(state, (plan,))

        experiment = self.propose_experiment.produce(hypotheses)
        if not isinstance(experiment, Proposal):
            raise TypeError("propose_experiment must produce a Proposal")
        return self._advance(state, (experiment,))

    @staticmethod
    def _advance(
        state: PhysicsState, proposals: tuple[Proposal, ...]
    ) -> Advance[PhysicsState]:
        next_state = PhysicsState(
            round=state.round + 1,
            max_rounds=state.max_rounds,
        )
        return Advance(state=next_state, proposals=proposals)


def _contains_identity(values: tuple[object, ...], selected: object) -> bool:
    return any(value is selected for value in values)


def _require_nonnegative_integer(value: object, name: str) -> None:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
