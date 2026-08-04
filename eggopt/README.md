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
  Its optional `input_files=(...)` contract authorizes files and includes their
  content hashes in the cache identity without copying large inputs into tool
  arguments.
  Its optional `output_files=(...)` contract snapshots sandbox-written files into
  content-addressed storage and verifies/rematerializes them on recovery while
  returning a compact `ThreadToolResult` receipt.
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

`PhysicsStrategy` is the complete Git-backed scientific method, implemented as
one persistent `ActorCritic`:

```text
Physics
└── Critic
    └── Actor
```

The domain supplies only its world ports:

```python
physics = PhysicsStrategy(
    actor=actor,
    observe=initial_state_task,
    execute=real_action_task,
    validate_action=trusted_domain_action_validator,
    is_goal=trusted_goal_predicate,
    terminal_outcome=trusted_absorbing_state_classifier,  # optional
    identity={"domain": "my-world", "version": 1},
    domain_information="Explain the public state and action formats.",
    evaluator_timeout_sec=300,
)
```

`validate_action(state=..., action=...)` returns `None` for one valid complete
domain action or raises before execution. It validates without translating the
action. `planner_actions` may optionally expose a finite tuple of complete actions
to the advisory planner; Actor-authored `plan.json` trajectories do not depend on
that tuple.

`is_goal(state)` identifies successful completion. Domains with absorbing
non-goal states may additionally return `TerminalOutcome("reason")` from
`terminal_outcome(state)`; otherwise it returns `None`. Eggopt owns when that
lifecycle port is checked, while the domain alone owns what its states mean.
`PhysicsResult.goal_reached` distinguishes successful goal completion from
domain-terminal and safety-limit stops; `accepted` means the ActorCritic loop
settled and is not itself a success signal.

`PhysicsStrategy.task(...)` composes the same study into an already-open Eggopt
runtime and an existing Physics thread. Batch applications can therefore place
related studies below one root, give each study its own workspace, and drive all
Actor turns through one bounded Eggthreads `SubtreeScheduler`. An Agent with
`scheduler_managed=True` waits for that shared scheduler instead of constructing
its own `ThreadRunner`; ordinary standalone Physics runs remain unchanged.

Eggopt owns the Timeline, independent `plan.json` trajectory validation, the
execute-until-resolution loop, and repository recovery. The Actor owns its
`step_<suffix>` hypotheses and every normal file in `innerContext`, including any
planning scripts it edits or creates. The domain owns its action representation
and trusted action validator.

The Actor works in `workspace/innerContext`; every turn submits a clean Git HEAD.
The Critic keeps `workspace/critic-repository`, pulls submitted history, and
restores/rehydrates the latest canonical state if the Actor deletes `.git`.

Committed world-model code is never imported into the controller. The generic
Critic writes a compact `.trusted/requests/<ACTOR_HEAD>.json` manifest; the
sandbox evaluator reads that manifest, committed `world_model.py`, and
`canonical-input.json`, and `plan.json` as declared `ThreadTool` file inputs. Only
the small fixed runner and paths travel through `python_exec`. Eggthreads applies the
Critic thread's working directory and Docker sandbox to untrusted execution while
the evaluator timeout terminates runaway submitted code and file hashes keep
caching content-addressed. The evaluator writes
`.trusted/evaluations/<ACTOR_HEAD>.json`; `ThreadTool` snapshots those bytes by
SHA-256 and records only a compact receipt. Cached replay verifies or
rematerializes the report before the Critic consumes it.

When creating or recovering a repository, PhysicsStrategy seeds a readable,
standard-library-only `plan.py` plus small `backtest.py`, `commit.py`, and
`physics-config.json` helpers. `plan.py` contains reward BFS, A*, and its detailed
usage guide. These are untrusted starter files: the Actor may edit, replace, or
delete them and may write different tools. A valid repository is never refreshed
to undo those choices; Git retains older versions. The trusted Critic independently
evaluates committed `world_model.py` and `plan.json` before any real action.

An experiment can contain a common multi-action prefix. Execution stops on the
first wrong prediction or immediately after the first intent whose model
predictions actually branch. Goal plans stop on trusted goal detection, mismatch,
plan exhaustion, or action budget.
