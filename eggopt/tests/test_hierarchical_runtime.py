import asyncio
import threading
from dataclasses import dataclass, field
from pickle import dumps, loads

import pytest
from eggflow import FlowExecutor, Task, TaskStore
from eggthreads import ThreadsDB, get_parent

from eggopt import (
    Advance,
    Candidate,
    CaseEvidence,
    CaseRequest,
    Metric,
    Observation,
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

    def produce(self, value: StrategyInput[int]):
        self.calls.append(value)
        return Advance(1, (Proposal(instruction="first"),))


@dataclass
class FakeCandidateProducer:
    calls: list[str] = field(default_factory=list)

    def produce(self, value: Proposal) -> Candidate:
        self.calls.append(value.instruction)
        if value.instruction == "seed":
            return value.parents[0]
        return Candidate(value.instruction)


@dataclass
class ConcurrentCases:
    calls: list[tuple[str, str]] = field(default_factory=list)
    active: int = 0
    peak: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    async def produce(self, value: CaseRequest[str]) -> CaseEvidence:
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

    def produce(self, value: Observation) -> Observation:
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
    candidates = FakeCandidateProducer()
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
    assert [name for _, name in thread_rows] == [
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
    thread_ids = {thread_id for thread_id, _ in thread_rows}
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
