import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
from eggflow import FlowExecutor, Result, Task, TaskError, TaskStore
from eggflow.eggthreads_tasks import ContextLimitExceededError
from eggthreads import (
    ThreadsDB,
    create_root_thread,
    get_parent,
    get_thread_sandbox_config,
    get_thread_tools_config,
    set_thread_tools_enabled,
)

from eggopt import (
    Accepted,
    ItemFailure,
    NeedsRepair,
    Producer,
    RepairFeedback,
)
from eggopt.solver_execution import (
    ExecutionInput,
    ExecutionSpec,
    SolverExecution,
    SolverExecutionRequest,
    SolverInput,
    SolverSpec,
    ToolCall,
)


@dataclass
class SolverDrive:
    calls: list[SolverInput[str]] = field(default_factory=list)

    def produce(self, value: SolverInput[str]) -> str:
        self.calls.append(value)
        return "print(40)" if not value.feedback else "print(42)"


@dataclass
class PythonInspection:
    calls: list[ExecutionInput[str]] = field(default_factory=list)

    def produce(self, value: ExecutionInput[str]):
        self.calls.append(value)
        execution = value.execute.produce(
            ToolCall("python", value.candidate, "candidate-python")
        )
        return InspectTask(execution, value.candidate)


@dataclass
class InspectTask(Task):
    execution: object
    candidate: str

    def run(self):
        result = yield self.execution
        if "42" not in result.output:
            return NeedsRepair(RepairFeedback("print 42"))
        return Accepted(self.candidate)


@dataclass
class BashInspection:
    calls: int = 0

    def produce(self, value: ExecutionInput[str]):
        self.calls += 1
        return BashInspectTask(
            value.execute.produce(ToolCall("bash", value.candidate, "candidate-bash")),
            value.candidate,
        )


@dataclass
class BashInspectTask(Task):
    execution: object
    candidate: str

    def run(self):
        result = yield self.execution
        return Accepted(result.output)


def _setup(tmp_path, monkeypatch, solver=None, inspect=None, max_repairs=1):
    monkeypatch.chdir(tmp_path)
    threads_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(threads_path)
    db.init_schema()
    parent = create_root_thread(db, "ItemParent")
    set_thread_tools_enabled(db, parent, True)
    db.conn.close()
    workspace = Path(".eggopt-test-workspaces") / uuid4().hex / "outerContext"
    execution = SolverExecution(
        str(threads_path),
        parent,
        SolverSpec(system_prompt="Solve from feedback only."),
        ExecutionSpec(str(workspace)),
        solver or SolverDrive(),
        "solver:v1",
        inspect or PythonInspection(),
        "inspect:v1",
        max_repairs,
    )
    return threads_path, parent, execution


def _run(task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def _children(db, parent):
    return [row[0] for row in db.conn.execute(
        "SELECT child_id FROM children WHERE parent_id=? ORDER BY rowid", (parent,)
    )]


def _events(db, thread_id, event_type):
    return [
        json.loads(event["payload_json"])
        for event in db.events_since(thread_id, -1)
        if event["type"] == event_type
    ]


def test_exact_pair_real_sandbox_history_and_same_solver_repair(tmp_path, monkeypatch) -> None:
    solver = SolverDrive()
    inspect = PythonInspection()
    threads_path, parent, composition = _setup(tmp_path, monkeypatch, solver, inspect)

    result = _run(
        composition.produce(SolverExecutionRequest("item-1", "source")),
        tmp_path / "flow.db",
    )

    assert isinstance(composition, Producer)
    assert result == "print(42)"
    assert len(solver.calls) == 2
    assert len({call.thread_id for call in solver.calls}) == 1
    assert solver.calls[1].feedback == (RepairFeedback("print 42"),)
    assert len({call.thread_id for call in inspect.calls}) == 1
    assert {call.attempt for call in inspect.calls} == {0, 1}

    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        children = _children(db, parent)
        assert len(children) == 2
        solver_id, execution_id = children
        assert db.get_thread(solver_id).name == "Solver"
        assert db.get_thread(execution_id).name == "Execution"
        assert get_parent(db, solver_id) == parent
        assert get_parent(db, execution_id) == parent
        assert not _children(db, solver_id)
        assert not _children(db, execution_id)

        calls = _events(db, execution_id, "msg.create")
        tool_requests = [event for event in calls if event.get("tool_calls")]
        tool_results = [event for event in calls if event.get("role") == "tool"]
        assert len(tool_requests) == len(tool_results) == 2
        assert all(
            event["tool_calls"][0]["function"]["name"] == "python"
            for event in tool_requests
        )
        assert "40" in tool_results[0]["content"]
        assert "42" in tool_results[1]["content"]
        assert len(_events(db, execution_id, "tool_call.finished")) == 2
        sandbox = get_thread_sandbox_config(db, execution_id)
        assert sandbox.enabled and sandbox.provider == "docker"
        assert sandbox.settings["network"] == "none"
    finally:
        db.conn.close()


def test_fresh_executor_replay_and_changed_input_reuses_pair(tmp_path, monkeypatch) -> None:
    first_solver = SolverDrive()
    first_inspect = PythonInspection()
    threads_path, parent, first = _setup(tmp_path, monkeypatch, first_solver, first_inspect)
    flow_path = tmp_path / "flow.db"
    request = SolverExecutionRequest("item-1", "source")
    expected = _run(first.produce(request), flow_path)

    replay_solver = SolverDrive()
    replay_inspect = PythonInspection()
    replay = SolverExecution(
        str(threads_path),
        parent,
        first.solver_spec,
        first.execution_spec,
        replay_solver,
        "solver:v1",
        replay_inspect,
        "inspect:v1",
        1,
    )
    assert _run(replay.produce(request), flow_path) == expected
    assert replay_solver.calls == []
    assert replay_inspect.calls == []

    changed_solver = SolverDrive()
    changed_inspect = PythonInspection()
    changed = SolverExecution(
        str(threads_path),
        parent,
        first.solver_spec,
        first.execution_spec,
        changed_solver,
        "solver:v1",
        changed_inspect,
        "inspect:v1",
        1,
    )
    assert _run(
        changed.produce(SolverExecutionRequest("item-1", "changed source")),
        flow_path,
    ) == expected
    assert len(changed_solver.calls) == len(changed_inspect.calls) == 2

    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        children = _children(db, parent)
        assert len(children) == 2
        execution_id = children[1]
        assert len(_events(db, execution_id, "tool_call.finished")) == 4
    finally:
        db.conn.close()


def test_check_identity_or_script_change_invalidates_only_execution(tmp_path, monkeypatch) -> None:
    solver = SolverDrive()
    inspect = BashInspection()
    threads_path, parent, composition = _setup(
        tmp_path, monkeypatch, solver=ConstantProducer("printf old"), inspect=inspect, max_repairs=0
    )
    flow_path = tmp_path / "flow.db"
    request = SolverExecutionRequest("item-1", "printf old")
    assert "old" in _run(composition.produce(request), flow_path)

    class ChangedInspection:
        def produce(self, value):
            return BashInspectTask(
                value.execute.produce(ToolCall("bash", "printf new", "changed-check")),
                value.candidate,
            )

    changed = SolverExecution(
        str(threads_path),
        parent,
        composition.solver_spec,
        composition.execution_spec,
        SolverDrive(),
        "solver:v1",
        ChangedInspection(),
        "inspect:v2",
        0,
    )
    assert "new" in _run(changed.produce(request), flow_path)

    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        assert len(_children(db, parent)) == 2
        assert len(_events(db, _children(db, parent)[1], "tool_call.finished")) == 2
    finally:
        db.conn.close()


def test_capability_isolation_and_item_failures(tmp_path, monkeypatch) -> None:
    threads_path, parent, exhausted = _setup(tmp_path, monkeypatch)
    result = _run(
        SolverExecution(
            exhausted.threads_db_path,
            exhausted.parent_thread_id,
            exhausted.solver_spec,
            exhausted.execution_spec,
            ConstantProducer("print(0)"),
            "solver:invalid",
            ConstantProducer(NeedsRepair(RepairFeedback("invalid"))),
            "inspect:invalid",
            0,
        ).produce(SolverExecutionRequest("bad", "source")),
        tmp_path / "flow.db",
    )
    assert result == ItemFailure("repair_exhausted", "invalid", 1)

    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        solver_id, execution_id = _children(db, parent)
        solver_tools = get_thread_tools_config(db, solver_id)
        execution_tools = get_thread_tools_config(db, execution_id)
        assert not solver_tools.llm_tools_enabled
        assert solver_tools.allowed_tools == set()
        assert not execution_tools.llm_tools_enabled
        assert execution_tools.allowed_tools == {"python", "bash"}
    finally:
        db.conn.close()


@dataclass
class ConstantProducer:
    result: object

    def produce(self, value):
        return self.result


def test_nonterminal_infrastructure_failure_propagates(tmp_path, monkeypatch) -> None:
    class FailingInspection:
        def produce(self, value):
            raise TaskError("execution unavailable", Result(error="execution unavailable"))

    _, _, composition = _setup(tmp_path, monkeypatch, inspect=FailingInspection())
    with pytest.raises(TaskError, match="execution unavailable"):
        _run(
            composition.produce(SolverExecutionRequest("item-1", "source")),
            tmp_path / "flow.db",
        )



def test_context_terminal_becomes_item_failure(tmp_path, monkeypatch) -> None:
    class TerminalSolver:
        def produce(self, value):
            raise ContextLimitExceededError("context exhausted")

    _, _, composition = _setup(tmp_path, monkeypatch, solver=TerminalSolver())
    result = _run(
        composition.produce(SolverExecutionRequest("item-1", "source")),
        tmp_path / "flow.db",
    )
    assert result == ItemFailure("terminal", "context exhausted", 1)
