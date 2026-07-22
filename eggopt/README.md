# eggopt

Eggopt has one simple native front door and one explicit upstream facade:

- `optimize_anything(...)` runs Egg's own GEPA search using only Eggflow and
  Eggthreads.
- `UpstreamGEPA` runs the optional external `gepa` package behind Egg's durable
  runtime.

## Native GEPA

```python
from eggopt import NativeGEPAConfig, Reflection, optimize_anything


def evaluate(candidate, case):
    answer = run_my_system(candidate, case)
    score = grade(answer, case)
    return score, {"answer": answer, "expected": case["expected"]}


result = optimize_anything(
    seed_candidate={"system_prompt": seed_prompt},
    evaluator=evaluate,
    dataset=trainset,
    valset=valset,             # optional; defaults to dataset
    objective="Improve accuracy while preserving strict JSON output.",
    config=NativeGEPAConfig(
        reflection=Reflection.eggthreads(
            llm=reflection_lm,
            tools=reflection_tools,
            identity={"model": "reflection-model-v1"},
        ),
        run_dir="runs/my-gepa",
        max_evaluator_calls=150,
        max_candidates=10,
    ),
)

use(result.best_candidate)
```

`config` has ordinary search defaults. In this first slice, use it to pass a
reflection strategy (or custom candidate generator). Evaluators return either
`score` or `(score, feedback)`. Plain sync and async functions are cached as
Eggflow Tasks. An advanced evaluator may expose `task(candidate, case)` and
return its own composite Eggflow Task.

NativeGEPA uses seeded epoch-shuffled minibatches for mutation, checks a child
on the same minibatch, evaluates accepted children on the full validation set,
and selects distinct parents from the per-case Pareto frontier. Aggregate score
determines `best_candidate`.

Every study is durable:

```text
Mutation
├── Candidate 1 Evaluation
│   ├── Case 1 Evaluation
│   └── Case 2 Evaluation
└── Candidate 2 Evaluation
```

Each case owns `outerContext/innerContext/`. Evaluator Tasks may call
`current_evaluation()` to discover those paths and create an Actor/Critic
subtree. Rerunning the same study with larger limits replays finished Tasks and
continues with new work.

Eggopt also includes the optional reusable `ActorCritic` Task. It creates one
Actor and one Critic thread for the current case, keeps both across bounded
revision rounds, gives them a shared sandboxed `innerContext`, and requires only
the Critic decision envelope `{"decision":"accept|revise","feedback":"..."}`.

```python
from eggflow import Task
from eggopt import ActorCritic, Agent


class EvaluateCase(Task):
    def run(self):
        attempt = yield ActorCritic(
            actor=Agent(actor_llm, {"role": "actor"}),
            critic=Agent(critic_llm, {"role": "critic"}),
            actor_prompt=actor_prompt,
            critic_prompt=critic_prompt,
            max_rounds=3,
        )
        return hidden_grade(attempt.answer), {"answer": attempt.answer}
```

Use `plan_optimization(...)` to estimate total and additional evaluator work
before choosing limits.

## Upstream GEPA

Install external GEPA separately, then use `UpstreamGEPA`. It is intentionally
not routed through `optimize_anything`; the two algorithms keep their own clear
configuration and search semantics.

Advanced legacy integrations remain available from `eggopt.gepa`.
