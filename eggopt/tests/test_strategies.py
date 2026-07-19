from eggopt import (
    Candidate,
    GEPAState,
    GEPAStrategy,
    Observation,
    PhysicsState,
    PhysicsStrategy,
    Proposal,
    StrategyDecision,
    StrategyInput,
)


def observation(text: str, score: float, feedback: str = "") -> Observation:
    return Observation(Candidate(text), score, feedback)


def test_gepa_selects_best_parent_and_advances_generation() -> None:
    strategy = GEPAStrategy()
    state = GEPAState(max_generations=2, proposals_per_generation=2)

    decision = strategy.produce(
        StrategyInput(
            observations=(
                observation("weak", 0.2, "missed an edge case"),
                observation("strong", 0.9, "make the answer shorter"),
            ),
            state=state,
        )
    )

    assert decision.state == GEPAState(
        generation=1, max_generations=2, proposals_per_generation=2
    )
    assert len(decision.proposals) == 2
    assert {proposal.parent for proposal in decision.proposals} == {Candidate("strong")}
    assert "make the answer shorter" in decision.proposals[0].prompt
    assert not decision.stop


def test_gepa_stops_at_generation_budget() -> None:
    state = GEPAState(generation=1, max_generations=1)

    decision = GEPAStrategy().advance((observation("done", 1.0),), state)

    assert decision == StrategyDecision(
        state=state, stop=True, reason="generation budget exhausted"
    )


def test_physics_strategy_asks_for_discriminating_observation_on_tie() -> None:
    strategy = PhysicsStrategy()
    state = PhysicsState(max_rounds=2, agreement_tolerance=0.1)

    decision = strategy.advance(
        (
            observation("gravity model", 0.8),
            observation("pressure model", 0.75),
        ),
        state,
    )

    assert decision.state == PhysicsState(
        round=1,
        max_rounds=2,
        physical_root=Candidate("physical evidence"),
        agreement_tolerance=0.1,
    )
    assert decision.proposals == (
        Proposal(
            prompt=(
                "Design the cheapest discriminating observation for these competing "
                "explanations:\nA: gravity model\nB: pressure model"
            ),
            parent=Candidate("physical evidence"),
        ),
    )


def test_physics_strategy_preserves_explicit_physical_root() -> None:
    root = Candidate("conservation law")
    state = PhysicsState(max_rounds=2, physical_root=root)

    decision = PhysicsStrategy().advance(
        (
            observation("new theory", 0.9, "fits run 4"),
            observation("old theory", 0.1, "fails run 4"),
        ),
        state,
    )

    assert decision.state.physical_root == root
    assert decision.proposals[0].parent == root
    assert "fails run 4" in decision.proposals[0].prompt


def test_physics_strategy_stops_at_experiment_budget() -> None:
    state = PhysicsState(round=1, max_rounds=1)

    decision = PhysicsStrategy().advance((observation("theory", 1.0),), state)

    assert decision == StrategyDecision(
        state=state, stop=True, reason="experiment budget exhausted"
    )
