# eggopt

`eggopt` contains concrete optimization integrations for Egg. Its first slice
runs maintained upstream `gepa==0.1.4`: GEPA owns candidate archives, lineage,
Pareto frontiers, acceptance, budgets, and `GEPAResult`; Eggflow durably caches
each candidate/example evaluation, and Eggthreads stores each reflective
mutation as an inspectable `GEPA Study -> Iteration -> Mutation` conversation.

The package deliberately does **not** define a generic optimization runtime.

## Minimal wiring

```python
from eggflow import FlowExecutor, TaskStore
from eggthreads import ThreadsDB
from eggopt.gepa import (
    CandidateMutation, EggflowGEPAAdapter, EggthreadsCandidateProposer,
    ExampleEvaluation, ReflectionEvidence, optimize_with_egg,
)

flow = FlowExecutor(TaskStore("flow.db"))
threads = ThreadsDB("threads.sqlite")
threads.init_schema()

def evaluate(candidate, example):
    answer = candidate["instruction"]
    return ExampleEvaluation(
        output=answer,
        score=float(answer == example["target"]),
        evidence=ReflectionEvidence(
            inputs={"target": example["target"]},
            generated_outputs=answer,
            feedback="match the target",
        ),
    )

# A real application injects its own drive. Tests use deterministic fakes only.
def drive(conversation, request):
    del request
    mutation = CandidateMutation({"instruction": "better instruction"})
    conversation.append_assistant("Inspectable reflection", mutation)
    return mutation

adapter = EggflowGEPAAdapter(
    flow,
    evaluator=evaluate,
    evaluator_id="my-domain-evaluator",
    evaluator_version="1",
    evaluator_config={"metric": "exact-match"},
    example_id=lambda example: example["id"],
)
proposer = EggthreadsCandidateProposer(
    flow,
    threads,
    drive=drive,
    reflector_id="my-reflector",
    reflector_version="1",
    reflector_config={"policy": "domain-review-v1"},
)
result = optimize_with_egg(
    seed_candidate={"instruction": "baseline"},
    trainset=[{"id": "train-1", "target": "better instruction"}],
    valset=[{"id": "val-1", "target": "better instruction"}],
    adapter=adapter,
    proposer=proposer,
    max_metric_calls=20,
    reflection_minibatch_size=1,
    skip_perfect_score=False,
)
print(result.best_candidate, result.parents, result.per_val_instance_best_candidates)
```

Semantic evaluation keys include a versioned operation, stable evaluator
identity/configuration, the candidate, and application-supplied example
identity. Store paths, thread IDs, labels, and live resource objects are
excluded. To replay evaluation results after a process restart, open a new
`TaskStore`/`FlowExecutor` on a copy or restored instance of the prior Eggflow
SQLite database; its filesystem path may differ.
