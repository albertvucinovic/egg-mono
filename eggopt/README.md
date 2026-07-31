# eggopt

Eggopt GEPA searches over opaque finite-JSON candidate values. A domain supplies
one Mutator and one Evaluator; GEPA knows only when to call them.

```python
from eggflow import Task
from eggopt import GEPAConfig, optimize_anything


class Improve(Task):
    context: object

    def run(self):
        # A domain may compose ActorCritic here, extract a response or file,
        # validate it, and return the complete next candidate.
        return improve(self.context)


def mutate(context):
    return Improve(context)


def evaluate(candidate, case):
    answer = run_my_system(candidate, case)
    return grade(answer, case), {"answer": answer}


result = optimize_anything(
    seed_candidate,
    evaluator=evaluate,
    dataset=trainset,
    valset=valset,
    objective="Improve accuracy while preserving the output contract.",
    config=GEPAConfig(
        run_dir="runs/my-gepa",
        max_evaluator_calls=150,
        max_candidates=20,
        mutator=mutate,
    ),
)

use(result.best_candidate)
```

## Composition

- Eggflow owns task caching, retry, and restart recovery.
- GEPA owns Pareto parent selection, minibatches, acceptance, evaluator budgets,
  and result assembly.
- The domain Mutator receives a `MutatorInput` with selected complete parents,
  evaluator evidence, objective, generation, validation-score history, and the
  last proposal outcome. It returns one complete candidate, directly or as a
  Task/awaitable.
- Candidate values are opaque to GEPA. Finite JSON is the only representation
  contract required for stable cache identity and durable results.
- `ActorCritic` is the reusable checked-generation loop a Mutator may compose.
  A deterministic Critic may return `Critique.accept(candidate, feedback)`;
  `result.value` is that extracted candidate. Without an extracted value, it
  defaults to the Actor answer. Extraction may therefore come from the response,
  a workspace file, or any other domain-owned transport. Plain decision mappings
  remain valid for simple critics.
- ActorCritic prompt factories may return Tasks, so thread-bound preparation can
  finish after Actor/Critic assignment and before the corresponding model turn.
- `ThreadTool` is the reusable Eggflow task for durable synthetic tool calls on
  assigned Eggthreads threads; domain code never queries Eggthreads storage.
- Full valset evaluations live under `GEPA → Validation`. Dataset reflection
  evaluations live under `GEPA → Mutation Review → Mutation → Reflection`; the
  deterministic controller alone uses valset scores for Pareto selection.
- Domain code owns Actors, prompts, extraction, validation, candidate shape,
  cases, evaluators, and model selection.

All GEPA implementation modules live in `eggopt/gepa/`. Top-level `eggopt`
contains only reusable primitives and deliberate public re-exports.

Each study stores its durable Eggflow and Eggthreads state under one run-owned
`.egg` directory. Increasing stopping budgets reuses completed primitive work.
`max_evaluator_calls` and `max_candidates` are not primitive cache-key inputs.

`GEPAConfig(evaluator_context_limit=...)` controls the full Eggthreads history
available to each case evaluator. `mutator_context_limit` supplies the domain
Mutator operation's inherited limit; a domain ActorCritic agent may override it.
Eggthreads provider-context compaction remains independent.

Use `plan_optimization(...)` to estimate evaluator work before choosing limits.

## PhysicsStrategy

`PhysicsStrategy` is one Git-backed `ActorCritic` scientific loop. The domain
supplies an Actor, a repository-preparation Task, and a trusted Critic Task:

```python
from eggopt import PhysicsStrategy, physics_actor_system_prompt

physics = PhysicsStrategy(
    actor=actor,
    prepare=prepare_repository,
    critic=trusted_physics_critic,
    identity={"domain": "my-world", "version": 1},
)
result = physics.run(run_dir="runs/my-physics", max_actions=50)
```

The Actor works in `workspace/innerContext`, which is an ordinary writable Git
repository. Every turn must create a clean new commit. The trusted Critic keeps
`workspace/critic-repository` as a pulled copy of the complete history and
evaluates the submitted Git HEAD rather than the Actor's mutable working tree.
A dirty repository is rejected. If the Actor deletes or corrupts `.git`, the
Critic restores its last copy; the domain Critic then rehydrates its latest
canonical irreversible world state.

The generic system prompt, available through
`physics_actor_system_prompt(domain_information)`, explains the Git and trust
rules. Domains append their model, evidence, instrument, plan, and environment
contracts.

The domain Critic may yield cached Tasks and real effects. It returns
`Critique.revise(...)` after validation errors or newly acquired evidence, and
`Critique.accept(value)` only at a terminal success/stopping condition. Thus the
same persistent Actor/Critic pair naturally alternates theory revision, trusted
validation, real execution, and new evidence until win or budget.
