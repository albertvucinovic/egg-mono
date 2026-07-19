import asyncio
import json
import threading
from dataclasses import dataclass, field
from pickle import dumps, loads

import pytest
from eggflow import FlowExecutor, Task, TaskStore
from eggthreads import (
    ThreadsDB,
    create_child_thread,
    get_parent,
)

from eggopt import (
    Advance,
    Candidate,
    CaseEvidence,
    CaseRequest,
    ItemFailure,
    Metric,
    Observation,
    OperationInput,
    Producer,
    Proposal,
    StrategyInput,
    StrategyRunInput,
    StrategyRunResult,
)
from eggopt.eggthreads_runtime import HierarchicalRuntime
import eggopt.eggthreads_runtime as runtime_adapter


@dataclass
class FakeStrategy:
    calls: list[StrategyInput[int]] = field(default_factory=list)

    def produce(self, operation: OperationInput[StrategyInput[int]]):
        value = operation.value
        self.calls.append(value)
        return Advance(1, (Proposal(instruction="first"),))


@dataclass
class FakeCandidateProducer:
    calls: list[str] = field(default_factory=list)
    contexts: list = field(default_factory=list)
    threads_db_path: str | None = None
    child_thread_id: str | None = None

    def produce(self, operation: OperationInput[Proposal]) -> Candidate:
        value = operation.value
        self.calls.append(value.instruction)
        self.contexts.append(operation.context)
        if self.threads_db_path and self.child_thread_id is None:
            db = ThreadsDB(self.threads_db_path)
            try:
                db.init_schema()
                self.child_thread_id = create_child_thread(
                    db, operation.context.thread_id, name="Mutation"
                )
            finally:
                db.conn.close()
        if value.instruction == "seed":
            return value.parents[0]
        return Candidate(value.instruction)


@dataclass
class ConcurrentCases:
    calls: list[tuple[str, str]] = field(default_factory=list)
    active: int = 0
    peak: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    async def produce(
        self, operation: OperationInput[CaseRequest[str]]
    ) -> CaseEvidence:
        value = operation.value
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((value.candidate.text, value.case))
        await asyncio.sleep(0.03)
        with self.lock:
            self.active -= 1
        return CaseEvidence(value.case, metrics=(Metric("length", len(value.case)),))


@dataclass
class OrderedAggregate:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def produce(self, operation: OperationInput[Observation]) -> Observation:
        value = operation.value
        case_ids = tuple(case.case_id for case in value.cases)
        self.calls.append(case_ids)
        return Observation(
            value.candidate,
            cases=value.cases,
            metrics=(Metric("count", len(value.cases)),),
        )


def _run(task: Task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def _threads(db_path):
    db = ThreadsDB(db_path)
    try:
        db.init_schema()
        rows = db.conn.execute(
            "SELECT thread_id, name FROM threads ORDER BY rowid"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]
    finally:
        db.conn.close()


def _audit_messages(db_path, thread_id):
    db = ThreadsDB(db_path)
    try:
        db.init_schema()
        messages = []
        for event in db.events_since(thread_id, -1):
            if event["type"] != "msg.create":
                continue
            payload = json.loads(event["payload_json"])
            if payload.get("eggopt_operation_audit"):
                messages.append(payload)
        return messages
    finally:
        db.conn.close()


def _runtime(threads_path, strategy, candidates, cases, aggregate):
    return HierarchicalRuntime(
        str(threads_path),
        strategy,
        "strategy:v1",
        candidates,
        "candidate:v1",
        cases,
        "case:v1",
        aggregate,
        "aggregate:v1",
    )


def test_exact_hierarchy_serial_steps_and_bounded_ordered_cases(
    tmp_path, monkeypatch
) -> None:
    def fail_scan(*args, **kwargs):
        raise AssertionError("thread listing scans must not be used")

    for name in (
        "list_threads",
        "list_root_threads",
        "list_children_ids",
        "list_children_with_meta",
    ):
        monkeypatch.setattr(runtime_adapter, name, fail_scan, raising=False)

    threads_path = tmp_path / "threads.sqlite"
    strategy = FakeStrategy()
    candidates = FakeCandidateProducer(threads_db_path=str(threads_path))
    cases = ConcurrentCases()
    aggregate = OrderedAggregate()
    runtime = _runtime(threads_path, strategy, candidates, cases, aggregate)
    request = StrategyRunInput(
        state=0,
        seed=Candidate("seed candidate"),
        cases=("slow", "fast", "last"),
        max_steps=1,
        max_concurrent_cases=2,
    )

    task = runtime.produce(request)
    result = _run(task, tmp_path / "flow.db")

    assert isinstance(runtime, Producer)
    assert isinstance(task, Task)
    assert isinstance(result, StrategyRunResult)
    assert loads(dumps(result)) == result
    assert [step.step_id for step in result.steps] == ["S000", "S001"]
    assert [
        proposal.proposal_id
        for step in result.steps
        for proposal in step.proposals
    ] == ["P000", "P001"]
    assert result.steps[0].transition is None
    assert candidates.calls == ["seed", "first"]
    assert [context.semantic_name for context in candidates.contexts] == [
        "Production",
        "Production",
    ]
    db = ThreadsDB(threads_path)
    try:
        db.init_schema()
        assert (
            get_parent(db, candidates.child_thread_id)
            == result.steps[0].proposals[0].production.thread_id
        )
    finally:
        db.conn.close()
    assert len(strategy.calls) == 1
    first_observations = strategy.calls[0].observations
    assert [item.candidate.text for item in first_observations] == [
        "seed candidate"
    ]
    assert cases.peak == 2
    assert aggregate.calls == [
        ("slow", "fast", "last"),
        ("slow", "fast", "last"),
    ]

    thread_rows = _threads(threads_path)
    runtime_thread_rows = [row for row in thread_rows if row[1] != "Mutation"]
    assert [name for _, name in runtime_thread_rows] == [
        "StudyRoot",
        "StrategyRunRoot",
        "RunSetup",
        "Step S000",
        "Proposal P000",
        "Production",
        "Evaluation",
        "Case K000",
        "Case K001",
        "Case K002",
        "Aggregation",
        "Step S001",
        "StrategyTransition",
        "Proposal P001",
        "Production",
        "Evaluation",
        "Case K000",
        "Case K001",
        "Case K002",
        "Aggregation",
    ]
    assert "Validation" not in [name for _, name in thread_rows]

    operation_ids = {
        operation.thread_id
        for step in result.steps
        for operation in (step.transition,)
        if operation is not None
    }
    operation_ids.update(
        operation.thread_id
        for step in result.steps
        for proposal in step.proposals
        for operation in (
            proposal.production,
            *proposal.cases,
            proposal.aggregation,
        )
    )
    thread_ids = {thread_id for thread_id, _ in runtime_thread_rows}
    structural_ids = {
        result.study_thread_id,
        result.strategy_thread_id,
        result.run_setup_thread_id,
        *(step.step_thread_id for step in result.steps),
        *(
            proposal.proposal_thread_id
            for step in result.steps
            for proposal in step.proposals
        ),
        *(
            proposal.evaluation_thread_id
            for step in result.steps
            for proposal in step.proposals
        ),
    }
    assert thread_ids == structural_ids | operation_ids

    db = ThreadsDB(threads_path)
    try:
        db.init_schema()
        assert get_parent(db, result.study_thread_id) is None
        assert (
            get_parent(db, result.strategy_thread_id)
            == result.study_thread_id
        )
        assert (
            get_parent(db, result.run_setup_thread_id)
            == result.strategy_thread_id
        )
        for step in result.steps:
            assert (
                get_parent(db, step.step_thread_id)
                == result.strategy_thread_id
            )
            if step.transition is not None:
                assert (
                    get_parent(db, step.transition.thread_id)
                    == step.step_thread_id
                )
            for proposal in step.proposals:
                assert (
                    get_parent(db, proposal.proposal_thread_id)
                    == step.step_thread_id
                )
                assert (
                    get_parent(db, proposal.production.thread_id)
                    == proposal.proposal_thread_id
                )
                assert (
                    get_parent(db, proposal.evaluation_thread_id)
                    == proposal.proposal_thread_id
                )
                assert (
                    get_parent(db, proposal.aggregation.thread_id)
                    == proposal.evaluation_thread_id
                )
                assert all(
                    get_parent(db, case.thread_id)
                    == proposal.evaluation_thread_id
                    for case in proposal.cases
                )
    finally:
        db.conn.close()


def test_operation_audit_is_hidden_digest_only_and_replay_safe(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    flow_path = tmp_path / "flow.db"
    secret = "full secret candidate input must not be logged"
    request = StrategyRunInput(
        state=0,
        seed=Candidate(secret),
        cases=("private case",),
        max_steps=1,
    )
    first = _run(
        _runtime(
            threads_path,
            FakeStrategy(),
            FakeCandidateProducer(),
            ConcurrentCases(),
            OrderedAggregate(),
        ).produce(request),
        flow_path,
    )
    operation_ids = [
        operation.thread_id
        for step in first.steps
        for operation in (step.transition,)
        if operation is not None
    ]
    operation_ids += [
        operation.thread_id
        for step in first.steps
        for proposal in step.proposals
        for operation in (
            proposal.production,
            *proposal.cases,
            proposal.aggregation,
        )
        if operation is not None
    ]
    counts = {}
    for thread_id in operation_ids:
        messages = _audit_messages(threads_path, thread_id)
        counts[thread_id] = len(messages)
        assert len(messages) == 2
        assert all(message["no_api"] for message in messages)
        assert all(message["keep_user_turn"] for message in messages)
        assert "input_sha256=" in messages[0]["content"]
        assert secret not in messages[0]["content"]
        assert "private case" not in messages[0]["content"]
        assert "outcome=succeeded" in messages[1]["content"]
        assert "output_sha256=" in messages[1]["content"]

    replay = _run(
        _runtime(
            threads_path,
            FakeStrategy(),
            FakeCandidateProducer(),
            ConcurrentCases(),
            OrderedAggregate(),
        ).produce(request),
        flow_path,
    )
    assert replay == first
    assert {
        thread_id: len(_audit_messages(threads_path, thread_id))
        for thread_id in operation_ids
    } == counts


def test_fresh_runtime_replays_authoritative_refs_and_values(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    flow_path = tmp_path / "flow.db"
    request = StrategyRunInput(
        state=0,
        seed=Candidate("seed"),
        cases=("a", "b"),
        max_steps=1,
        max_concurrent_cases=2,
    )

    first_strategy = FakeStrategy()
    first_candidates = FakeCandidateProducer()
    first_cases = ConcurrentCases()
    first_aggregate = OrderedAggregate()
    first = _run(
        _runtime(
            threads_path,
            first_strategy,
            first_candidates,
            first_cases,
            first_aggregate,
        ).produce(request),
        flow_path,
    )
    first_threads = _threads(threads_path)

    second_strategy = FakeStrategy()
    second_candidates = FakeCandidateProducer()
    second_cases = ConcurrentCases()
    second_aggregate = OrderedAggregate()
    second = _run(
        _runtime(
            threads_path,
            second_strategy,
            second_candidates,
            second_cases,
            second_aggregate,
        ).produce(request),
        flow_path,
    )

    assert second == first
    assert _threads(threads_path) == first_threads
    assert second_strategy.calls == []
    assert second_candidates.calls == []
    assert second_cases.calls == []
    assert second_aggregate.calls == []


def test_runtime_input_rejects_invalid_case_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrent_cases must be positive"):
        StrategyRunInput(0, Candidate("seed"), max_concurrent_cases=0)
    with pytest.raises(TypeError, match="max_concurrent_cases must be an integer"):
        StrategyRunInput(0, Candidate("seed"), max_concurrent_cases=True)

@dataclass
class TwoProposalStrategy:
    inputs: list[StrategyInput[int]] = field(default_factory=list)

    def produce(self, operation: OperationInput[StrategyInput[int]]):
        value = operation.value
        self.inputs.append(value)
        if value.state == 0:
            return Advance(
                1,
                (
                    Proposal(instruction="fail"),
                    Proposal(instruction="keep"),
                ),
            )
        if value.state == 1:
            return Advance(2, (Proposal(instruction="fail"),))
        return Advance(3, (Proposal(instruction="later"),))


@dataclass
class FailingCandidateProducer(FakeCandidateProducer):
    def produce(self, operation: OperationInput[Proposal]):
        if operation.value.instruction == "fail":
            self.calls.append("fail")
            self.contexts.append(operation.context)
            return ItemFailure("candidate", "cannot produce", 1)
        return super().produce(operation)


@dataclass
class FailingCaseProducer:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def produce(self, operation: OperationInput[CaseRequest[str]]):
        request = operation.value
        self.calls.append((request.candidate.text, request.case))
        if request.candidate.text == "first" and request.case == "bad":
            return ItemFailure("case", "case failed", 1)
        return CaseEvidence(request.case)


@dataclass
class OneThenLaterStrategy:
    inputs: list[StrategyInput[int]] = field(default_factory=list)

    def produce(self, operation: OperationInput[StrategyInput[int]]):
        value = operation.value
        self.inputs.append(value)
        instruction = "first" if value.state == 0 else "later"
        return Advance(value.state + 1, (Proposal(instruction=instruction),))


def test_candidate_item_failure_skips_evaluation_and_continues(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    strategy = TwoProposalStrategy()
    candidates = FailingCandidateProducer()
    cases = ConcurrentCases()
    aggregate = OrderedAggregate()
    result = _run(
        _runtime(threads_path, strategy, candidates, cases, aggregate).produce(
            StrategyRunInput(
                0,
                Candidate("seed"),
                cases=("case",),
                max_steps=3,
            )
        ),
        tmp_path / "flow.db",
    )

    failed, kept = result.steps[1].proposals
    later = result.steps[3].proposals[0]
    assert failed.proposal_id == "P001"
    assert isinstance(failed.production.value, ItemFailure)
    assert failed.evaluation_thread_id is None
    assert failed.cases == ()
    assert failed.aggregation is None
    assert kept.proposal_id == "P002"
    assert kept.aggregation is not None
    assert result.steps[2].proposals[0].aggregation is None
    assert strategy.inputs[2].observations == ()
    assert later.proposal_id == "P004"
    assert later.aggregation is not None
    assert [item.candidate.text for item in strategy.inputs[1].observations] == [
        "keep"
    ]
    assert ("fail", "case") not in cases.calls
    names = [name for _, name in _threads(threads_path)]
    assert names.count("Evaluation") == 3


def test_case_item_failure_is_ordered_and_later_step_continues(
    tmp_path,
) -> None:
    threads_path = tmp_path / "threads.sqlite"
    strategy = OneThenLaterStrategy()
    cases = FailingCaseProducer()
    aggregate = OrderedAggregate()
    result = _run(
        _runtime(
            threads_path,
            strategy,
            FakeCandidateProducer(),
            cases,
            aggregate,
        ).produce(
            StrategyRunInput(
                0,
                Candidate("seed"),
                cases=("good", "bad", "last"),
                max_steps=2,
                max_concurrent_cases=2,
            )
        ),
        tmp_path / "flow.db",
    )

    failed = result.steps[1].proposals[0]
    assert [
        case.value.case_id
        if isinstance(case.value, CaseEvidence)
        else case.value.kind
        for case in failed.cases
    ] == ["good", "case", "last"]
    assert failed.aggregation is None
    assert strategy.inputs[1].observations == ()
    later = result.steps[2].proposals[0]
    assert later.aggregation is not None
    assert [item.case_id for item in later.aggregation.value.cases] == [
        "good",
        "bad",
        "last",
    ]
    assert aggregate.calls == [
        ("good", "bad", "last"),
        ("good", "bad", "last"),
    ]


def test_infrastructure_failure_is_audited_and_raised(tmp_path) -> None:
    @dataclass
    class BrokenCandidate:
        context = None
        calls = 0

        def produce(self, operation):
            self.calls += 1
            self.context = operation.context
            raise RuntimeError("provider unavailable")

    threads_path = tmp_path / "threads.sqlite"
    broken = BrokenCandidate()
    with pytest.raises(Exception, match="provider unavailable"):
        _run(
            _runtime(
                threads_path,
                FakeStrategy(),
                broken,
                ConcurrentCases(),
                OrderedAggregate(),
            ).produce(StrategyRunInput(0, Candidate("seed"))),
            tmp_path / "flow.db",
        )

    messages = _audit_messages(threads_path, broken.context.thread_id)
    assert len(messages) == 2
    assert "outcome=failed" in messages[-1]["content"]
    assert "failure_type=RuntimeError" in messages[-1]["content"]
    assert "provider unavailable" not in messages[-1]["content"]

    replay_broken = BrokenCandidate()
    with pytest.raises(Exception, match="provider unavailable"):
        _run(
            _runtime(
                threads_path,
                FakeStrategy(),
                replay_broken,
                ConcurrentCases(),
                OrderedAggregate(),
            ).produce(StrategyRunInput(0, Candidate("seed"))),
            tmp_path / "flow.db",
        )
    assert replay_broken.calls == 1
    assert len(_audit_messages(threads_path, broken.context.thread_id)) == 4
