import asyncio

from eggflow import FlowExecutor, TaskStore

from eggopt import (
    Accepted,
    Advance,
    Candidate,
    CaseEvidence,
    EvaluationRequest,
    Feedback,
    FunctionProducer,
    GEPAState,
    GEPAStrategy,
    Metric,
    NeedsRepair,
    Observation,
    PhysicsState,
    PhysicsStrategy,
    Proposal,
    RepairFeedback,
    RepairInput,
    StrategyInput,
)
from eggopt.eggflow import ProduceTask
from eggopt.eggflow_evaluation import EvaluationProducer
from eggopt.eggflow_repair import RepairingProducer


def _run(task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def test_complete_deterministic_substrate_replays_from_flow_db(tmp_path) -> None:
    flow_path = tmp_path / "flow.db"
    calls = {"case": 0, "aggregate": 0, "candidate": 0, "inspect": 0}
    repair_inputs = []

    def evaluate_case(request):
        calls["case"] += 1
        return CaseEvidence(
            request.case,
            metrics=(Metric("passes", float(request.case == "positive")),),
            feedback=(Feedback(f"example:{request.case}"),),
        )

    def aggregate(base):
        calls["aggregate"] += 1
        return Observation(
            base.candidate,
            cases=base.cases,
            metrics=(Metric("case_count", len(base.cases)),),
            feedback=(Feedback("retain both examples"),),
        )

    original = Candidate("def transition(state):\n    pass")
    request = EvaluationRequest(original, ("positive", "counterexample"))
    observation = _run(
        EvaluationProducer(
            FunctionProducer(evaluate_case),
            "case-evaluator:v1",
            FunctionProducer(aggregate),
            "aggregate:v1",
        ).produce(request),
        flow_path,
    )

    gepa = GEPAStrategy(
        FunctionProducer(lambda observations: observations),
        FunctionProducer(lambda parent: (parent.cases[1], parent.cases[0])),
        instruction="Revise the transition model.",
    )
    decision = _run(
        ProduceTask(
            gepa,
            "gepa:all-evidence:v1",
            StrategyInput(GEPAState(), (observation,)),
        ),
        flow_path,
    )
    assert isinstance(decision, Advance)
    proposal = decision.proposals[0]
    assert [case.case_id for case in proposal.evidence] == [
        "counterexample",
        "positive",
    ]
    assert proposal.feedback == observation.feedback

    def materialize(value: RepairInput[Proposal]) -> Candidate:
        calls["candidate"] += 1
        repair_inputs.append(value)
        text = value.original.parents[0].text + "\n# revised from two cases"
        if value.feedback:
            text += "\nreturn state + 1"
        return Candidate(text)

    def inspect(candidate: Candidate):
        calls["inspect"] += 1
        if "return state + 1" not in candidate.text:
            return NeedsRepair(RepairFeedback("add the missing transition"))
        return Accepted(candidate)

    repairing = RepairingProducer(
        FunctionProducer(materialize),
        "candidate-materializer:v1",
        FunctionProducer(inspect),
        "candidate-inspector:v1",
        max_repairs=1,
    )
    repaired = _run(repairing.produce(proposal), flow_path)
    assert isinstance(repaired, Candidate)
    assert repaired.text.endswith("return state + 1")
    assert repair_inputs[1].feedback == (
        RepairFeedback("add the missing transition"),
    )

    competing = Observation(Candidate("state stays fixed"))
    experiment = Proposal(
        parents=(repaired, competing.candidate),
        instruction="Probe one nonzero state.",
    )
    physics = PhysicsStrategy(
        FunctionProducer(lambda observations: observations),
        FunctionProducer(lambda hypotheses: None),
        FunctionProducer(lambda hypotheses: experiment),
        FunctionProducer(lambda observations: ()),
    )
    physics_decision = physics.produce(
        StrategyInput(PhysicsState(), (Observation(repaired), competing))
    )
    assert isinstance(physics_decision, Advance)
    assert physics_decision.proposals == (experiment,)
    assert calls == {"case": 2, "aggregate": 1, "candidate": 2, "inspect": 2}

    replay_calls = {"case": 0, "aggregate": 0, "candidate": 0, "inspect": 0}

    def replay(role, result):
        replay_calls[role] += 1
        return result

    replay_observation = _run(
        EvaluationProducer(
            FunctionProducer(lambda value: replay("case", evaluate_case(value))),
            "case-evaluator:v1",
            FunctionProducer(lambda value: replay("aggregate", aggregate(value))),
            "aggregate:v1",
        ).produce(request),
        flow_path,
    )
    replay_candidate = _run(
        RepairingProducer(
            FunctionProducer(lambda value: replay("candidate", materialize(value))),
            "candidate-materializer:v1",
            FunctionProducer(lambda value: replay("inspect", inspect(value))),
            "candidate-inspector:v1",
            max_repairs=1,
        ).produce(proposal),
        flow_path,
    )
    assert replay_observation == observation
    assert replay_candidate == repaired
    assert replay_calls == {"case": 0, "aggregate": 0, "candidate": 0, "inspect": 0}
