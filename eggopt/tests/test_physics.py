from pickle import dumps, loads

import pytest

from eggopt import (
    Advance,
    Candidate,
    CaseEvidence,
    Feedback,
    FunctionProducer,
    Observation,
    PhysicsState,
    PhysicsStrategy,
    Producer,
    Proposal,
    Stop,
    StrategyInput,
)


def _strategy(
    *,
    select=lambda observations: observations,
    plan=lambda hypotheses: None,
    experiment=lambda hypotheses: Proposal(instruction="experiment"),
    revise=lambda observations: (),
) -> PhysicsStrategy:
    return PhysicsStrategy(
        select_hypotheses=FunctionProducer(select),
        propose_plan=FunctionProducer(plan),
        propose_experiment=FunctionProducer(experiment),
        revise_hypotheses=FunctionProducer(revise),
    )


def test_empty_input_requests_zero_parent_model_inventions() -> None:
    inventions = (
        Proposal(instruction="invent model A"),
        Proposal(instruction="invent model B"),
    )
    calls = []
    strategy = _strategy(
        select=lambda observations: calls.append(("select", observations)) or (),
        revise=lambda observations: calls.append(("revise", observations))
        or inventions,
    )

    decision = strategy.produce(StrategyInput(PhysicsState(max_rounds=3)))

    assert isinstance(strategy, Producer)
    assert calls == [("select", ()), ("revise", ())]
    assert decision == Advance(
        state=PhysicsState(round=1, max_rounds=3), proposals=inventions
    )
    assert all(proposal.parents == () for proposal in decision.proposals)


def test_competing_arbitrary_models_request_experiment_when_plan_is_absent() -> None:
    first = Observation(Candidate('{"gravity": "down"}'))
    second = Observation(Candidate("def transition(state): return state + 1"))
    experiment = Proposal(
        parents=(first.candidate, second.candidate),
        instruction="Run the cheapest discriminating observation.",
    )
    selected_hypotheses = ()

    def propose_experiment(hypotheses):
        nonlocal selected_hypotheses
        selected_hypotheses = hypotheses
        return experiment

    strategy = _strategy(
        select=lambda observations: (observations[1], observations[0]),
        experiment=propose_experiment,
    )

    decision = strategy.produce(
        StrategyInput(PhysicsState(max_rounds=2), (first, second))
    )

    assert selected_hypotheses == (second, first)
    assert decision == Advance(
        state=PhysicsState(round=1, max_rounds=2), proposals=(experiment,)
    )


def test_credible_plan_is_preferred_over_experiment() -> None:
    model = Observation(Candidate("world model"))
    plan = Proposal(parents=(model.candidate,), instruction="use consistent plan")
    experiment_calls = 0

    def propose_experiment(hypotheses):
        nonlocal experiment_calls
        experiment_calls += 1
        return Proposal(instruction="should not run")

    decision = _strategy(
        plan=lambda hypotheses: plan,
        experiment=propose_experiment,
    ).produce(StrategyInput(PhysicsState(), (model,)))

    assert decision == Advance(state=PhysicsState(round=1), proposals=(plan,))
    assert experiment_calls == 0


def test_inconsistent_hypotheses_request_revisions_with_counterexamples() -> None:
    counterexample = CaseEvidence(
        "counterexample",
        feedback=(Feedback("transition prediction failed"),),
    )
    rejected = Observation(
        Candidate("old model"),
        cases=(counterexample,),
        feedback=(Feedback("revise dynamics"),),
    )
    revision = Proposal(
        parents=(rejected.candidate,),
        instruction="revise the explanatory model",
        evidence=(counterexample,),
        feedback=rejected.feedback,
    )
    revision_inputs = ()

    def revise_hypotheses(observations):
        nonlocal revision_inputs
        revision_inputs = observations
        return (revision,)

    strategy = _strategy(
        select=lambda observations: (),
        revise=revise_hypotheses,
    )

    decision = strategy.produce(StrategyInput(PhysicsState(), (rejected,)))

    assert revision_inputs == (rejected,)
    assert decision == Advance(
        state=PhysicsState(round=1), proposals=(revision,)
    )


@pytest.mark.parametrize(
    ("strategy_input", "strategy", "reason"),
    [
        (
            StrategyInput(
                PhysicsState(round=1, max_rounds=1),
                (Observation(Candidate("model")),),
            ),
            _strategy(),
            "round budget exhausted",
        ),
        (
            StrategyInput(
                PhysicsState(max_rounds=1),
                (Observation(Candidate("inconsistent")),),
            ),
            _strategy(select=lambda observations: (), revise=lambda observations: ()),
            "no consistent hypotheses or revisions available",
        ),
    ],
)
def test_physics_stops_for_budget_or_missing_revisions(
    strategy_input: StrategyInput[PhysicsState],
    strategy: PhysicsStrategy,
    reason: str,
) -> None:
    assert strategy.produce(strategy_input) == Stop(
        state=strategy_input.state, reason=reason
    )


def test_external_selected_hypothesis_is_rejected() -> None:
    supplied = Observation(Candidate("model"))
    equal_but_external = Observation(Candidate("model"))

    with pytest.raises(ValueError, match="hypotheses must come from input"):
        _strategy(select=lambda observations: (equal_but_external,)).produce(
            StrategyInput(PhysicsState(), (supplied,))
        )


@pytest.mark.parametrize(
    "field",
    [
        "select_hypotheses",
        "propose_plan",
        "propose_experiment",
        "revise_hypotheses",
    ],
)
def test_physics_requires_producer_roles(field: str) -> None:
    roles = {
        "select_hypotheses": FunctionProducer(lambda observations: observations),
        "propose_plan": FunctionProducer(lambda hypotheses: None),
        "propose_experiment": FunctionProducer(
            lambda hypotheses: Proposal(instruction="experiment")
        ),
        "revise_hypotheses": FunctionProducer(lambda observations: ()),
    }
    roles[field] = object()

    with pytest.raises(TypeError, match=f"{field} must implement Producer"):
        PhysicsStrategy(**roles)  # type: ignore[arg-type]


def test_physics_rejects_wrong_role_outputs() -> None:
    model = Observation(Candidate("model"))
    strategy_input = StrategyInput(PhysicsState(), (model,))

    with pytest.raises(TypeError, match="selected hypotheses"):
        _strategy(select=lambda observations: ("not an observation",)).produce(
            strategy_input
        )
    with pytest.raises(TypeError, match="Proposal or None"):
        _strategy(plan=lambda hypotheses: "not a proposal").produce(strategy_input)
    with pytest.raises(TypeError, match="must produce a Proposal"):
        _strategy(experiment=lambda hypotheses: None).produce(strategy_input)
    with pytest.raises(TypeError, match="hypothesis revisions"):
        _strategy(
            select=lambda observations: (),
            revise=lambda observations: ("not a proposal",),
        ).produce(strategy_input)


def test_physics_state_validation_and_pickle() -> None:
    state = PhysicsState(round=1, max_rounds=2)

    assert loads(dumps(state)) == state
    with pytest.raises(ValueError, match="round must be nonnegative"):
        PhysicsState(round=-1)
    with pytest.raises(ValueError, match="max_rounds must be nonnegative"):
        PhysicsState(max_rounds=-1)
