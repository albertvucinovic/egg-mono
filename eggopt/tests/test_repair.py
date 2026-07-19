import asyncio
from dataclasses import dataclass, field
from pickle import dumps, loads

import pytest
from eggflow import FlowExecutor, Result, TaskError, TaskStore

from eggopt import (
    Accepted,
    ItemFailure,
    NeedsRepair,
    Producer,
    RepairFeedback,
    RepairInput,
)
from eggopt.eggflow_repair import RepairTask, RepairingProducer


@dataclass
class RecordingInner:
    calls: list[RepairInput[str]] = field(default_factory=list)

    def produce(self, value: RepairInput[str]) -> str:
        self.calls.append(value)
        return "invalid" if not value.feedback else "corrected"


@dataclass
class RecordingInspector:
    calls: list[str] = field(default_factory=list)

    def produce(self, value: str):
        self.calls.append(value)
        if value == "invalid":
            return NeedsRepair(RepairFeedback("fix the syntax"))
        return Accepted(value.upper())


def _run_task(task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def test_repair_reuses_same_inner_instance_with_cumulative_feedback(tmp_path) -> None:
    inner = RecordingInner()
    inner_instance_id = id(inner)
    inspector = RecordingInspector()
    producer = RepairingProducer(
        inner, "inner:v1", inspector, "inspect:v1", max_repairs=2
    )

    result = _run_task(producer.produce("source"), tmp_path / "flow.db")

    assert isinstance(producer, Producer)
    assert id(producer.inner) == inner_instance_id
    assert result == "CORRECTED"
    assert inner.calls == [
        RepairInput("source"),
        RepairInput("source", (RepairFeedback("fix the syntax"),)),
    ]
    assert inspector.calls == ["invalid", "corrected"]


def test_accepted_normalized_value_is_returned(tmp_path) -> None:
    inner = RecordingInner()
    inner.produce = lambda value: " raw "  # type: ignore[method-assign]
    inspector = RecordingInspector()
    inspector.produce = (  # type: ignore[method-assign]
        lambda value: Accepted(value.strip())
    )

    result = _run_task(
        RepairingProducer(
            inner, "inner:v1", inspector, "inspect:v1", max_repairs=0
        ).produce("source"),
        tmp_path / "flow.db",
    )

    assert result == "raw"


def test_exhausted_item_does_not_abort_batch(tmp_path) -> None:
    exhausted = RepairingProducer(
        ConstantInner("invalid"),
        "invalid-inner:v1",
        ConstantInspector(NeedsRepair(RepairFeedback("still invalid"))),
        "repair-inspect:v1",
        max_repairs=1,
    ).produce("bad item")
    accepted = RepairingProducer(
        ConstantInner("valid"),
        "valid-inner:v1",
        ConstantInspector(Accepted("accepted item")),
        "accept-inspect:v1",
        max_repairs=1,
    ).produce("good item")

    store = TaskStore(str(tmp_path / "flow.db"))
    try:
        results = asyncio.run(FlowExecutor(store).run([exhausted, accepted]))
    finally:
        store.conn.close()

    assert results == [
        ItemFailure(
            kind="repair_exhausted",
            reason="still invalid",
            attempts=2,
        ),
        "accepted item",
    ]


@pytest.mark.parametrize("stage", ["inner", "inspect"])
def test_terminal_subtask_becomes_item_failure(tmp_path, stage: str) -> None:
    inner = (
        RaisingProducer(terminal=True)
        if stage == "inner"
        else ConstantInner("output")
    )
    inspector = (
        RaisingInspector(terminal=True)
        if stage == "inspect"
        else ConstantInspector(Accepted("unused"))
    )
    producer = RepairingProducer(
        inner,
        f"{stage}-inner:v1",
        inspector,
        f"{stage}-inspect:v1",
        max_repairs=2,
    )

    result = _run_task(producer.produce("item"), tmp_path / f"{stage}.db")

    reason = "context exhausted" if stage == "inner" else "inspection terminal"
    assert result == ItemFailure(kind="terminal", reason=reason, attempts=1)


def test_nonterminal_subtask_remains_task_failure(tmp_path) -> None:
    producer = RepairingProducer(
        RaisingProducer(terminal=False),
        "failing-inner:v1",
        ConstantInspector(Accepted("unused")),
        "inspect:v1",
        max_repairs=2,
    )
    store = TaskStore(str(tmp_path / "flow.db"))
    try:
        with pytest.raises(TaskError, match="infrastructure failed"):
            asyncio.run(FlowExecutor(store).run(producer.produce("item")))
    finally:
        store.conn.close()


def test_cached_replay_uses_no_new_inner_or_inspector_calls(tmp_path) -> None:
    flow_path = tmp_path / "flow.db"
    first_inner = RecordingInner()
    first_inspector = RecordingInspector()
    first_result = _run_task(
        RepairingProducer(
            first_inner,
            "inner:v1",
            first_inspector,
            "inspect:v1",
            max_repairs=1,
        ).produce("source"),
        flow_path,
    )

    second_inner = RecordingInner()
    second_inspector = RecordingInspector()
    second_result = _run_task(
        RepairingProducer(
            second_inner,
            "inner:v1",
            second_inspector,
            "inspect:v1",
            max_repairs=1,
        ).produce("source"),
        flow_path,
    )

    assert second_result == first_result == "CORRECTED"
    assert len(first_inner.calls) == 2
    assert len(first_inspector.calls) == 2
    assert second_inner.calls == []
    assert second_inspector.calls == []


def test_attempt_and_inspection_boundaries_cache_independently(tmp_path) -> None:
    flow_path = tmp_path / "flow.db"
    inner = RecordingInner()
    failing_inspector = RaisingInspector()
    store = TaskStore(str(flow_path))
    try:
        with pytest.raises(TaskError, match="inspection infrastructure failed"):
            asyncio.run(
                FlowExecutor(store).run(
                    RepairingProducer(
                        inner,
                        "inner:v1",
                        failing_inspector,
                        "inspect:v1",
                        max_repairs=1,
                    ).produce("source")
                )
            )
    finally:
        store.conn.close()

    replacement_inner = RecordingInner()
    accepting_inspector = ConstantInspector(Accepted("recovered"))
    result = _run_task(
        RepairingProducer(
            replacement_inner,
            "inner:v1",
            accepting_inspector,
            "inspect:v2",
            max_repairs=1,
        ).produce("source"),
        flow_path,
    )

    assert result == "recovered"
    assert len(inner.calls) == 1
    assert failing_inspector.calls == 1
    assert replacement_inner.calls == []
    assert accepting_inspector.calls == 1


def test_wrong_inspection_output_is_programming_failure(tmp_path) -> None:
    producer = RepairingProducer(
        ConstantInner("output"),
        "inner:v1",
        ConstantInspector("not an inspection"),
        "inspect:v1",
        max_repairs=0,
    )
    store = TaskStore(str(tmp_path / "flow.db"))
    try:
        with pytest.raises(TaskError, match="Accepted or NeedsRepair"):
            asyncio.run(FlowExecutor(store).run(producer.produce("item")))
    finally:
        store.conn.close()


def test_repair_values_validation_pickle_and_task_keys() -> None:
    feedback = RepairFeedback("fix it")
    values = (
        feedback,
        Accepted({"normalized": True}),
        NeedsRepair(feedback),
        RepairInput("source", [feedback]),  # type: ignore[arg-type]
        ItemFailure("terminal", "context exhausted", 2),
    )
    assert loads(dumps(values)) == values
    assert values[3].feedback == (feedback,)

    inner = ConstantInner("output")
    inspector = ConstantInspector(Accepted("output"))
    base = RepairingProducer(
        inner, "inner:v1", inspector, "inspect:v1", 1
    ).produce("source")
    assert base.get_cache_key() != RepairingProducer(
        inner, "inner:v2", inspector, "inspect:v1", 1
    ).produce("source").get_cache_key()
    assert base.get_cache_key() != RepairingProducer(
        inner, "inner:v1", inspector, "inspect:v2", 1
    ).produce("source").get_cache_key()
    assert base.get_cache_key() != RepairingProducer(
        inner, "inner:v1", inspector, "inspect:v1", 2
    ).produce("source").get_cache_key()
    assert base.get_cache_key() != RepairingProducer(
        inner, "inner:v1", inspector, "inspect:v1", 1
    ).produce("other").get_cache_key()

    with pytest.raises(ValueError, match="text must not be empty"):
        RepairFeedback("")
    with pytest.raises(TypeError, match="feedback must be RepairFeedback"):
        NeedsRepair("fix")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="only RepairFeedback"):
        RepairInput("source", ("fix",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="kind must not be empty"):
        ItemFailure("", "reason", 0)
    with pytest.raises(ValueError, match="reason must not be empty"):
        ItemFailure("kind", "", 0)
    with pytest.raises(ValueError, match="attempts must be nonnegative"):
        ItemFailure("kind", "reason", -1)
    with pytest.raises(TypeError, match="inner must implement Producer"):
        RepairingProducer(
            object(),  # type: ignore[arg-type]
            "inner:v1",
            inspector,
            "inspect:v1",
            0,
        )
    with pytest.raises(ValueError, match="inner_identity must not be empty"):
        RepairingProducer(inner, "", inspector, "inspect:v1", 0)
    with pytest.raises(ValueError, match="inspect_identity must not be empty"):
        RepairingProducer(inner, "inner:v1", inspector, "", 0)
    with pytest.raises(TypeError, match="inspect must implement Producer"):
        RepairingProducer(
            inner,
            "inner:v1",
            object(),  # type: ignore[arg-type]
            "inspect:v1",
            0,
        )
    with pytest.raises(TypeError, match="inner_identity must be a string"):
        RepairingProducer(
            inner,
            1,  # type: ignore[arg-type]
            inspector,
            "inspect:v1",
            0,
        )
    with pytest.raises(TypeError, match="inspect_identity must be a string"):
        RepairingProducer(
            inner,
            "inner:v1",
            inspector,
            1,  # type: ignore[arg-type]
            0,
        )
    with pytest.raises(TypeError, match="max_repairs must be an integer"):
        RepairingProducer(
            inner,
            "inner:v1",
            inspector,
            "inspect:v1",
            "1",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="max_repairs must be nonnegative"):
        RepairingProducer(inner, "inner:v1", inspector, "inspect:v1", -1)

    class Unpickleable:
        def __reduce__(self):
            raise RuntimeError("cannot serialize")

    with pytest.raises(TypeError, match="original must be pickleable"):
        RepairingProducer(
            inner, "inner:v1", inspector, "inspect:v1", 0
        ).produce(Unpickleable()).get_cache_key()


@dataclass
class ConstantInner:
    value: str
    calls: int = 0

    def produce(self, value: RepairInput[str]) -> str:
        self.calls += 1
        return self.value


@dataclass
class ConstantInspector:
    inspection: object
    calls: int = 0

    def produce(self, value: str):
        self.calls += 1
        return self.inspection


@dataclass
class RaisingInspector:
    calls: int = 0
    terminal: bool = False

    def produce(self, value: str):
        self.calls += 1
        message = (
            "inspection terminal"
            if self.terminal
            else "inspection infrastructure failed"
        )
        raise TaskError(
            message,
            Result(error=message, terminal=self.terminal),
            terminal=self.terminal,
        )


@dataclass
class RaisingProducer:
    terminal: bool

    def produce(self, value: RepairInput[str]) -> str:
        if self.terminal:
            raise TaskError(
                "context exhausted",
                Result(error="context exhausted", terminal=True),
                terminal=True,
            )
        raise TaskError(
            "infrastructure failed",
            Result(error="infrastructure failed"),
        )
