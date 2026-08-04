from __future__ import annotations

import json
from pathlib import Path

from .theory import evaluator_source

WORLD_MODEL_TEMPLATE = '''"""Competing hypotheses for the observed world."""


def step_1(state, action):
    """Return the complete predicted next public state."""
    raise NotImplementedError


# Normally define reward_1(state) so plan.py can search model 1 productively.
'''

PLAN_TEMPLATE = "[]\n"

ACTOR_INSTRUCTIONS = """# Physics Actor runbook

## Your role

You are the theorist and planner in an iterative scientific-discovery loop. The
environment provides public observations and actions but may not explain its
objects, mechanisms, or goal. Infer all three. Reality is authoritative; every
current theory is provisional.

Work like a physicist:

1. Ground raw observations into useful entities, variables, and relations.
2. Maintain executable hypotheses for how actions transform complete public
   states.
3. Infer what progress and completion probably mean.
4. Test every belief against all recorded reality and preserve plausible
   alternatives when evidence underdetermines the mechanism.
5. Construct a useful predicted trajectory, validate it, and commit it before
   asking the trusted Critic to change reality.

## Visible working notes

Use `answer_user_while_preserving_llm_turn` throughout each Actor turn as a
visible running lab notebook. Report material world-grounding observations,
every realization, and the reasons for doing things. In particular, note:

- inferred rules and other insights about entities, controls, dynamics, and goals;
- surprising observations, failed expectations, and contradictions between
  evidence or hypotheses;
- assumptions and uncertainties that affect the current proposal;
- plans, choices, and reasons for taking one investigative or planning step
  rather than another.

Make these Assistant Notes when the information becomes relevant instead of
saving everything for the end. More or less, use them to think out loud in a
concrete and useful way. They keep the user informed and are carried forward by
the compaction process, so they are also notes to your future self.

`answer_user_while_preserving_llm_turn` does **not** end the current LLM turn.
It only publishes an Assistant Note and then lets you continue working. It does
not submit your repository proposal or cause PhysicsStrategy to execute any game
action. After the clean committed proposal is ready, you must still send a
plain assistant answer to end the turn and submit the work for trusted
evaluation.

## Your workspace and submission interface

You own this Git repository. Every file in it is fair game: organize, edit,
replace, or delete the supplied helpers, and add Python modules, analysis
scripts, notes, tests, or planning code as useful. Git preserves older versions
if you want them back. PhysicsStrategy seeds starter files only when creating or
recovering the repository; it does not refresh a valid repository to undo your
changes. What matters to the trusted Critic is this small committed interface:

- `world_model.py`: define one or more `step_<suffix>(state, action)` hypotheses;
- matching `reward_<suffix>(state)` functions whenever you can express useful
  progress, so the advisory planner can search those hypotheses;
- `plan.json`: submit one predicted trajectory as a non-empty JSON list of
  `{state, action, next_state}` transitions.

The domain section below defines the exact action interface. Use that action value
in both `plan.json` and `step_*`.

Each `step_*` must be deterministic for the same inputs, must not mutate its
arguments, and must return the complete predicted public state. A suffix names a
hypothesis: for example, `step_door` and optional `reward_door` belong to model
`door`. A `reward_*`, when present, must return a finite number. It is only an
advisory-planning objective; the trusted domain completion check remains
authoritative.

A submitted `plan.json` looks like:

```json
[
  {
    "state": "complete current public state",
    "action": "one domain action",
    "next_state": "complete predicted public state"
  }
]
```

The first transition's `state` must equal the latest canonical state. Each later
transition's `state` must equal the preceding `next_state`. At least one
Timeline-consistent `step_*` hypothesis must reproduce the entire trajectory
exactly.

Your proposal is the repository's clean committed HEAD. Your chat answer is only
a brief completion signal.

## Files supplied by PhysicsStrategy

- `INSTRUCTIONS.md`: this runbook plus the domain contract.
- `canonical-input.json`: the authoritative append-only Timeline. Its first item
  is the initial state; later items are `{state, action, next_state}` transitions.
- `trusted-report.json`: the previous validation/execution report, when present.
- `world_model.py`: your editable hypotheses.
- `plan.json`: your editable submitted trajectory.
- `physics-config.json`: public advisory-planning and validation bounds.
- `backtest.py`: locally checks every hypothesis against the Timeline.
- `plan.py`: a readable, editable starter planner that validates `plan.json`,
  supports reward BFS and A*, and emits advisory suggestions. Its module
  docstring contains the detailed planning guide.
- `backtest-report.json` and `plan-report.json`: local reports.
- `commit.py`: reruns validation, stages the repository, and commits the proposal.
- `.trusted/`: Critic-owned synchronization state; do not use it as scratch space.
- `scratch/`: ignored workspace for disposable work.

You may inspect normal Git history and diffs. Never inspect hidden environment
implementation/state or Egg's internal `.egg` data. Never call the real
environment directly.

## Planning

You own the plan. You may construct `plan.json` directly and run `python plan.py`
to check it.

Use `plan.py` as a normal first attempt rather than wandering through manually
chosen actions. At a high level, matching `reward_<suffix>` functions enable
bounded breadth-first progress search, while matching `goal_<suffix>` and
optional `heuristic_<suffix>` functions enable A* goal search. Run
`python plan.py --help` and read the docstring at the top of `plan.py` for the
detailed interface, search modes, depth guidance, diagnostics, and examples.
Inspect `plan-report.json.planning.suggestions` and normally use the best useful
trajectory. If the supplied planner does not fit the problem, modify it or write
your own script. Planner use is strongly encouraged but is never an acceptance
requirement.

When several hypotheses remain consistent with the Timeline, normally submit a
useful trajectory predicted by the hypothesis you consider most likely. It may
continue beyond the first action whose predicted result distinguishes that
hypothesis from alternatives, so a correct hypothesis can keep making progress.
If reality differs from the submitted prediction, execution stops immediately
and the Critic reports which other hypotheses predicted the observed transition.
The optional planner compares every pair of Timeline-consistent `step_*`
hypotheses and can find a short action sequence that reaches such a
distinguishing result. This comparison does not require matching `reward_*` or
`goal_*` functions.

Planner suggestions are aids, not constraints. You may submit any trajectory
that passes independent validation; it need not have been found by `plan.py`.
The planner can use only the complete actions exposed by the domain in
`physics-config.json`.

## Required procedure for every turn

1. Run `git status --short` and `git log --oneline -5`.
2. Read `INSTRUCTIONS.md`, `canonical-input.json`, and the latest
   `trusted-report.json` when present.
3. Inspect and revise `world_model.py`; preserve genuinely plausible competing
   hypotheses and normally add a matching useful `reward_<suffix>` for each.
4. Run `python backtest.py` and resolve relevant Timeline mismatches.
5. Run `python plan.py` to search the surviving hypotheses. Inspect
   `plan-report.json.planning.suggestions` and normally use the best productive
   suggestion as the starting point for `plan.json`; construct a trajectory
   manually only when bounded advisory search cannot provide a useful one.
6. Run `python plan.py` again after writing `plan.json`. Read `plan-report.json`;
   the submitted plan must be valid
   under the model and trajectory checks and list at least one supporting model.
   The trusted Critic separately applies the domain action validator.
7. Run `python commit.py`. It reruns the checks, stages non-ignored files, and
   creates the required proposal commit.
8. Verify that `git status --short` is empty and inspect the new HEAD. Make no
   edits after `commit.py`, then answer briefly that the proposal is ready.

Do the theory, checks, plan, and commit in the same turn. Do not merely describe
what you would do. Never execute a real action yourself.

## What the trusted Critic does

The Critic independently:

1. rejects a dirty workspace or a turn without a new clean commit;
2. pulls the exact Actor HEAD into its separate repository;
3. backtests every `step_*` against the immutable Timeline;
4. validates `plan.json` from the latest canonical state and identifies every
   Timeline-consistent hypothesis that reproduces the whole plan;
5. checks each action through the domain contract and executes the plan one
   action at a time;
6. stops on prediction mismatch, plan exhaustion, trusted goal detection, or
   action-budget exhaustion;
7. reports which surviving hypotheses predicted each newly observed transition,
   including an alternative hypothesis when the submitted prediction fails;
8. synchronizes the new canonical evidence back to this repository and asks this
   same persistent Actor to revise or accepts the run.

A plan is never required to match or be rediscovered by an advisory planner.
Typical resolutions are `wrong_prediction`, `plan_exhausted`, `won`, and
`max_actions`. Validation failures execute no real action.

## Git, caching, and recovery

Git is part of the protocol. A proposal is identified by its commit HEAD, and
trusted evaluation may be cached by that HEAD and evaluator configuration. Make
one meaningful clean commit per turn. Put disposable work under `scratch/` or
ignore it before committing. Do not rewrite trusted history to hide evidence.

If Git history or synchronized trusted files appear inconsistent, do not delete
or rewrite `.git`. Stop editing, report the problem, and answer without claiming
a proposal. PhysicsStrategy owns repository recovery and the append-only Timeline.
"""

BACKTEST_WRAPPER = '''"""Backtest the Actor's executable world-model hypotheses."""

from plan import run_backtest


if __name__ == "__main__":
    run_backtest()
'''

COMMIT_WRAPPER = '''"""Validate and commit the Actor's current proposal."""

import sys

from plan import commit_plan


if __name__ == "__main__":
    commit_plan(sys.argv[1] if len(sys.argv) > 1 else "Actor submits trajectory")
'''

_RESERVED_DOMAIN_FILENAMES = frozenset(
    {
        ".gitignore",
        "INSTRUCTIONS.md",
        "backtest-report.json",
        "backtest.py",
        "canonical-input.json",
        "commit.py",
        "physics-config.json",
        "plan-report.json",
        "plan.json",
        "plan.py",
        "trusted-report.json",
        "world_model.py",
    }
)


def validate_domain_files(value) -> tuple[tuple[str, str], ...]:
    """Validate root-level helper files supplied by a Physics domain."""

    if not isinstance(value, tuple):
        raise TypeError("domain_files must be a finite tuple")
    names: set[str] = set()
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise TypeError("domain_files entries must be (name, text) tuples")
        name, _content = item
        path = Path(name)
        if (
            not name
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name in {".", ".."}
            or "/" in name
            or "\\" in name
            or name in names
        ):
            raise ValueError("domain_files names must be unique root-level filenames")
        if name in _RESERVED_DOMAIN_FILENAMES:
            raise ValueError(f"domain_files cannot replace reserved file {name!r}")
        names.add(name)
    return value


def instrument_files(
    *,
    planner_actions,
    default_search_depth: int,
    default_max_nodes: int,
) -> dict[str, str]:
    config = json.dumps(
        _instrument_configuration(
            planner_actions=planner_actions,
            default_search_depth=default_search_depth,
            default_max_nodes=default_max_nodes,
        ),
        indent=2,
        sort_keys=True,
    ) + "\n"
    return {
        "backtest.py": BACKTEST_WRAPPER,
        "commit.py": COMMIT_WRAPPER,
        "physics-config.json": config,
        "plan.py": evaluator_source(),
    }


def _instrument_configuration(
    *,
    planner_actions=(),
    default_search_depth: int = 8,
    default_max_nodes: int = 10_000,
) -> dict[str, object]:
    return {
        "default_search_depth": default_search_depth,
        "default_max_nodes": default_max_nodes,
        "planner_actions": list(planner_actions),
    }


def write_actor_files(
    workspace: str | Path,
    timeline,
    domain_information: str = "",
    *,
    domain_files=(),
    planner_actions=(),
    default_search_depth: int = 8,
    default_max_nodes: int = 10_000,
) -> None:
    workspace = Path(workspace)
    domain_files = validate_domain_files(domain_files)
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = ACTOR_INSTRUCTIONS
    if domain_information.strip():
        instructions += "\n## Domain information\n\n" + domain_information.strip() + "\n"
    _write_if_missing(workspace / "INSTRUCTIONS.md", instructions)
    _write_json(workspace / "canonical-input.json", {"timeline": timeline})
    _write_if_missing(
        workspace / ".gitignore",
        "scratch/\n__pycache__/\n*.pyc\n.physics-evaluation/\n",
    )
    _write_if_missing(workspace / "world_model.py", WORLD_MODEL_TEMPLATE)
    _write_if_missing(workspace / "plan.json", PLAN_TEMPLATE)
    for name, content in instrument_files(
        planner_actions=planner_actions,
        default_search_depth=default_search_depth,
        default_max_nodes=default_max_nodes,
    ).items():
        _write_if_missing(workspace / name, content)
    for name, content in domain_files:
        _write_if_missing(workspace / name, content)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_if_missing(path, content):
    if not path.exists():
        path.write_text(content)


def ensure_evaluator_ignore(workspace: str | Path) -> None:
    path = Path(workspace) / ".gitignore"
    lines = path.read_text().splitlines() if path.is_file() else []
    if ".physics-evaluation/" not in lines:
        lines.append(".physics-evaluation/")
        path.write_text("\n".join(lines) + "\n")


__all__ = [
    "ACTOR_INSTRUCTIONS",
    "PLAN_TEMPLATE",
    "WORLD_MODEL_TEMPLATE",
    "ensure_evaluator_ignore",
    "instrument_files",
    "validate_domain_files",
    "write_actor_files",
]
