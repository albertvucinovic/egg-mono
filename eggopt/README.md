# eggopt

Eggopt has one GEPA implementation, built from reusable Eggflow Tasks and
Eggthreads agents.

```python
from eggopt import GEPAConfig, Mutator, optimize_anything


def evaluate(candidate, case):
    answer = run_my_system(candidate, case)
    return grade(answer, case), {"answer": answer}


result = optimize_anything(
    {"system_prompt": seed_prompt},
    evaluator=evaluate,
    dataset=trainset,
    valset=valset,
    objective="Improve accuracy while preserving strict JSON output.",
    config=GEPAConfig(
        run_dir="runs/my-gepa",
        max_evaluator_calls=150,
        max_candidates=10,
        mutator=Mutator.eggthreads(
            llm=mutation_llm,
            identity={"model": "mutation-model"},
            instruction="Improve the complete system prompt.",
            max_correction_turns=2,
        ),
    ),
)

use(result.best_candidate)
```

## Composition

- Eggflow owns task caching, retry, and restart recovery.
- `ActorCritic` is the reusable checked-generation loop.
- Its prompt factories may return Tasks, so thread-bound preparation can finish
  after Actor/Critic assignment and before the corresponding model turn.
- `ThreadTool` is the reusable Eggflow task for durable synthetic tool calls on
  assigned Eggthreads threads; domain code never queries Eggthreads storage.
- GEPA mutation uses `ActorCritic(actor=Mutation Agent, critic=ValidateMutation Task)`.
- The deterministic Critic returns `revise` with a precise schema error; ActorCritic
  continues the same Mutation thread with that feedback.
- Full valset evaluations live under `GEPA → Validation`, outside Mutation's
  descendant tree. Dataset minibatch evaluations live under
  `GEPA → Mutation Review → Mutation → Reflection`, so Mutation may inspect only
  reflection evidence and transcripts. The deterministic controller alone uses
  valset scores for Pareto selection.
- GEPA owns Pareto parent selection, minibatches, mutation requests, acceptance,
  evaluator budgets, and result assembly.
- Domain code owns prompts, cases, evaluators, and model selection.

All GEPA implementation modules live in `eggopt/gepa/`. Top-level `eggopt`
contains only reusable primitives and deliberate public re-exports.

Each study stores its durable Eggflow and Eggthreads state under one run-owned
`.egg` directory. Increasing stopping budgets reuses completed primitive work.
`max_evaluator_calls` and `max_candidates` are not primitive cache-key inputs.

`GEPAConfig(evaluator_context_limit=...)` controls the full Eggthreads history
available to each case evaluator. `Mutator.eggthreads(context_limit=...)` does the
same for Mutation. Eggthreads provider-context compaction remains independent.

Use `plan_optimization(...)` to estimate evaluator work before choosing limits.

## PhysicsStrategy

`PhysicsStrategy` is a durable observe → hypothesize → test → deliberate →
execute loop. It prescribes no world-model, hypothesis, action, or evidence
types: each role constructs an Eggflow `Task` and may be deterministic,
ActorCritic-backed, GEPA-backed, or another Task graph.

```python
from eggopt import PhysicsStrategy

physics = PhysicsStrategy(
    observe=observe_task,
    hypothesize=hypothesize_task,
    test=test_task,
    deliberate=deliberate_task,
    execute=execute_task,
    identity={"name": "my-physics", "version": 1},
)

result = physics.run(run_dir="runs/my-physics", max_actions=50)
```

The Timeline is append-only. A commitment is durably produced before its
opaque intents execute one at a time. After each real transition, `test`
returns `None` to keep the remaining queue or feedback to abort it and revise
the hypotheses. Thus reality always outranks the model.

Wrap domain effects in `PhysicsEffect` when their calls and results should also
form one readable history on the shared Environment thread. The wrapped Task
remains the cache/recovery boundary; no live environment handle is persisted.

The default thread skeleton is deliberately small:

```text
Physics
├── Environment
├── Hypotheses
└── Plan
```

Tasks, not threads, are the durable unit of work. A role may yield
`ActorCritic`; its Critic/Actor children then live beneath the corresponding
holding thread and retain conversation context across cached operations.
