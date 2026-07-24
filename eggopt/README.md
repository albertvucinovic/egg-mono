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
            identity={"model": "reflection-model-v1"},
        ),
        run_dir="runs/my-gepa",
        max_evaluator_calls=150,
        max_candidates=10,
        minibatch_acceptance="strict_improvement",
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

`NativeGEPAConfig(evaluator_context_limit=...)` applies one explicit context
budget to every Case Evaluation thread and its descendants. It is independent
of model capacity, counts the full Eggthreads history rather than only the
post-compaction provider prompt, and becomes part of the durable evaluation
identity.

`minibatch_acceptance="strict_improvement"` is the default: a child tied with
the selected parents' per-case score envelope is rejected. Use
`"improvement_or_equal"` to send tied children to full validation as well.

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
`max_evaluator_calls` and `max_candidates` are stopping budgets, not cache-key
inputs, so changing them does not invalidate completed primitive work.

Every GEPA-managed LLM thread receives the versioned `solver_safe` registry and
full safe allowlist by default. Pass an explicit `allowed_tools` list to
replace it; explicit tools need not belong to the default set, but must be
present in the selected registry. Structural Candidate/Case threads inherit
the profile; a domain may still explicitly disable or narrow specialized
descendants. The profile lets a Mutation thread list its own subtree
with `threads` and run opted-in tools such as `python_repl` in a strict
descendant's context through `execute_tool_in_other_thread`. The caller's policy
authorizes this supervisory action; a descendant's own narrowed allowlist still
limits its self-invocation but does not block its
authorized ancestor. Ancestors, siblings, and unrelated threads remain hidden.

Mutation's solver-safe profile omits `send_message_to_child`. Candidate
evaluation subtrees are trusted Eggopt-owned runtimes and begin independent
tool-policy scopes, so a Critic may guide its Actor without granting Mutation
that capability.
NativeGEPA also adds a stable mutation-agent system prompt before the first
model turn. Candidate, generation, and evaluation summaries are durable typed
events, not conversation messages, so they remain inspectable without ever
entering provider context.

Pass `NativeGEPAConfig(progress=callback)` for synchronous, occurrence-level
evaluation updates. The callback is observability only: it is excluded from
semantic cache identities. Each distinct candidate/case-set/stage occurrence is
reported once per durable study, so reruns do not reprint completed progress.

Domains may replace both prompt layers independently:
`Reflection.eggthreads(system_prompt=...)` sets the persistent mutation role,
while `instruction=...` is the task-specific user request included in each
reflection cache identity.

```python
restricted = Reflection.eggthreads(
    llm=reflection_lm,
    identity={"model": "reflection-model-v1"},
    allowed_tools={"python_exec"},
)
```

Reflection runner steps are unbounded by default, allowing Eggthreads to use
compaction for long-lived conversations. Applications may set an explicit
Mutation-thread full-context budget; compaction controls provider-prompt size
without resetting that budget. `max_runner_steps` is only an optional finite
guard:

```python
reflection = Reflection.eggthreads(
    llm=reflection_lm,
    identity={"model": "reflection-model-v1"},
    context_limit=240_000,
)
```

These are deliberately separate counters: Eggthreads decides when to compact
from its current provider context, while Eggopt stops the experiment from the
full thread history.

Eggopt also includes the optional reusable `ActorCritic` Task. It creates a
Critic thread with an Actor child for the current case, keeps both across bounded
revision rounds, gives them a shared sandboxed `innerContext`, and requires only
the Critic decision envelope `{"decision":"accept|revise","feedback":"..."}`.

```python
from eggflow import Task
from eggopt import ActorCritic, Agent


class EvaluateCase(Task):
    def run(self):
        attempt = yield ActorCritic(
            actor=Agent(actor_llm, {"role": "actor"}, context_limit=32_000),
            critic=Agent(critic_llm, {"role": "critic"}),
            actor_prompt=actor_prompt,
            critic_prompt=critic_prompt,
            max_rounds=3,
        )
        return hidden_grade(attempt.answer), {"answer": attempt.answer}
```

When configuring an `Agent` directly, pass its Eggopt budget as
`context_limit=...`; `runner_config.context_limit` remains the distinct
Eggthreads provider-context setting.

The Critic-parent topology lets a Critic model inspect its Actor descendant
through Eggthreads' descendant-safe tools. The critic may also be an ordinary
Eggflow `Task`. Before every critique,
ActorCritic fills matching dataclass fields on a fresh copy:
`actor_thread_id`, `critic_thread_id`, `workspace`, `answer`, `feedback`, and
`round_number`. The Task can use its assigned critic thread for model turns or
tools, then returns the same `{"decision","feedback"}` envelope.

```python
@dataclass
class Check(Task):
    actor_thread_id: str | None = None
    critic_thread_id: str | None = None
    answer: object = None

    def run(self):
        return {"decision": "accept", "feedback": "Valid."}
```

Use `plan_optimization(...)` to estimate total and additional evaluator work
before choosing limits.

## Upstream GEPA

Install external GEPA separately, then use `UpstreamGEPA`. It is intentionally
not routed through `optimize_anything`; the two algorithms keep their own clear
configuration and search semantics.

Advanced legacy integrations remain available from `eggopt.gepa`.
