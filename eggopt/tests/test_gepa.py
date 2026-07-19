from pickle import dumps, loads

import pytest

from eggopt import (
    Advance,
    Candidate,
    CaseEvidence,
    Feedback,
    FunctionProducer,
    GEPAState,
    GEPAStrategy,
    Observation,
    Producer,
    Stop,
    StrategyInput,
)


def test_gepa_builds_ordered_proposals_from_injected_selection() -> None:
    first = Observation(
        Candidate('{"policy": "first"}\nallow = []'),
        cases=(CaseEvidence("first-good"), CaseEvidence("first-bad")),
        feedback=(Feedback("first aggregate", {"attempts": [1, 2]}),),
    )
    second = Observation(
        Candidate("def decide(state):\n    return state"),
        cases=(CaseEvidence("second-good"), CaseEvidence("second-bad")),
        feedback=(Feedback("second aggregate"),),
    )
    strategy = GEPAStrategy(
        select_parents=FunctionProducer(
            lambda observations: (observations[1], observations[0])
        ),
        select_evidence=FunctionProducer(
            lambda observation: tuple(reversed(observation.cases))
        ),
        instruction="Reflect on both examples.",
        proposals_per_parent=2,
    )

    decision = strategy.produce(
        StrategyInput(GEPAState(max_generations=3), (first, second))
    )

    assert isinstance(strategy, Producer)
    assert isinstance(decision, Advance)
    assert decision.state == GEPAState(generation=1, max_generations=3)
    assert [proposal.parents for proposal in decision.proposals] == [
        (second.candidate,),
        (second.candidate,),
        (first.candidate,),
        (first.candidate,),
    ]
    assert [proposal.evidence for proposal in decision.proposals] == [
        tuple(reversed(second.cases)),
        tuple(reversed(second.cases)),
        tuple(reversed(first.cases)),
        tuple(reversed(first.cases)),
    ]
    assert [proposal.feedback for proposal in decision.proposals] == [
        second.feedback,
        second.feedback,
        first.feedback,
        first.feedback,
    ]
    assert all(
        proposal.instruction == "Reflect on both examples."
        for proposal in decision.proposals
    )


@pytest.mark.parametrize(
    ("strategy_input", "select_parents", "reason"),
    [
        (
            StrategyInput(
                GEPAState(generation=1, max_generations=1),
                (Observation(Candidate("candidate")),),
            ),
            lambda observations: observations,
            "generation budget exhausted",
        ),
        (
            StrategyInput(GEPAState(max_generations=1)),
            lambda observations: observations,
            "no observations available",
        ),
        (
            StrategyInput(
                GEPAState(max_generations=1),
                (Observation(Candidate("candidate")),),
            ),
            lambda observations: (),
            "parent selector chose no parents",
        ),
    ],
)
def test_gepa_stops_for_budget_or_missing_selection(
    strategy_input: StrategyInput[GEPAState],
    select_parents,
    reason: str,
) -> None:
    strategy = GEPAStrategy(
        FunctionProducer(select_parents),
        FunctionProducer(lambda observation: observation.cases),
    )

    decision = strategy.produce(strategy_input)

    assert decision == Stop(state=strategy_input.state, reason=reason)


def test_gepa_rejects_parent_or_evidence_not_drawn_from_input() -> None:
    parent = Observation(Candidate("parent"), cases=(CaseEvidence("owned"),))
    strategy_input = StrategyInput(GEPAState(), (parent,))
    outside_parent = Observation(Candidate("outside"))
    outside_case = CaseEvidence("outside")

    with pytest.raises(ValueError, match="parents must come from input"):
        GEPAStrategy(
            FunctionProducer(lambda observations: (outside_parent,)),
            FunctionProducer(lambda observation: observation.cases),
        ).produce(strategy_input)

    with pytest.raises(ValueError, match="evidence must come from its parent"):
        GEPAStrategy(
            FunctionProducer(lambda observations: observations),
            FunctionProducer(lambda observation: (outside_case,)),
        ).produce(strategy_input)

    equal_but_external_parent = Observation(
        Candidate("parent"), cases=(parent.cases[0],)
    )
    with pytest.raises(ValueError, match="parents must come from input"):
        GEPAStrategy(
            FunctionProducer(lambda observations: (equal_but_external_parent,)),
            FunctionProducer(lambda observation: observation.cases),
        ).produce(strategy_input)

    equal_but_external_case = CaseEvidence("owned")
    with pytest.raises(ValueError, match="evidence must come from its parent"):
        GEPAStrategy(
            FunctionProducer(lambda observations: observations),
            FunctionProducer(lambda observation: (equal_but_external_case,)),
        ).produce(strategy_input)


def test_gepa_configuration_and_state_validation() -> None:
    parents = FunctionProducer(lambda observations: observations)
    evidence = FunctionProducer(lambda observation: observation.cases)

    assert loads(dumps(GEPAState(1, 2))) == GEPAState(1, 2)
    with pytest.raises(ValueError, match="generation must be nonnegative"):
        GEPAState(generation=-1)
    with pytest.raises(ValueError, match="max_generations must be nonnegative"):
        GEPAState(max_generations=-1)
    with pytest.raises(TypeError, match="select_parents must implement Producer"):
        GEPAStrategy(object(), evidence)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="select_evidence must implement Producer"):
        GEPAStrategy(parents, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="instruction must be a string"):
        GEPAStrategy(parents, evidence, instruction=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="proposals_per_parent must be positive"):
        GEPAStrategy(parents, evidence, proposals_per_parent=0)
