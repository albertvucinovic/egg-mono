import asyncio
from dataclasses import dataclass

from eggflow import FlowExecutor, Task, TaskStore
from eggthreads import ThreadsDB, create_root_thread, set_thread_tools_enabled

from eggopt import Accepted, NeedsRepair, RepairFeedback
from eggopt.solver_execution import (
    ExecutionSpec,
    SolverExecution,
    SolverExecutionRequest,
    SolverSpec,
)


@dataclass
class Solver:
    def produce(self, request):
        return "print(42)" if request.feedback else "print(0)"


@dataclass
class ExecutionRole:
    def produce(self, request):
        return Inspect(request.execution.python(
            request.candidate,
            key="candidate-test",
            cache_by=request.candidate,
        ), request.candidate)


@dataclass
class Inspect(Task):
    task: Task
    candidate: str

    def run(self):
        result = yield self.task
        if "42" not in result.output:
            return NeedsRepair(RepairFeedback("print 42"))
        return Accepted(self.candidate)


def test_solver_execution_end_to_end_and_replay(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    threads_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(threads_path)
    db.init_schema()
    parent = create_root_thread(db, "Item")
    set_thread_tools_enabled(db, parent, True)
    db.conn.close()

    pair = SolverExecution(
        threads_db_path=str(threads_path),
        parent_thread_id=parent,
        solver=Solver(),
        execution=ExecutionRole(),
        solver_identity="solver:v1",
        execution_identity="execution:v1",
        solver_spec=SolverSpec("run/outerContext/innerContext"),
        execution_spec=ExecutionSpec("run/outerContext"),
        max_repairs=1,
    )
    task = pair.produce(SolverExecutionRequest("item", "source"))
    flow_path = tmp_path / "flow.db"
    store = TaskStore(str(flow_path))
    try:
        first = asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()
    store = TaskStore(str(flow_path))
    try:
        replay = asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()
    assert first == replay == "print(42)"
