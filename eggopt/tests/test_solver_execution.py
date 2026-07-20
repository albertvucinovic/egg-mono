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
    build_tool_call_states,
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
)


@dataclass
class SolverDrive:
    threads_path: str = ""
    calls: list[SolverInput[str]] = field(default_factory=list)

    def produce(self, value: SolverInput[str]) -> str:
        if value.feedback:
            db = ThreadsDB(self.threads_path)
            db.init_schema()
            try:
                messages = _events(db, value.thread_id, "msg.create")
                assert messages[-1]["content"] == value.feedback[-1].text
                assert messages[-1]["role"] == "user"
                assert messages[-1].get("eggopt_repair_feedback") is True
                assert messages[-1].get("no_api") is not True
            finally:
                db.conn.close()
            assert value.trigger_message_id
        self.calls.append(value)
        return "print(40)" if not value.feedback else "print(42)"


@dataclass
class PythonInspection:
    calls: list[ExecutionInput[str]] = field(default_factory=list)

    def produce(self, value: ExecutionInput[str]):
        self.calls.append(value)
        execution = value.execution.python(
            value.candidate, key="candidate-python", cache_by=value.candidate
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
            value.execution.bash(
                value.candidate, key="candidate-bash", cache_by=value.candidate
            ),
            value.candidate,
        )


@dataclass
class BashInspectTask(Task):
    execution: object
    candidate: str

    def run(self):
        result = yield self.execution
        return Accepted(result.output)


def _setup(tmp_path, monkeypatch, solver=None, execution=None, max_repairs=1):
    monkeypatch.chdir(tmp_path)
    threads_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(threads_path)
    db.init_schema()
    parent = create_root_thread(db, "ItemParent")
    set_thread_tools_enabled(db, parent, True)
    db.conn.close()
    workspace = Path(".eggopt-test-workspaces") / uuid4().hex / "outerContext"
    execution = SolverExecution(
        threads_db_path=str(threads_path),
        parent_thread_id=parent,
        solver=solver or SolverDrive(str(threads_path)),
        execution=execution or PythonInspection(),
        solver_identity="solver:v1",
        execution_identity="execution:v1",
        solver_spec=SolverSpec(
            str(workspace / "innerContext"),
            system_prompt="Solve from feedback only.",
        ),
        execution_spec=ExecutionSpec(str(workspace)),
        max_repairs=max_repairs,
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
    solver = SolverDrive(str(tmp_path / "threads.sqlite"))
    execution_role = PythonInspection()
    threads_path, parent, composition = _setup(tmp_path, monkeypatch, solver, execution_role)

    result = _run(
        composition.produce(SolverExecutionRequest("item-1", "source")),
        tmp_path / "flow.db",
    )

    assert isinstance(composition, Producer)
    assert result == "print(42)"
    assert len(solver.calls) == 2
    assert len({call.thread_id for call in solver.calls}) == 1
    assert solver.calls[1].feedback == (RepairFeedback("print 42"),)
    assert len({call.thread_id for call in execution_role.calls}) == 1
    assert {call.attempt for call in execution_role.calls} == {0, 1}

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
        solver_sandbox = get_thread_sandbox_config(db, solver_id)
        execution_sandbox = get_thread_sandbox_config(db, execution_id)
        assert solver_sandbox.enabled and solver_sandbox.provider == "docker"
        assert execution_sandbox.enabled and execution_sandbox.provider == "docker"
        assert execution_sandbox.settings["network"] == "none"
        solver_messages = _events(db, solver_id, "msg.create")
        assert [message["role"] for message in solver_messages] == ["system", "user"]
    finally:
        db.conn.close()


def test_fresh_executor_replay_and_changed_input_reuses_pair(tmp_path, monkeypatch) -> None:
    first_solver = SolverDrive(str(tmp_path / "threads.sqlite"))
    first_execution = PythonInspection()
    threads_path, parent, first = _setup(tmp_path, monkeypatch, first_solver, first_execution)
    flow_path = tmp_path / "flow.db"
    request = SolverExecutionRequest("item-1", "source")
    expected = _run(first.produce(request), flow_path)

    replay_solver = SolverDrive(str(threads_path))
    replay_execution = PythonInspection()
    replay = SolverExecution(
        threads_db_path=str(threads_path), parent_thread_id=parent,
        solver=replay_solver, execution=replay_execution,
        solver_identity="solver:v1", execution_identity="execution:v1",
        solver_spec=first.solver_spec, execution_spec=first.execution_spec,
        max_repairs=1,
    )
    assert _run(replay.produce(request), flow_path) == expected
    assert replay_solver.calls == []
    assert replay_execution.calls == []

    changed_solver = SolverDrive(str(threads_path))
    changed_execution = PythonInspection()
    changed = SolverExecution(
        threads_db_path=str(threads_path), parent_thread_id=parent,
        solver=changed_solver, execution=changed_execution,
        solver_identity="solver:v1", execution_identity="execution:v1",
        solver_spec=first.solver_spec, execution_spec=first.execution_spec,
        max_repairs=1,
    )
    assert _run(
        changed.produce(SolverExecutionRequest("item-1", "changed source")),
        flow_path,
    ) == expected
    assert len(changed_solver.calls) == len(changed_execution.calls) == 2

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
    execution_role = BashInspection()
    threads_path, parent, composition = _setup(
        tmp_path, monkeypatch, solver=ConstantProducer("printf old"), execution=execution_role, max_repairs=0
    )
    flow_path = tmp_path / "flow.db"
    request = SolverExecutionRequest("item-1", "printf old")
    assert "old" in _run(composition.produce(request), flow_path)

    class ChangedInspection:
        def produce(self, value):
            return BashInspectTask(
                value.execution.bash(
                    "printf new", key="changed-check", cache_by="new"
                ),
                value.candidate,
            )

    changed = SolverExecution(
        threads_db_path=str(threads_path), parent_thread_id=parent,
        solver=SolverDrive(), execution=ChangedInspection(),
        solver_identity="solver:v1", execution_identity="execution:v2",
        solver_spec=composition.solver_spec,
        execution_spec=composition.execution_spec, max_repairs=0,
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
            threads_db_path=exhausted.threads_db_path,
            parent_thread_id=exhausted.parent_thread_id,
            solver=ConstantProducer("print(0)"),
            execution=ConstantProducer(NeedsRepair(RepairFeedback("invalid"))),
            solver_identity="solver:invalid",
            execution_identity="execution:invalid",
            solver_spec=exhausted.solver_spec,
            execution_spec=exhausted.execution_spec,
            max_repairs=0,
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
        assert execution_tools.llm_tools_enabled
        assert execution_tools.allowed_tools == {"python", "bash"}
        assert not solver_tools.is_tool_allowed("python")
        assert execution_tools.is_tool_allowed("python")
        assert get_thread_sandbox_config(db, solver_id).enabled
        assert get_thread_sandbox_config(db, execution_id).enabled
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

    _, _, composition = _setup(tmp_path, monkeypatch, execution=FailingInspection())
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


def test_execution_resumes_existing_call_without_duplicate(tmp_path, monkeypatch) -> None:
    import eggopt.solver_execution as module

    threads_path, parent, composition = _setup(
        tmp_path, monkeypatch, solver=ConstantProducer("printf ok"),
        execution=BashInspection(), max_repairs=0,
    )
    original = module.ThreadRunner.run_once
    crashed = False

    async def crash_after_execution(self):
        nonlocal crashed
        result = await original(self)
        states = build_tool_call_states(self.db, self.thread_id)
        if not crashed and any(state.finished_reason for state in states.values()):
            crashed = True
            raise RuntimeError("crash after tool execution")
        return result

    monkeypatch.setattr(module.ThreadRunner, "run_once", crash_after_execution)
    flow_path = tmp_path / "flow.db"
    request = SolverExecutionRequest("item-1", "source")
    with pytest.raises(TaskError, match="crash after tool execution"):
        _run(composition.produce(request), flow_path)

    monkeypatch.setattr(module.ThreadRunner, "run_once", original)
    assert "ok" in _run(composition.produce(request), flow_path)
    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        execution_id = _children(db, parent)[1]
        requests = [
            event for event in _events(db, execution_id, "msg.create")
            if event.get("tool_calls")
        ]
        assert len(requests) == 1
        assert len(_events(db, execution_id, "tool_call.finished")) == 1
    finally:
        db.conn.close()


def test_cache_by_invalidates_fixed_workspace_check(tmp_path, monkeypatch) -> None:
    threads_path, parent, composition = _setup(
        tmp_path, monkeypatch, solver=ConstantProducer("candidate"),
        execution=CacheBoundInspection("v1"), max_repairs=0,
    )
    flow_path = tmp_path / "flow.db"
    request = SolverExecutionRequest("item-1", "source")
    assert "fixed" in _run(composition.produce(request), flow_path)
    changed = SolverExecution(
        threads_db_path=str(threads_path), parent_thread_id=parent,
        solver=ConstantProducer("candidate"), execution=CacheBoundInspection("v2"),
        solver_identity="solver:v1", execution_identity="execution:v2",
        solver_spec=composition.solver_spec, execution_spec=composition.execution_spec,
        max_repairs=0,
    )
    assert "fixed" in _run(changed.produce(request), flow_path)
    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        assert len(_events(db, _children(db, parent)[1], "tool_call.finished")) == 2
    finally:
        db.conn.close()


@dataclass
class CacheBoundInspection:
    dependency: str

    def produce(self, value):
        return BashInspectTask(
            value.execution.bash(
                "printf fixed", key="workspace-check", cache_by=self.dependency
            ),
            value.candidate,
        )
