# Running an Eggopt hierarchy

Eggopt is a library, not a standalone optimizer CLI. A domain supplies typed
Producers and owns candidate meaning, cases, effects, budgets, model clients,
and any validation/repair policy.

## Install locally

From the Egg monorepo root:

```bash
python -m pip install -e './eggopt[eggflow,eggthreads]'
```

A consuming project can use an editable path dependency instead. Refresh that
project's environment after changing Eggopt so Python does not keep a stale
installed copy.

## Durable run directory

Use one directory per experiment:

```text
my-run/
├── flow.db
└── .egg/
    └── threads.sqlite
```

`flow.db` is Eggflow's cache/control authority. `threads.sqlite` contains the
inspectable physical hierarchy. Cached typed results and their returned thread
IDs are authoritative; do not recover work by scanning thread names.

## Minimal hierarchy invocation

The following compact example uses deterministic contextual Producers. Real
domains generally use a contextual setup Producer to load cases and task-backed
Producers for effects.

```python
import asyncio
from dataclasses import dataclass
from pathlib import Path

from eggflow import FlowExecutor, TaskStore
from eggopt import (
    Advance, Candidate, CaseEvidence, Observation, OperationInput,
    Proposal, StrategyInput, StrategyRunInput,
)
from eggopt.eggthreads_runtime import HierarchicalRuntime


@dataclass
class Strategy:
    def produce(self, op: OperationInput[StrategyInput[int]]):
        return Advance(op.value.state + 1, (Proposal(instruction="revise"),))


@dataclass
class CandidateProducer:
    def produce(self, op: OperationInput[Proposal]):
        proposal = op.value
        return proposal.parents[0] if proposal.instruction == "seed" else Candidate("revised")


@dataclass
class CaseProducer:
    def produce(self, op: OperationInput):
        return CaseEvidence(str(op.value.case))


@dataclass
class Aggregate:
    def produce(self, op: OperationInput[Observation]):
        return op.value


root = Path("my-run")
(root / ".egg").mkdir(parents=True, exist_ok=True)
runtime = HierarchicalRuntime(
    str(root / ".egg" / "threads.sqlite"),
    Strategy(), "strategy:v1",
    CandidateProducer(), "candidate:v1",
    CaseProducer(), "case:v1",
    Aggregate(), "aggregate:v1",
)
task = runtime.produce(
    StrategyRunInput(
        state=0,
        seed=Candidate("base"),
        cases=("case-a", "case-b"),
        max_steps=1,
        max_concurrent_cases=2,
    )
)
store = TaskStore(str(root / "flow.db"))
try:
    result = asyncio.run(FlowExecutor(store).run(task))
finally:
    store.conn.close()
print(result.study_thread_id, result.final_state)
```

The physical seed is `StudyRoot/StrategyRunRoot/RunSetup/Step S000/Proposal
P000`; later steps and proposals are serial. Case operations may run up to
`max_concurrent_cases` concurrently, but returned case evidence and aggregation
remain in request order.

## Resume, replay, and new experiments

Rerun the same top-level task with:

- the same `flow.db` and `threads.sqlite`;
- equal `StrategyRunInput`;
- unchanged Producer identities and configuration.

Eggflow returns completed cached values without invoking Producers, creating
threads, or appending audits. An interrupted/failed nonterminal task resumes
through Eggflow's normal failed-task recovery; completed predecessors remain
cached. `ItemFailure` is an item outcome, while infrastructure exceptions remain
flow failures.

Change an identity whenever code or behavior, model/tool/sandbox settings,
selection policy, or other semantic configuration changes. Prefer a **new run
directory** for a genuinely new experiment. Reusing a directory with changed
identities intentionally creates new cached work alongside old evidence; it is
not a clean experiment reset. Never delete only one of the two databases and
call that a resume.

## Inspection

The generic, reliable inspection method is to point an Eggthreads reader at
`my-run/.egg/threads.sqlite`. EggW supports an explicit database environment:

```bash
export EGG_DB_PATH="$PWD/my-run/.egg/threads.sqlite"
export EGG_CWD="$PWD/my-run"
hypercorn eggw.main:app --bind 127.0.0.1:8000
```

Then use the EggW frontend as documented by that package. The terminal `egg`
client uses `.egg/threads.sqlite` relative to its current directory, so run it
from `my-run/` when the Egg installation is available. Inspection tools must
not become orchestration authority.

## Domain-owned runtimes

A domain may replace `HierarchicalRuntime` with any structural
`Producer[StrategyRunInput, Task]`. It may also reuse public `OperationTask` to
create audited contextual operation children. Keep live clients, schedulers,
and open databases outside pickled task values and reconstruct them after
restart.

## Persistent Solver/Execution items

Create the authoritative item parent once, import `SolverExecution` from
`eggopt.solver_execution`, then construct
`SolverExecution(...).produce(SolverExecutionRequest(item_id, value))`. Run the
returned task through `FlowExecutor(TaskStore("flow.db"))`. Resume with the
same `flow.db`, `threads.sqlite`, parent ID, item ID, specs, identities, and
request value. A fresh executor then returns cached work without model/inspector
calls, child creation, or repeated tool execution.

The configured Execution working directory must be under the process working
directory (Eggthreads' filesystem boundary). Execution is sandbox-enabled and
restricted to its explicit Python/bash allowlist; Solver has no tool capability
and sees only original input plus sanitized repair feedback supplied by the
client drive. Inspect the two persistent children and Execution's durable
`tool_call.*` plus tool `msg.create` events through EggW or Eggthreads APIs.
Change a semantic identity when solver, inspection, command, sandbox, or tool
behavior changes; changed work is cached separately while the item keeps its
same two thread IDs.
