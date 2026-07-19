import asyncio
from dataclasses import dataclass
from threading import Lock

import pytest
from eggflow import FlowExecutor, Task, TaskStore

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
    StrategyInput,
)
from eggopt.eggflow import EggflowProducer, ProduceTask


@dataclass
class CountingProducer:
    calls: int = 0

    def produce(self, value: StrategyInput[GEPAState]):
        self.calls += 1
        return GEPAStrategy(
            select_parents=FunctionProducer(lambda observations: observations),
            select_evidence=FunctionProducer(
                lambda observation: observation.cases
            ),
        ).produce(value)


def _strategy_input(text: str = "policy") -> StrategyInput[GEPAState]:
    case = CaseEvidence("case", feedback=(Feedback("selected example"),))
    observation = Observation(
        Candidate(text),
        cases=(case,),
        feedback=(Feedback("aggregate feedback"),),
    )
    return StrategyInput(GEPAState(max_generations=2), (observation,))


def test_eggflow_wrapper_is_runtime_producer_and_runs_gepa_transition(
    tmp_path,
) -> None:
    inner = CountingProducer()
    wrapper = EggflowProducer(inner, "gepa:first-parent:v1")

    task = wrapper.produce(_strategy_input())
    store = TaskStore(str(tmp_path / "flow.db"))
    try:
        decision = asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()

    assert isinstance(wrapper, Producer)
    assert isinstance(task, Task)
    assert isinstance(decision, Advance)
    assert decision.state == GEPAState(generation=1, max_generations=2)
    assert decision.proposals[0].evidence[0].case_id == "case"
    assert decision.proposals[0].feedback[0].text == "aggregate feedback"
    assert inner.calls == 1


def test_produce_task_flattens_task_result_and_replays_cache(tmp_path) -> None:
    first = TaskReturningProducer()
    flow_path = tmp_path / "flow.db"
    first_store = TaskStore(str(flow_path))
    try:
        first_result = asyncio.run(
            FlowExecutor(first_store).run(
                ProduceTask(first, "task-returning:v1", "value")
            )
        )
    finally:
        first_store.conn.close()

    second = TaskReturningProducer()
    second_store = TaskStore(str(flow_path))
    try:
        second_result = asyncio.run(
            FlowExecutor(second_store).run(
                ProduceTask(second, "task-returning:v1", "value")
            )
        )
    finally:
        second_store.conn.close()

    assert first_result == second_result == "task:value"
    assert first.calls == 1
    assert second.calls == 0


def test_produce_task_key_changes_with_identity_or_input() -> None:
    producer = CountingProducer()
    base = ProduceTask(producer, "gepa:v1", _strategy_input("first"))
    changed_identity = ProduceTask(
        producer, "gepa:v2", _strategy_input("first")
    )
    changed_input = ProduceTask(producer, "gepa:v1", _strategy_input("second"))

    assert base.get_cache_key() != changed_identity.get_cache_key()
    assert base.get_cache_key() != changed_input.get_cache_key()
    assert base.get_cache_key() == ProduceTask(
        CountingProducer(), "gepa:v1", _strategy_input("first")
    ).get_cache_key()


def test_flow_cache_result_round_trips_after_store_reopen(tmp_path) -> None:
    db_path = tmp_path / "flow.db"
    value = _strategy_input()
    first_producer = CountingProducer()
    first_store = TaskStore(str(db_path))
    try:
        first_result = asyncio.run(
            FlowExecutor(first_store).run(
                ProduceTask(first_producer, "gepa:first-parent:v1", value)
            )
        )
    finally:
        first_store.conn.close()

    second_producer = CountingProducer()
    second_store = TaskStore(str(db_path))
    try:
        second_result = asyncio.run(
            FlowExecutor(second_store).run(
                ProduceTask(second_producer, "gepa:first-parent:v1", value)
            )
        )
    finally:
        second_store.conn.close()

    assert isinstance(first_result, Advance)
    assert second_result == first_result
    assert second_result.state == GEPAState(generation=1, max_generations=2)
    assert first_producer.calls == 1
    assert second_producer.calls == 0


def test_invalid_configuration_and_unpickleable_input_fail_clearly() -> None:
    producer = FunctionProducer(lambda value: value)

    with pytest.raises(TypeError, match="producer must implement Producer"):
        ProduceTask(object(), "identity", "value")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="producer must implement Producer"):
        EggflowProducer(object(), "identity")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="producer_identity must be a string"):
        ProduceTask(producer, 1, "value")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="producer_identity must not be empty"):
        EggflowProducer(producer, "")

    task = ProduceTask(producer, "identity", Lock())
    with pytest.raises(TypeError, match="value must be pickleable"):
        task.get_cache_key()

    class CustomPickleFailure:
        def __reduce__(self):
            raise RuntimeError("custom serialization failed")

    task = ProduceTask(producer, "identity", CustomPickleFailure())
    with pytest.raises(TypeError, match="value must be pickleable"):
        task.get_cache_key()


@dataclass
class ValueTask(Task):
    value: str

    def run(self) -> str:
        return f"task:{self.value}"


@dataclass
class TaskReturningProducer:
    calls: int = 0

    def produce(self, value: str) -> Task:
        self.calls += 1
        return ValueTask(value)
