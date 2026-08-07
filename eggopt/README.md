# eggopt

`eggopt` builds restartable agentic operations on top of
[`eggflow`](../eggflow/README.md) tasks and
[`eggthreads`](../eggthreads/README.md) conversations. It supplies reusable
Actor–Critic loops, durable tool calls and file receipts, finite-JSON GEPA
search, and Git-backed Physics strategies for model-based scientific discovery.

Eggopt does not prescribe one candidate schema or domain. Domain code owns its
prompts, evaluators, action/state contracts, extraction, and validation; Eggopt
owns durable composition, identities, thread/workspace structure, recovery, and
search control.

## Install

```bash
pip install -e ./eggflow
pip install -e ./eggthreads
pip install -e ./eggopt
```

Python 3.10+ is required.

## Durable standalone operations

`run_operation(...)` is the smallest supported runtime boundary for a finite
Eggflow composition:

```python
from dataclasses import dataclass
from eggflow import Task
from eggopt import current_operation, run_operation

@dataclass
class BuildReport(Task):
    topic: str

    def run(self):
        context = current_operation()
        return {
            "topic": self.topic,
            "workspace": context["inner_context"],
            "thread": context["operation_thread_id"],
        }

result = run_operation(
    BuildReport("cache behavior"),
    identity={"dataset": "v1"},
    name="Report",
    run_dir="runs/report",
)
```

The run directory owns one `.egg/` database for Eggflow and Eggthreads plus a
`workspace/innerContext` directory. Reusing `run_dir`, `name`, task identity,
and finite-JSON `identity` resumes cached primitive work. Changing the identity
creates a distinct durable operation namespace without discarding the run.

## Actor–Critic compositions

`Agent` describes an Eggthreads-backed model worker: its model/client, tools,
identity, full-history context limit, tool approval, and optional system prompt.
`ActorCritic` is an Eggflow task that creates a stable Actor/Critic child pair
and runs a bounded, recoverable proposal/revision loop.

```python
from eggflow import Task
from eggopt import ActorCritic, Agent, Critique, run_operation

actor = Agent(llm=llm, identity={"role": "writer"})

def actor_prompt(round_number, state):
    return "Produce the requested artifact." if round_number == 1 else state["feedback"]

class CheckArtifact(Task):
    def run(self):
        return Critique.accept(load_artifact()) if valid() else Critique.revise("Fix validation errors.")

loop = ActorCritic(
    actor=actor,
    critic=CheckArtifact(),
    actor_prompt=actor_prompt,
    max_rounds=3,
)
result = run_operation(loop, identity={"artifact": "v1"})
```

In this sketch `llm`, `load_artifact`, and `valid` are supplied by the domain.

A critic may be another `Agent` with a `critic_prompt`, or a deterministic
`Task`. `Critique.accept(value, feedback)` extracts a result;
`Critique.revise(feedback)` requests another Actor turn. Prompt factories may
return text or a Task, allowing durable preparation after the pair is assigned.
Interrupted model turns use Eggthreads continuation/recovery rather than being
silently replayed.

### GitCritic

`GitCritic` wraps a domain Critic when proposals are files in a Git repository.
Each Actor turn must create one new clean commit. The wrapper maintains an
isolated Critic clone, verifies fast-forward history, keeps trusted state
separate, commits the Critic result, and synchronizes authoritative history back
to the Actor workspace. The wrapped Critic still owns domain-specific checks.

## Durable thread tools and files

`ThreadTool` executes one synthetic tool call on an assigned Eggthreads thread
while participating in Eggflow cache identity:

```python
from eggopt import ThreadTool

receipt = yield ThreadTool(
    tools=tools,
    thread_id=thread_id,
    name="python_exec",
    arguments={"code": "build()"},
    input_files=("input.json",),
    output_files=("result.json",),
)
```

`tools` is a `ToolRegistry` and `thread_id` is an operation-owned Eggthreads
thread supplied by the surrounding task context.

Declared input-file hashes become part of identity without copying file bytes
into arguments. Declared outputs are snapshotted into Eggthreads' content-
addressed provider-output store. Cache reuse verifies or rematerializes missing
workspace files and returns a compact `ThreadToolResult`; transcripts store a
receipt rather than large bytes.

## GEPA over finite JSON

`optimize_anything(...)` performs case-wise Pareto search over opaque finite-JSON
candidate values. A domain supplies a seed, evaluator, dataset, objective, and a
Mutator through `GEPAConfig`:

```python
from eggopt import GEPAConfig, optimize_anything

result = optimize_anything(
    {"prompt": "Answer carefully."},
    evaluator=evaluate,
    dataset=trainset,
    valset=validation,
    objective="Improve accuracy without changing the output contract.",
    config=GEPAConfig(
        run_dir="runs/gepa",
        mutator=mutate,
        max_evaluator_calls=150,
        max_candidates=20,
    ),
)
print(result.best_candidate, result.best_score)
```

The evaluator, Mutator, datasets, and case identities are domain-owned.

GEPA owns deterministic minibatches, Pareto parent selection, acceptance,
evaluator budgets, full validation, and result assembly. The Mutator receives a
`MutatorInput` containing complete parents, objective, evaluator evidence,
score history, generation, and the last proposal outcome; it returns one
complete candidate directly or through a Task/awaitable. The evaluator may be a
callable or expose `task(candidate, case)`.

Candidate values and identities must be finite JSON so durable keys remain
stable. `plan_optimization(...)` estimates evaluator work before a run. Raising
`max_evaluator_calls` or `max_candidates` in the same run directory reuses
completed primitive evaluations.

## Physics scientific-discovery strategies

`PhysicsStrategy` is a Git-backed Actor–Critic protocol for domains with trusted
observation and action ports. The Actor proposes a world model and `plan.json`
in a clean commit; an isolated trusted Critic validates predictions and may
execute real actions until the goal, a terminal state, mismatch, plan
exhaustion, or safety budget.

A domain supplies:

```python
from eggopt import PhysicsStrategy, run_physics

strategy = PhysicsStrategy(
    actor=actor,
    observe=observe_task,
    execute=execute_task,
    validate_action=validate_action,
    is_goal=is_goal,
    terminal_outcome=terminal_outcome,  # optional
    identity={"domain": "experiment-v1"},
    domain_information="Describe public states and complete actions.",
)

result = run_physics(strategy, run_dir="runs/physics", max_actions=100)
```

The Actor and trusted observation, execution, validation, goal, and terminal
ports are domain-owned.

Three presets share the same lifecycle and Git boundary:

| Preset | Model state | Verification | Bundled planner |
| --- | --- | --- | --- |
| `eggopt.physics.latent` | latent | re-encoded observations | no |
| `eggopt.physics.latent-verified` | latent | latent plus complete public prediction | no |
| `eggopt.physics.verified` | complete public state | exact public-state comparison | yes |

The default `PhysicsStrategy` is equivalent to `verified`. Import a preset's
`strategy(...)` factory to select it explicitly; because `latent-verified` has
a hyphenated module name, load that preset with `importlib.import_module(...)`.
The verified preset seeds a
standard-library `plan.py` with search helpers; Actors may edit or replace
advisory planning code. Trusted validation of the committed world model and
trajectory remains independent of Actor-authored helpers.

The domain owns action representation and `validate_action`; Eggopt never
translates actions before execution. `is_goal` identifies success, while an
optional `TerminalOutcome` distinguishes absorbing non-goal stops.
`PhysicsResult.goal_reached` reports trusted goal completion; `accepted` only
reports that the Actor–Critic loop settled.

For batch composition, `PhysicsStrategy.task(...)` can attach a study to an
already-open runtime/thread tree and use a shared bounded `SubtreeScheduler`.
Standalone `run_physics(...)` owns its own run runtime.

## Durable state and context limits

Every GEPA, Physics, or standalone operation run owns an Eggflow cache,
Eggthreads event store, and workspace beneath its run directory. Eggopt
`Agent.context_limit` and GEPA evaluator/mutator limits bound full visible
thread history; Eggthreads provider-context compaction remains a separate
mechanism.

Do not query Eggopt private runtime/context helpers from domain code. Use
`current_operation`, `current_evaluation`, `ActorCritic`, and `ThreadTool` as the
public composition ports.

## Development

```bash
pip install -e "./eggopt[dev]"
pytest -q eggopt/tests
```

Related docs: [eggflow](../eggflow/README.md),
[eggthreads](../eggthreads/README.md), and the
[eggthreads API reference](../eggthreads/API.md).
