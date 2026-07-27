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
