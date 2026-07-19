# eggopt API

`eggopt` is a domain-neutral optimization substrate. Candidate meaning, cases,
metrics, safety policy, effects, objectives, and budgets belong to adapters.
Every semantic role—strategy, mutation, case evaluation, aggregation,
inspection, or judging—is a typed `Producer[Input, Output]`.

## Stable pure concepts

- `Candidate(text)` — the only mandatory candidate representation.
- `Producer` / `FunctionProducer` — synchronous role contract and deterministic adapter.
- `Metric`, `Feedback`, `CaseEvidence`, `Observation` — ordered evidence values.
- `StrategyInput`, `Proposal`, `Advance | Stop` — one strategy transition.
- `GEPAState` / `GEPAStrategy` — selected-parent reflective proposals.
- `PhysicsState` / `PhysicsStrategy` — plan, experiment, or hypothesis revision.
- `RepairInput`, `RepairFeedback`, `Accepted | NeedsRepair`, `ItemFailure` —
  repair values.
- `CaseRequest`, `EvaluationRequest` — ordered case-evaluation inputs.

## Optional runtime modules

- `eggopt.eggflow` — generic cacheable `ProduceTask` / `EggflowProducer`.
- `eggopt.eggthreads` — cached run roots and an inspectable fake leaf Producer.
- `eggopt.eggflow_repair` — independently cached cumulative repair attempts.
- `eggopt.eggflow_evaluation` — independently cached case map and aggregation.

```python
import asyncio
from eggflow import FlowExecutor, TaskStore
from eggopt import (
    Candidate, CaseEvidence, EvaluationRequest, FunctionProducer,
    GEPAState, GEPAStrategy, StrategyInput,
)
from eggopt.eggflow_evaluation import EvaluationProducer

candidate = Candidate("def transition(state):\n    pass")
cases = FunctionProducer(
    lambda request: CaseEvidence(str(request.case))
)
aggregate = FunctionProducer(lambda observation: observation)
evaluate = EvaluationProducer(
    cases, "cases:v1", aggregate, "aggregate:v1"
)
evaluation_task = evaluate.produce(
    EvaluationRequest(candidate, ("example", "counterexample"))
)
store = TaskStore("flow.db")
try:
    observation = asyncio.run(FlowExecutor(store).run(evaluation_task))
finally:
    store.conn.close()

gepa = GEPAStrategy(
    FunctionProducer(lambda observations: observations),
    FunctionProducer(lambda parent: parent.cases),
)
decision = gepa.produce(
    StrategyInput(GEPAState(), (observation,))
)
assert len(decision.proposals[0].evidence) == 2
```

## Hard boundaries

- No trading, ARC, prompt-only, game, action, or artifact semantics.
- No first-class evaluator, Check, Constraint, objective, archive, or mandatory
  Pareto layer.
- Pure `import eggopt` has no runtime dependencies; optional adapters are
  imported explicitly.
- Caller-owned identities must change with Producer behavior/configuration.
- Live clients, schedulers, and DB connections stay out of cache keys/results
  and are reconstructed.
- Irreversible effects require an Eggflow task boundary; cached typed results
  are authoritative.
