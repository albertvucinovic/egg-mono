import asyncio
import json
from dataclasses import dataclass, field

from eggflow import FlowExecutor, TaskStore
from eggthreads import ThreadsDB, get_parent

from eggopt import (
    Candidate,
    CaseEvidence,
    Feedback,
    FunctionProducer,
    GEPAStrategy,
    GEPAState,
    Observation,
    OperationContext,
    OperationInput,
    Stop,
    StrategyInput,
)
from eggopt.eggthreads_runtime import ContextualGEPAStrategy, OperationTask


@dataclass
class ParentSelector:
    calls: int = 0

    def produce(self, operation: OperationInput[tuple[Observation, ...]]):
        self.calls += 1
        return tuple(reversed(operation.value))


@dataclass
class EvidenceSelector:
    calls: list[str] = field(default_factory=list)

    def produce(self, operation: OperationInput[Observation]):
        self.calls.append(operation.value.candidate.text)
        return tuple(reversed(operation.value.cases))


def _run(task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def _new_parent(db_path):
    from eggthreads import create_root_thread

    db = ThreadsDB(db_path)
    try:
        db.init_schema()
        return create_root_thread(db, name="StrategyTransition")
    finally:
        db.conn.close()


def _rows(db_path):
    db = ThreadsDB(db_path)
    try:
        db.init_schema()
        rows = db.conn.execute(
            "SELECT thread_id, name FROM threads ORDER BY rowid"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]
    finally:
        db.conn.close()


def _audit_count(db_path):
    db = ThreadsDB(db_path)
    try:
        db.init_schema()
        count = 0
        for thread_id, _ in _rows(db_path):
            for event in db.events_since(thread_id, -1):
                if event["type"] != "msg.create":
                    continue
                payload = json.loads(event["payload_json"])
                count += bool(payload.get("eggopt_operation_audit"))
        return count
    finally:
        db.conn.close()


def test_contextual_gepa_selector_children_match_pure_decision(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    transition_id = _new_parent(threads_path)
    first = Observation(
        Candidate("first"),
        cases=(CaseEvidence("f-good"), CaseEvidence("f-bad")),
        feedback=(Feedback("first feedback"),),
    )
    second = Observation(
        Candidate("second"),
        cases=(CaseEvidence("s-good"), CaseEvidence("s-bad")),
        feedback=(Feedback("second feedback"),),
    )
    value = StrategyInput(GEPAState(max_generations=2), (first, second))
    parents = ParentSelector()
    evidence = EvidenceSelector()
    contextual = ContextualGEPAStrategy(
        str(threads_path),
        parents,
        "parents:v1",
        evidence,
        "evidence:v1",
        instruction="reflect",
        proposals_per_parent=2,
    )
    task = OperationTask(
        str(threads_path),
        transition_id,
        "StrategyTransition",
        contextual,
        "contextual-gepa:v1",
        value,
        transition_id,
    )
    changed = ContextualGEPAStrategy(
        str(threads_path),
        parents,
        "parents:v2",
        evidence,
        "evidence:v1",
        instruction="reflect",
        proposals_per_parent=2,
    ).produce(
        OperationInput(
            OperationContext(transition_id, "StrategyTransition"), value
        )
    )
    base = contextual.produce(
        OperationInput(OperationContext(transition_id, "StrategyTransition"), value)
    )
    assert base.get_cache_key() != changed.get_cache_key()

    decision = _run(task, tmp_path / "flow.db").value
    pure = GEPAStrategy(
        FunctionProducer(lambda observations: tuple(reversed(observations))),
        FunctionProducer(lambda parent: tuple(reversed(parent.cases))),
        instruction="reflect",
        proposals_per_parent=2,
    ).produce(value)
    assert decision == pure
    assert parents.calls == 1
    assert evidence.calls == ["second", "first"]

    rows = _rows(threads_path)
    assert [name for _, name in rows] == [
        "StrategyTransition",
        "ParentSelection",
        "EvidenceSelection 000",
        "EvidenceSelection 001",
    ]
    db = ThreadsDB(threads_path)
    try:
        db.init_schema()
        assert all(
            get_parent(db, thread_id) == transition_id
            for thread_id, _ in rows[1:]
        )
    finally:
        db.conn.close()

    audit_count = _audit_count(threads_path)
    replay_parents = ParentSelector()
    replay_evidence = EvidenceSelector()
    replay_task = OperationTask(
        str(threads_path),
        transition_id,
        "StrategyTransition",
        ContextualGEPAStrategy(
            str(threads_path),
            replay_parents,
            "parents:v1",
            replay_evidence,
            "evidence:v1",
            instruction="reflect",
            proposals_per_parent=2,
        ),
        "contextual-gepa:v1",
        value,
        transition_id,
    )
    assert _run(replay_task, tmp_path / "flow.db").value == pure
    assert replay_parents.calls == 0
    assert replay_evidence.calls == []
    assert _rows(threads_path) == rows
    assert _audit_count(threads_path) == audit_count


def test_contextual_gepa_preserves_preselection_stop_behavior(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    transition_id = _new_parent(threads_path)
    parents = ParentSelector()
    evidence = EvidenceSelector()
    value = StrategyInput(GEPAState(max_generations=1))
    strategy = ContextualGEPAStrategy(
        str(threads_path),
        parents,
        "parents:v1",
        evidence,
        "evidence:v1",
    )

    decision = strategy.produce(
        OperationInput(
            OperationContext(transition_id, "StrategyTransition"), value
        )
    )

    assert decision == Stop(value.state, "no observations available")
    assert parents.calls == 0
    assert evidence.calls == []
    assert _rows(threads_path) == [(transition_id, "StrategyTransition")]
