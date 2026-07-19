import pytest

from eggopt import (
    Candidate,
    FunctionProducer,
    GEPAState,
    GEPAStrategy,
    Producer,
    Proposal,
    Strategy,
    StrategyDecision,
)


def test_candidate_is_text_only_value() -> None:
    assert Candidate("first") == Candidate(text="first")


def test_observation_copies_metrics_into_immutable_evidence() -> None:
    from eggopt import Observation

    metrics = {"accuracy": 0.5}
    evidence = Observation(Candidate("first"), 0.5, metrics=metrics)
    metrics["accuracy"] = 1.0

    assert evidence.metrics == {"accuracy": 0.5}
    with pytest.raises(TypeError):
        evidence.metrics["accuracy"] = 0.0  # type: ignore[index]


def test_function_producers_compose() -> None:
    normalize = FunctionProducer[str, str](str.strip)
    candidate = FunctionProducer[str, Candidate](Candidate)

    assert normalize.then(candidate).produce("  answer  ") == Candidate("answer")


def test_strategies_are_producers() -> None:
    strategy = GEPAStrategy()

    assert isinstance(strategy, Producer)
    assert isinstance(strategy, Strategy)


def test_decision_has_exactly_one_outcome() -> None:
    with pytest.raises(ValueError, match="stopping decision cannot"):
        StrategyDecision(
            state=None,
            proposals=(Proposal("try another"),),
            stop=True,
            reason="done",
        )

    with pytest.raises(ValueError, match="must include a reason"):
        StrategyDecision(state=None, stop=True)

    with pytest.raises(ValueError, match="must contain proposals"):
        StrategyDecision(state=None)
