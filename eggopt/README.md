# eggopt

Eggopt offers two deliberately small GEPA interfaces:

- `UpstreamGEPA` runs the maintained external `gepa` algorithm while Eggflow
  caches metric calls and Eggthreads preserves reflection conversations.
- `NativeGEPA` is Egg's own dependency-free search: evaluate, reflect, keep the
  best, repeat. It has no dependency on the external `gepa` package.

Both optimizers own their `flow.db`, `.egg/threads.sqlite`, study thread,
reflection recovery, and resource lifecycle. Calling `compile()` again with the
same `run_dir` resumes from durable work instead of repeating model or metric
calls.

## Native GEPA

```python
from eggopt import Evaluation, NativeGEPA, Reflection


def metric(candidate, example):
    answer = run_student(candidate, example)
    return Evaluation(
        score=grade(answer, example),
        output=answer,
        feedback=critique(answer, example),
        evidence={"example": example},
    )


optimizer = NativeGEPA(
    metric=metric,
    reflection=Reflection.eggthreads(
        llm=reflection_lm,
        tools=reflection_tools,
        identity={"model": "reflection-model-v1"},
        workspace="runs/my-native-gepa/workspaces/mutation",
        model_key="reflection-model",
        models_path="runs/my-native-gepa/models.json",
    ),
    run_dir="runs/my-native-gepa",
    generations=5,
    proposals_per_generation=2,
)

result = optimizer.compile(
    {"system_prompt": seed_prompt},
    trainset=trainset,
    valset=valset,
)

print(result.best_candidate, result.best_score)
```

The native algorithm intentionally reads like its idea:

1. evaluate the seed;
2. reflect on the best candidate's evidence;
3. evaluate the proposed alternatives;
4. keep the best;
5. repeat.

Its primitive metric result is `Evaluation(score, output, feedback, evidence)`.
Its result contains candidates, scores, parents, outputs, metric-call count, and
the best candidate. More elaborate archives should be earned by a real use case,
not pre-installed as framework furniture.

## Upstream GEPA

Install the optional external algorithm:

```bash
pip install -e './eggopt[upstream]'
```

Then use the same client shape:

```python
from eggopt import Reflection, UpstreamGEPA

optimizer = UpstreamGEPA(
    metric=metric,
    reflection=Reflection.eggthreads(
        llm=reflection_lm,
        tools=reflection_tools,
        identity={"model": "reflection-model-v1"},
        workspace="runs/my-upstream-gepa/workspaces/mutation",
        model_key="reflection-model",
        models_path="runs/my-upstream-gepa/models.json",
    ),
    run_dir="runs/my-upstream-gepa",
    max_metric_calls=150,
)

result = optimizer.compile(
    {"system_prompt": seed_prompt},
    trainset=trainset,
    valset=valset,
)
```

`UpstreamGEPA` delegates candidate archives, Pareto behavior, lineage,
acceptance, and budgets to external GEPA. Eggopt owns the durable runtime around
it. Advanced clients may still import low-level compatibility components from
`eggopt.gepa`, but normal clients should not assemble `TaskStore`,
`FlowExecutor`, `ThreadsDB`, adapters, drives, or recovery calls themselves.
