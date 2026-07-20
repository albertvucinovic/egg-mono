# eggopt

`eggopt` contains concrete optimization integrations for Egg. Its first slice
runs maintained upstream `gepa==0.1.4`: GEPA owns candidate archives, lineage,
Pareto frontiers, acceptance, budgets, and `GEPAResult`; Eggflow durably caches
each candidate/example evaluation, and Eggthreads stores each reflective
mutation as an inspectable `GEPA Study -> Iteration -> Mutation` conversation.

The package deliberately does **not** define a generic optimization runtime.
Its injected reflection drive has two local entry points: `start` begins the
first turn in a new Mutation conversation, while `resume` drives a request that
was appended to an existing candidate-lineage conversation.

## Minimal wiring

```python
from eggflow import FlowExecutor, TaskStore
from eggthreads import ThreadsDB
from eggopt.gepa import (
    CandidateMutation, CandidateMutations, EggflowGEPAAdapter,
    EggthreadsReflectionLM, ExampleEvaluation, ReflectionEvidence,
    optimize_with_egg,
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
class Drive:
    def start(self, conversation, request):
        del request
        mutation = CandidateMutation({"instruction": "better instruction"})
        conversation.append_assistant("Inspectable reflection", mutation)
        return mutation

    def resume(self, conversation, request):
        # The request is already appended to the producing conversation.
        mutation = CandidateMutation({"instruction": "better instruction"})
        conversation.append_assistant("Inspectable follow-up", mutation)
        return mutation

drive = Drive()

adapter = EggflowGEPAAdapter(
    flow,
    evaluator=evaluate,
    evaluator_id="my-domain-evaluator",
    evaluator_version="1",
    evaluator_config={"metric": "exact-match"},
    example_id=lambda example: example["id"],
)
proposer = EggthreadsReflectionLM(
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

## One turn, multiple proposals

Upstream GEPA can request several proposals from one parent with its public
`SameParentSampling` strategy. `EggthreadsReflectionLM.reflect_many()` groups
those ordered jobs by parent and calls the persistent mutator once per parent.
The drive returns a typed ordered batch and persists it with the assistant turn:

```python
from gepa.strategies.proposal_sampling import SameParentSampling

class BestOfTwoDrive(Drive):
    def start(self, conversation, request):
        assert request["mutation_count"] == 2
        mutations = CandidateMutations((
            CandidateMutation({"instruction": "concise"}),
            CandidateMutation({"instruction": "show your reasoning"}),
        ))
        conversation.append_assistant("Two informed alternatives", mutations)
        return mutations

proposer = EggthreadsReflectionLM(
    flow,
    threads,
    drive=BestOfTwoDrive(),
    reflector_id="my-reflector",
    reflector_version="1",
    reflector_config={"policy": "best-of-two"},
)
result = optimize_with_egg(
    seed_candidate={"instruction": "baseline"},
    trainset=trainset,
    adapter=adapter,
    proposer=proposer,
    sampling_strategy=SameParentSampling(2),
    max_metric_calls=100,
)
```

GEPA independently evaluates each returned `ReflectionProposal`, then applies
its configured acceptance and selection strategies. Eggopt does not duplicate
that optimizer logic. A later critique of any emitted candidate is appended to
the same producing Mutation conversation.

Reflection turns have two identities: the Eggflow task key is semantic, while
the Eggthread is physical conversation context. Repeating an identical request
reuses the typed Eggflow result even from another study. For a new request about
a candidate produced by an earlier mutation, the proposer finds that structured
candidate result and appends the follow-up to the same Mutation thread. On
restart, pass the persisted `study_thread_id` to recover that affinity; thread
display names are never authority. An interrupted request already present in
that study is driven in place without appending a duplicate trigger.

Semantic evaluation keys include a versioned operation, stable evaluator
identity/configuration, the candidate, and application-supplied example
identity. Store paths, thread IDs, labels, and live resource objects are
excluded. To replay evaluation results after a process restart, open a new
`TaskStore`/`FlowExecutor` on a copy or restored instance of the prior Eggflow
SQLite database; its filesystem path may differ.
