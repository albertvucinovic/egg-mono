import asyncio
from dataclasses import dataclass, field
from pickle import dumps, loads

import pytest
from eggflow import FlowExecutor, Task, TaskError, TaskStore

from eggopt import (
    Candidate,
    CaseEvidence,
    CaseRequest,
    EvaluationRequest,
    Feedback,
    Metric,
    Observation,
    Producer,
)
from eggopt.eggflow_evaluation import EvaluationProducer


@dataclass
class CountingCaseProducer:
    calls: list[str] = field(default_factory=list)

    def produce(self, request: CaseRequest[str]) -> CaseEvidence:
        self.calls.append(request.case)
        return CaseEvidence(
            request.case,
            metrics=(Metric("score", len(request.case)),),
            feedback=(Feedback(f"evidence:{request.case}"),),
        )


@dataclass
class CountingAggregate:
    calls: list[Observation] = field(default_factory=list)

    def produce(self, observation: Observation) -> Observation:
        self.calls.append(observation)
        return Observation(
            candidate=observation.candidate,
            cases=observation.cases,
            metrics=(Metric("case_count", len(observation.cases)),),
            feedback=(Feedback("aggregate feedback"),),
        )


def _run(task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def test_deterministic_map_preserves_order_and_aggregate_evidence(tmp_path) -> None:
    cases = CountingCaseProducer()
    aggregate = CountingAggregate()
    producer = EvaluationProducer(cases, "cases:v1", aggregate, "aggregate:v1")
    request = EvaluationRequest(Candidate("policy"), ["second", "first"])

    result = _run(producer.produce(request), tmp_path / "flow.db")

    assert isinstance(producer, Producer)
    assert cases.calls == ["second", "first"]
    assert [case.case_id for case in result.cases] == ["second", "first"]
    assert result.cases[0].feedback == (Feedback("evidence:second"),)
    assert result.metrics == (Metric("case_count", 2),)
    assert result.feedback == (Feedback("aggregate feedback"),)


def test_domain_case_may_be_an_indivisible_batch(tmp_path) -> None:
    case_producer = BatchCaseProducer()
    aggregate = CountingAggregate()
    batch = ("fixture-a", "fixture-b")

    result = _run(
        EvaluationProducer(
            case_producer, "batch-case:v1", aggregate, "aggregate:v1"
        ).produce(EvaluationRequest(Candidate("policy"), (batch,))),
        tmp_path / "flow.db",
    )

    assert case_producer.calls == [batch]
    assert result.cases == (CaseEvidence("batch:2"),)


def test_task_returning_case_and_aggregate_producers_are_flattened(tmp_path) -> None:
    case_producer = TaskCaseProducer()
    aggregate = TaskAggregateProducer()
    request = EvaluationRequest(Candidate("code"), ("one", "two"))

    result = _run(
        EvaluationProducer(
            case_producer,
            "task-cases:v1",
            aggregate,
            "task-aggregate:v1",
        ).produce(request),
        tmp_path / "flow.db",
    )

    assert [case.case_id for case in result.cases] == ["one", "two"]
    assert result.metrics == (Metric("task_aggregate", 2),)
    assert case_producer.calls == 2
    assert aggregate.calls == 1


def test_changed_case_reuses_unchanged_per_case_cache(tmp_path) -> None:
    flow_path = tmp_path / "flow.db"
    first_cases = CountingCaseProducer()
    first_aggregate = CountingAggregate()
    first_request = EvaluationRequest(Candidate("policy"), ("a", "b"))
    _run(
        EvaluationProducer(
            first_cases, "cases:v1", first_aggregate, "aggregate:v1"
        ).produce(first_request),
        flow_path,
    )

    second_cases = CountingCaseProducer()
    second_aggregate = CountingAggregate()
    second_request = EvaluationRequest(Candidate("policy"), ("a", "c"))
    result = _run(
        EvaluationProducer(
            second_cases, "cases:v1", second_aggregate, "aggregate:v1"
        ).produce(second_request),
        flow_path,
    )

    assert first_cases.calls == ["a", "b"]
    assert second_cases.calls == ["c"]
    assert len(second_aggregate.calls) == 1
    assert [case.case_id for case in result.cases] == ["a", "c"]


def test_full_replay_with_new_producers_makes_no_calls(tmp_path) -> None:
    flow_path = tmp_path / "flow.db"
    request = EvaluationRequest(Candidate("policy"), ("a", "b"))
    first_cases = CountingCaseProducer()
    first_aggregate = CountingAggregate()
    first_result = _run(
        EvaluationProducer(
            first_cases, "cases:v1", first_aggregate, "aggregate:v1"
        ).produce(request),
        flow_path,
    )

    second_cases = CountingCaseProducer()
    second_aggregate = CountingAggregate()
    second_result = _run(
        EvaluationProducer(
            second_cases, "cases:v1", second_aggregate, "aggregate:v1"
        ).produce(request),
        flow_path,
    )

    assert second_result == first_result
    assert first_cases.calls == ["a", "b"]
    assert len(first_aggregate.calls) == 1
    assert second_cases.calls == []
    assert second_aggregate.calls == []


def test_empty_case_set_is_aggregated(tmp_path) -> None:
    cases = CountingCaseProducer()
    aggregate = CountingAggregate()

    result = _run(
        EvaluationProducer(
            cases, "cases:v1", aggregate, "aggregate:v1"
        ).produce(EvaluationRequest(Candidate("policy"))),
        tmp_path / "flow.db",
    )

    assert cases.calls == []
    assert len(aggregate.calls) == 1
    assert result.cases == ()
    assert result.metrics == (Metric("case_count", 0),)


@pytest.mark.parametrize(
    ("case_kind", "aggregate_kind", "message"),
    [
        (
            "invalid",
            "valid",
            "case_producer must produce CaseEvidence",
        ),
        (
            "valid",
            "invalid",
            "aggregate must produce an Observation",
        ),
        (
            "valid",
            "candidate",
            "aggregate must preserve the request candidate",
        ),
        (
            "valid",
            "drop",
            "aggregate must preserve all case evidence in order",
        ),
        (
            "valid",
            "reverse",
            "aggregate must preserve all case evidence in order",
        ),
    ],
)
def test_invalid_case_or_aggregate_result_fails_task(
    tmp_path, case_kind: str, aggregate_kind: str, message: str
) -> None:
    case_producer = (
        ConstantProducer("not evidence")
        if case_kind == "invalid"
        else CountingCaseProducer()
    )
    if aggregate_kind == "valid":
        aggregate = CountingAggregate()
    elif aggregate_kind == "invalid":
        aggregate = ConstantProducer("not observation")
    else:
        aggregate = AlteringAggregate(aggregate_kind)
    producer = EvaluationProducer(
        case_producer, "cases:v1", aggregate, "aggregate:v1"
    )
    store = TaskStore(str(tmp_path / "flow.db"))
    try:
        with pytest.raises(TaskError, match=message):
            asyncio.run(
                FlowExecutor(store).run(
                    producer.produce(
                        EvaluationRequest(Candidate("policy"), ("a", "b"))
                    )
                )
            )
    finally:
        store.conn.close()


def test_request_key_and_configuration_validation() -> None:
    candidate = Candidate("policy")
    request = EvaluationRequest(candidate, ["a", "b"])
    values = (CaseRequest(candidate, {"batch": [1, 2]}), request)

    assert request.cases == ("a", "b")
    assert loads(dumps(values)) == values

    case_producer = CountingCaseProducer()
    aggregate = CountingAggregate()
    base = EvaluationProducer(
        case_producer, "cases:v1", aggregate, "aggregate:v1"
    ).produce(request)
    assert base.get_cache_key() != EvaluationProducer(
        case_producer, "cases:v2", aggregate, "aggregate:v1"
    ).produce(request).get_cache_key()
    assert base.get_cache_key() != EvaluationProducer(
        case_producer, "cases:v1", aggregate, "aggregate:v2"
    ).produce(request).get_cache_key()
    assert base.get_cache_key() != EvaluationProducer(
        case_producer, "cases:v1", aggregate, "aggregate:v1"
    ).produce(EvaluationRequest(candidate, ("a", "c"))).get_cache_key()

    with pytest.raises(TypeError, match="candidate must be a Candidate"):
        CaseRequest("candidate", "case")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="candidate must be a Candidate"):
        EvaluationRequest("candidate")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cases must be an iterable"):
        EvaluationRequest(candidate, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="case_producer must implement Producer"):
        EvaluationProducer(
            object(),  # type: ignore[arg-type]
            "cases:v1",
            aggregate,
            "aggregate:v1",
        )
    with pytest.raises(TypeError, match="aggregate must implement Producer"):
        EvaluationProducer(
            case_producer,
            "cases:v1",
            object(),  # type: ignore[arg-type]
            "aggregate:v1",
        )
    with pytest.raises(ValueError, match="case_identity must not be empty"):
        EvaluationProducer(case_producer, "", aggregate, "aggregate:v1")
    with pytest.raises(TypeError, match="case_identity must be a string"):
        EvaluationProducer(
            case_producer,
            1,  # type: ignore[arg-type]
            aggregate,
            "aggregate:v1",
        )
    with pytest.raises(ValueError, match="aggregate_identity must not be empty"):
        EvaluationProducer(case_producer, "cases:v1", aggregate, "")
    with pytest.raises(TypeError, match="aggregate_identity must be a string"):
        EvaluationProducer(
            case_producer,
            "cases:v1",
            aggregate,
            1,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="request must be an EvaluationRequest"):
        EvaluationProducer(
            case_producer, "cases:v1", aggregate, "aggregate:v1"
        ).produce("request")  # type: ignore[arg-type]

    class Unpickleable:
        def __reduce__(self):
            raise RuntimeError("cannot serialize")

    with pytest.raises(TypeError, match="request must be pickleable"):
        EvaluationProducer(
            case_producer, "cases:v1", aggregate, "aggregate:v1"
        ).produce(
            EvaluationRequest(candidate, (Unpickleable(),))
        ).get_cache_key()


@dataclass
class EvidenceTask(Task):
    evidence: CaseEvidence

    def run(self) -> CaseEvidence:
        return self.evidence


@dataclass
class ObservationTask(Task):
    observation: Observation

    def run(self) -> Observation:
        return self.observation


@dataclass
class BatchCaseProducer:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def produce(self, request: CaseRequest[tuple[str, ...]]) -> CaseEvidence:
        self.calls.append(request.case)
        return CaseEvidence(f"batch:{len(request.case)}")


@dataclass
class TaskCaseProducer:
    calls: int = 0

    def produce(self, request: CaseRequest[str]) -> Task:
        self.calls += 1
        return EvidenceTask(CaseEvidence(request.case))


@dataclass
class TaskAggregateProducer:
    calls: int = 0

    def produce(self, observation: Observation) -> Task:
        self.calls += 1
        return ObservationTask(
            Observation(
                candidate=observation.candidate,
                cases=observation.cases,
                metrics=(Metric("task_aggregate", len(observation.cases)),),
            )
        )


@dataclass
class ConstantProducer:
    value: object

    def produce(self, value):
        return self.value


@dataclass
class AlteringAggregate:
    alteration: str

    def produce(self, observation: Observation) -> Observation:
        if self.alteration == "candidate":
            return Observation(Candidate("other"), cases=observation.cases)
        if self.alteration == "drop":
            return Observation(
                observation.candidate, cases=observation.cases[:-1]
            )
        return Observation(
            observation.candidate, cases=tuple(reversed(observation.cases))
        )
