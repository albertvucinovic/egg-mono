# eggopt

`eggopt` is a domain-neutral Egg library for optimizing arbitrary
`Candidate(text)` values. Adapters own candidate meaning, cases, metrics,
effects, hidden inputs, and budgets.

See [`API.md`](API.md) for contracts and [`RUNNING.md`](RUNNING.md) for durable
execution and resume guidance.

## Install and test

```bash
python -m pip install -e './eggopt[dev,eggflow,eggthreads]'
python -m pytest eggopt/tests -q
```

## Persistent Solver + Execution

`SolverExecution` gives each item one persistent Solver conversation and one
persistent privileged Execution thread. A small task-backed execution role can
run real sandboxed Python or bash and request same-conversation repair:

```python
from eggflow import Task
from eggopt import Accepted, NeedsRepair, RepairFeedback
from eggopt.solver_execution import (
    ExecutionSpec, SolverExecution, SolverExecutionRequest, SolverSpec,
)

class Execute(Task):
    def __init__(self, request):
        self.request = request
    def run(self):
        result = yield self.request.execution.python(
            self.request.candidate,
            key="candidate-test",
            cache_by=self.request.candidate,
        )
        return (Accepted(self.request.candidate) if "42" in result.output else
                NeedsRepair(RepairFeedback("print 42")))

solve = SolverExecution(
    threads_db_path=".egg/threads.sqlite",
    parent_thread_id=item_parent_thread_id,
    solver=solver,
    execution=lambda request: Execute(request),
    solver_identity="solver:v1",
    execution_identity="candidate-test:v1",
    solver_spec=SolverSpec("run/outerContext/innerContext"),
    execution_spec=ExecutionSpec("run/outerContext"),
    max_repairs=2,
)
task = solve.produce(SolverExecutionRequest("item-1", source_input))
```

`Execution.python(...)` and `.bash(...)` require a semantic `key` and explicit
`cache_by` dependency (for example, a candidate or workspace-content hash).
Changed inputs rerun only affected work while preserving both thread IDs.
`Accepted` returns its typed value; exhaustion/context returns `ItemFailure`;
unrelated infrastructure failures remain Eggflow failures.

Other durable modules are `eggopt.eggflow`, `eggopt.eggflow_evaluation`, cached
run roots in `eggopt.eggthreads`, and the generic hierarchy in
`eggopt.eggthreads_runtime`. Cached typed values and explicit thread references,
not thread-name scans, are authoritative.
