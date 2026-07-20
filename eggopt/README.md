# eggopt

`eggopt` is a domain-neutral Egg library for composing optimization work over
arbitrary `Candidate(text)` values. The core supplies typed Producers,
evidence, and strategy transitions; adapters own all candidate meaning,
cases, metrics, effects, hidden inputs, and budgets.

See [`API.md`](API.md) for contracts and [`RUNNING.md`](RUNNING.md) for durable
execution and resume guidance.

## Install and test

```bash
python -m pip install -e './eggopt[dev,eggflow,eggthreads]'
python -m pytest eggopt/tests -q
```

## Persistent Solver + Execution

`SolverExecution` gives one item exactly one persistent restricted Solver
conversation and one persistent privileged Execution thread. The inspector can
run real sandboxed Python/bash operations in that Execution thread and return
`Accepted` or sanitized `NeedsRepair` feedback:

```python
from eggflow import Task
from eggopt import Accepted, NeedsRepair, RepairFeedback
from eggopt.solver_execution import (
    ExecutionSpec, SolverExecution, SolverExecutionRequest, SolverSpec, ToolCall,
)

class Solver:
    def produce(self, request):
        return "print(42)" if request.feedback else "print(40)"

class InspectTask(Task):
    def __init__(self, execution):
        self.execution = execution

    def run(self):
        result = yield self.execution
        if "42" not in result.output:
            return NeedsRepair(RepairFeedback("print 42"))
        return Accepted("print(42)")

class Inspect:
    def produce(self, request):
        return InspectTask(request.execute.produce(
            ToolCall("python", request.candidate, "candidate-test:v1")
        ))

solve = SolverExecution(
    ".egg/threads.sqlite", item_parent_thread_id,
    SolverSpec(system_prompt="Repair only from supplied feedback."),
    ExecutionSpec("runs/item-1/outerContext"),
    Solver(), "solver:v1",
    Inspect(), "inspect:v1",
    max_repairs=2,
)
task = solve.produce(SolverExecutionRequest("item-1", source_input))
```

The common call is one construction plus `.produce(request)`. Solver attempts,
inspection work, and each `ToolCall` have explicit cache identities. Changed
item/check input reruns only affected work while reusing both thread IDs.
Accepted normalized values return directly; exhaustion/context returns
`ItemFailure`; unrelated infrastructure failures remain Eggflow failures.

## Other composition

- `eggopt.eggflow`: cache any synchronous Producer with caller-owned identity.
- `eggopt.eggflow_evaluation`: cached ordered case map and aggregation.
- `eggopt.eggthreads`: cached Study/Strategy run roots.
- `eggopt.eggthreads_runtime`: the generic hierarchical optimization runtime.

Do not pickle live clients, schedulers, or open databases. Cached typed values
and explicit thread references—not thread-name scans—are authoritative.
