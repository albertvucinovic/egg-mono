# eggopt API

`eggopt` is domain-neutral. Its pure core is dependency-free; optional durable
composition uses Eggflow/Eggthreads. Candidate meaning, cases, metrics, effects,
safety policy, hidden information, and budgets belong to adapters. Every semantic
role is a typed `Producer[Input, Output]`.

## Pure concepts

- `Candidate(text)`; `Producer` / `FunctionProducer`.
- `Metric`, `Feedback`, `CaseEvidence`, `Observation`.
- `StrategyInput`, `Proposal`, `Advance | Stop`.
- `GEPAState` / `GEPAStrategy`; `PhysicsState` / `PhysicsStrategy`.
- `RepairFeedback`, `Accepted | NeedsRepair`, `ItemFailure`.
- `CaseRequest`, `EvaluationRequest`.
- `StrategyRunInput`, operation context/results, proposal/step/run results.

## SolverExecution

`SolverExecution(...).produce(SolverExecutionRequest(item_id, value))` returns one
durable Eggflow task; import it from `eggopt.solver_execution`. It composes:

- `SolverSpec`: pickle-safe Solver name, system prompt, and optional model key.
- `ExecutionSpec`: explicit working directory, sandbox settings, and the
  Python/bash capability allowlist.
- `SolverInput`: authoritative Solver thread ID, original input, cumulative
  sanitized feedback, and attempt number.
- `ExecutionInput`: authoritative Execution thread ID, candidate, attempt, and
  `execute`, a Producer accepting `ToolCall(tool, script, identity)`.
- `ExecutionResult`: thread/tool-call IDs and actual published tool output.

One cached pair of sibling Solver and Execution children is created per
`item_id` under the supplied authoritative parent. Attempts and checks share
those IDs. Solver attempts key on explicit identity, item digest, feedback, and
attempt. Tool work additionally keys on explicit check identity, tool, script
digest, and attempt. Execution records real Eggthreads tool-call and output
events in its existing thread; no per-attempt validation child exists.

Inspection returns `Accepted(value)` or `NeedsRepair(RepairFeedback)`. Repair
continues in the same Solver conversation. Exhaustion or recognized terminal
context returns `ItemFailure`; nonterminal infrastructure failures propagate.

## Optional modules

- `eggopt.eggflow`: generic `ProduceTask` / `EggflowProducer`.
- `eggopt.eggflow_evaluation`: cached case map and aggregation.
- `eggopt.eggthreads`: cached `CreateRunRoots` / `RunRoots`.
- `eggopt.eggthreads_runtime`: `OperationTask`, `HierarchicalRuntime`, and
  `ContextualGEPAStrategy`.

## Boundaries

- No trading, ARC, prompt-only, game, or artifact semantics.
- No first-class Check/Constraint or mandatory objective/archive/Pareto layer.
- Caller-owned identities change with behavior/configuration.
- Live clients, schedulers, and DB connections stay out of cached values.
- Cached typed values and explicit thread IDs are authoritative; never recover
  work by scanning names.
