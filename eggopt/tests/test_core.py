from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from math import inf, nan
from pickle import dumps, loads
from typing import get_args, get_origin, get_type_hints

import pytest

from eggopt import (
    Advance,
    Candidate,
    CaseEvidence,
    Feedback,
    FunctionProducer,
    Metric,
    Observation,
    Producer,
    Proposal,
    Stop,
    StrategyDecision,
    StrategyInput,
)


def test_candidate_is_minimal_immutable_text_value() -> None:
    candidate = Candidate("")

    assert candidate.text == ""
    assert tuple(candidate.__dataclass_fields__) == ("text",)
    with pytest.raises(FrozenInstanceError):
        candidate.text = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="text must be a string"):
        Candidate(1)  # type: ignore[arg-type]


def test_function_producer_is_runtime_substitutable_and_composes() -> None:
    class PrefixProducer:
        def produce(self, value: int) -> str:
            return f"value={value}"

    increment = FunctionProducer(lambda value: value + 1)
    prefix = PrefixProducer()
    pipeline = increment.then(prefix)

    assert isinstance(increment, Producer)
    assert isinstance(prefix, Producer)
    assert isinstance(pipeline, Producer)
    assert pipeline.produce(2) == "value=3"
    with pytest.raises(TypeError, match="implement Producer"):
        increment.then(object())  # type: ignore[arg-type]


def test_feedback_defensively_freezes_nested_json_like_data() -> None:
    source = {
        "labels": ["good", {"rank": 1}],
        "details": {"accepted": True, "note": None},
    }
    feedback = Feedback("kept examples", source)

    source["labels"].append("later")  # type: ignore[union-attr]
    source["details"]["accepted"] = False  # type: ignore[index]

    assert feedback.data["labels"] == ("good", {"rank": 1})
    assert feedback.data["details"] == {"accepted": True, "note": None}
    with pytest.raises(TypeError):
        feedback.data["later"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        feedback.data["details"]["accepted"] = False  # type: ignore[index]


def test_core_transition_values_round_trip_through_pickle() -> None:
    feedback = Feedback(
        "structured",
        {"nested": {"examples": ["one", "two"]}, "score": 1.5},
    )
    evidence = CaseEvidence(
        "case-1", metrics=(Metric("accuracy", 0.75),), feedback=(feedback,)
    )
    observation = Observation(
        Candidate("policy"),
        cases=(evidence,),
        metrics=(Metric("mean_accuracy", 0.75),),
        feedback=(feedback,),
    )
    strategy_input = StrategyInput(state=("round", 1), observations=(observation,))
    proposal = Proposal(
        parents=(observation.candidate,), instruction="revise", evidence=(evidence,)
    )
    values = (
        feedback,
        evidence,
        observation,
        strategy_input,
        proposal,
        Advance(state=("round", 2), proposals=(proposal,)),
        Stop(state=("round", 2), reason="done"),
    )

    restored = loads(dumps(values))

    assert restored == values
    restored_feedback = restored[0]
    assert isinstance(restored_feedback.data, Mapping)
    assert isinstance(restored_feedback.data["nested"], Mapping)
    assert restored_feedback.data["nested"]["examples"] == ("one", "two")
    with pytest.raises(TypeError):
        restored_feedback.data["later"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        restored_feedback.data["nested"]["later"] = True  # type: ignore[index]


@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_non_finite_numeric_evidence_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        Metric("score", value)
    with pytest.raises(ValueError, match="non-finite"):
        Feedback("invalid", {"nested": [value]})


def test_observation_preserves_multiple_ordered_case_and_aggregate_evidence() -> None:
    good_case = CaseEvidence(
        "case-good",
        metrics=(Metric("accuracy", 1.0), Metric("cost", 2)),
        feedback=(Feedback("useful", {"examples": ["a", "b"]}),),
    )
    bad_case = CaseEvidence(
        "case-bad",
        metrics=(Metric("accuracy", 0.0),),
        feedback=(Feedback("failed", {"line": 7}), Feedback("try another rule")),
    )
    cases = [good_case, bad_case]
    observation = Observation(
        Candidate("policy"),
        cases=cases,  # type: ignore[arg-type]
        metrics=[Metric("mean_accuracy", 0.5)],  # type: ignore[arg-type]
        feedback=[Feedback("aggregate note")],  # type: ignore[arg-type]
    )
    cases.clear()

    assert observation.cases == (good_case, bad_case)
    assert observation.metrics == (Metric("mean_accuracy", 0.5),)
    assert observation.feedback == (Feedback("aggregate note"),)
    assert len(observation.cases[0].feedback[0].data["examples"]) == 2


def test_identifiers_and_collection_members_are_validated() -> None:
    with pytest.raises(ValueError, match="name must not be empty"):
        Metric("", 1.0)
    with pytest.raises(ValueError, match="case_id must not be empty"):
        CaseEvidence("")
    with pytest.raises(TypeError, match="only Metric"):
        CaseEvidence("case", metrics=("score",))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="keys must be strings"):
        Feedback("bad key", {1: "value"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="non-JSON-like"):
        Feedback("bad value", {"value": object()})


def test_proposal_supports_initialization_mutation_and_crossover() -> None:
    first = Candidate("first")
    second = Candidate("second")
    evidence = CaseEvidence("selected", feedback=(Feedback("informative"),))

    initialization = Proposal()
    mutation = Proposal(parents=[first], instruction="revise")  # type: ignore[arg-type]
    crossover = Proposal(
        parents=(first, second),
        instruction="combine",
        evidence=[evidence],  # type: ignore[arg-type]
    )

    assert initialization.parents == ()
    assert initialization.instruction == ""
    assert mutation.parents == (first,)
    assert crossover.parents == (first, second)
    assert crossover.evidence == (evidence,)


def test_strategy_transition_contracts_normalize_and_enforce_tag_invariants() -> None:
    observation = Observation(Candidate("candidate"))
    transition_input = StrategyInput(
        state={"round": 1}, observations=[observation]  # type: ignore[arg-type]
    )
    proposal = Proposal(parents=(observation.candidate,))
    advance = Advance(
        state={"round": 2}, proposals=[proposal]  # type: ignore[arg-type]
    )
    stop = Stop(state={"round": 2}, reason="budget exhausted")

    assert transition_input.observations == (observation,)
    assert advance.proposals == (proposal,)
    assert stop.reason == "budget exhausted"
    decision_hint = get_type_hints(_decision_identity)["return"]
    assert get_origin(decision_hint) is get_origin(StrategyDecision)
    decision_members = {get_origin(member) for member in get_args(decision_hint)}
    assert decision_members == {Advance, Stop}

    with pytest.raises(ValueError, match="at least one proposal"):
        Advance(state=None, proposals=())
    with pytest.raises(ValueError, match="reason must not be empty"):
        Stop(state=None, reason="")


def _decision_identity(value: StrategyDecision[int]) -> StrategyDecision[int]:
    return value
