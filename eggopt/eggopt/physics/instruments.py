from __future__ import annotations

import json
from pathlib import Path

from .theory import MODEL_RUNNER

WORLD_MODEL_TEMPLATE = '''"""Competing hypotheses for the observed world."""


def step_1(state, action):
    """Return the complete predicted next public state."""
    raise NotImplementedError


# Optional: define reward_1(state) to opt model 1 into advisory planning.
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

## Your workspace and submission interface

You own this Git repository. Organize your work however you find effective: add
Python modules, analysis scripts, notes, tests, or planning code. What matters to
PhysicsStrategy is this small file interface:

- `world_model.py`: define one or more `step_<suffix>(state, action)` hypotheses;
- optional `reward_<suffix>(state)` functions for hypotheses you want the
  advisory planner to search;
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
exactly. The plan has no type and no branches.

Your proposal is the repository's clean committed HEAD. Your chat answer is only
a brief completion signal.

## Files supplied by PhysicsStrategy

- `INSTRUCTIONS.md`: this runbook plus the domain contract.
- `canonical-input.json`: the authoritative append-only Timeline. Its first item
  is the initial state; later items are `{state, action, next_state}` transitions.
- `trusted-report.json`: the previous validation/execution report, when present.
- `world_model.py`: your editable hypotheses.
- `plan.json`: your editable submitted trajectory.
- `physics_runtime.py`: generated standard-library-only instrumentation.
- `physics-config.json`: public advisory-planning and validation bounds.
- `backtest.py`: locally checks every hypothesis against the Timeline.
- `plan.py`: locally validates `plan.json` and emits advisory suggestions.
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

`plan.py` is also an optional bounded planner. A hypothesis opts into advisory
planning by defining `reward_<suffix>(state)`. The instrument may suggest a
trajectory toward higher predicted reward or a trajectory ending at the first
action whose predicted outcome distinguishes competing eligible hypotheses.
All hypotheses involved in an advisory search must define matching `reward_*`
functions.

The built-in planner is breadth-first and deliberately simple. It is useful for
small enumerable action spaces, but it is not the only planning method you may
use and it is never an acceptance requirement.

Planner suggestions are aids, not constraints. You may submit any trajectory
that passes independent validation; it need not have been found by `plan.py`.
The planner can use only the complete actions exposed by the domain in
`physics-config.json`.

## Required procedure for every turn

1. Run `git status --short` and `git log --oneline -5`.
2. Read `INSTRUCTIONS.md`, `canonical-input.json`, and the latest
   `trusted-report.json` when present.
3. Inspect and revise `world_model.py`; preserve genuinely plausible competing
   hypotheses.
4. Run `python backtest.py` and resolve relevant Timeline mismatches.
5. Write one complete trajectory to `plan.json` yourself, or use an advisory
   suggestion from `python plan.py` as a starting point.
6. Run `python plan.py`. Read `plan-report.json`; the submitted plan must be valid
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

If the Actor repository becomes unusable and you deliberately want the trusted
copy restored, delete `.git` and answer without claiming a proposal. The Critic
will restore its last pulled history and overlay the latest canonical state. This
never rewinds real actions or the append-only Timeline.
"""

BACKTEST_WRAPPER = """from physics_runtime import actor_backtest

if __name__ == "__main__":
    actor_backtest()
"""
PLAN_WRAPPER = """from physics_runtime import actor_plan

if __name__ == "__main__":
    actor_plan()
"""
COMMIT_WRAPPER = """from physics_runtime import actor_commit

if __name__ == "__main__":
    actor_commit()
"""

_RUNTIME_SUPPORT = r'''


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _configuration():
    value = json.loads(Path("physics-config.json").read_text())
    required = ("planner_actions", "max_depth", "max_nodes", "evaluator_timeout_sec")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise ValueError("physics-config.json is missing required configuration")
    return value


def _request(output_path, plan=True):
    config = _configuration()
    timeline = json.loads(Path("canonical-input.json").read_text())["timeline"]
    return {
        "source": Path("world_model.py").read_text(),
        "timeline": timeline,
        "plan": json.loads(Path("plan.json").read_text()) if plan else None,
        "planner_actions": config["planner_actions"],
        "max_depth": config["max_depth"],
        "max_nodes": config["max_nodes"],
        "work_dir": ".physics-evaluation",
        "output_path": output_path,
    }, float(config["evaluator_timeout_sec"])


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run_local_evaluator(plan=True):
    request, timeout = _request(".physics-evaluation/result.json", plan)
    command = [sys.executable, str(Path(__file__).resolve()), "_evaluate"]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(
            json.dumps(request, allow_nan=False, separators=(",", ":"), sort_keys=True),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        raise SystemExit(f"Physics evaluator timed out after {timeout:g} seconds") from exc
    if process.returncode:
        detail = (stderr or stdout).strip()
        raise SystemExit(detail or f"Physics evaluator exited with status {process.returncode}")
    path = Path(request["output_path"])
    if not path.is_file():
        raise SystemExit("Physics evaluator did not write its report")
    return json.loads(path.read_text())


def actor_backtest():
    report = _run_local_evaluator(plan=False)["backtest"]
    _write_json("backtest-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_plan():
    report = _run_local_evaluator(plan=True)
    output = {
        "validation": report["plan_validation"],
        "planning": report["planning"],
    }
    _write_json("plan-report.json", output)
    print(json.dumps(output, indent=2, sort_keys=True))


def actor_commit():
    actor_plan()
    report = json.loads(Path("plan-report.json").read_text())
    if not report["validation"]["valid"]:
        raise SystemExit("plan.json is invalid; inspect plan-report.json")
    if not report["validation"]["supporting_models"]:
        raise SystemExit("plan.json has no supporting model")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", "Actor submits trajectory"], check=True)


if __name__ == "__main__" and sys.argv[1:] == ["_evaluate"]:
    exec(MODEL_RUNNER, {"__name__": "__main__"})
'''


def _runtime_source() -> str:
    return (
        "# Generated by Eggopt PhysicsStrategy. Standard-library only.\n"
        "import json\n"
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"MODEL_RUNNER = {MODEL_RUNNER!r}\n"
        + _RUNTIME_SUPPORT
    )


def instrument_files(
    *,
    planner_actions,
    max_depth: int,
    max_nodes: int,
    evaluator_timeout_sec: float,
) -> dict[str, str]:
    config = json.dumps(
        _instrument_configuration(
            planner_actions=planner_actions,
            max_depth=max_depth,
            max_nodes=max_nodes,
            evaluator_timeout_sec=evaluator_timeout_sec,
        ),
        indent=2,
        sort_keys=True,
    ) + "\n"
    return {
        "backtest.py": BACKTEST_WRAPPER,
        "commit.py": COMMIT_WRAPPER,
        "physics-config.json": config,
        "physics_runtime.py": _runtime_source(),
        "plan.py": PLAN_WRAPPER,
    }


def _instrument_configuration(
    *,
    planner_actions=(),
    max_depth: int = 8,
    max_nodes: int = 10_000,
    evaluator_timeout_sec: float = 300.0,
) -> dict[str, object]:
    return {
        "evaluator_timeout_sec": evaluator_timeout_sec,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "planner_actions": list(planner_actions),
    }


def write_actor_files(
    workspace: str | Path,
    timeline,
    domain_information: str = "",
    *,
    planner_actions=(),
    max_depth: int = 8,
    max_nodes: int = 10_000,
    evaluator_timeout_sec: float = 300.0,
    refresh_instruments: bool = True,
) -> None:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = ACTOR_INSTRUCTIONS
    if domain_information.strip():
        instructions += "\n## Domain information\n\n" + domain_information.strip() + "\n"
    _write_if_changed(workspace / "INSTRUCTIONS.md", instructions)
    _write_json(workspace / "canonical-input.json", {"timeline": timeline})
    _write_if_missing(
        workspace / ".gitignore",
        "scratch/\n__pycache__/\n*.pyc\n.physics-evaluation/\n",
    )
    _write_if_missing(workspace / "world_model.py", WORLD_MODEL_TEMPLATE)
    _write_if_missing(workspace / "plan.json", PLAN_TEMPLATE)
    if refresh_instruments:
        for name, content in instrument_files(
            planner_actions=planner_actions,
            max_depth=max_depth,
            max_nodes=max_nodes,
            evaluator_timeout_sec=evaluator_timeout_sec,
        ).items():
            _write_if_changed(workspace / name, content)


def actor_backtest() -> None:
    document = _run_local_evaluator(plan=False)
    report = document["backtest"]
    _write_json(Path("backtest-report.json"), report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_plan() -> None:
    document = _run_local_evaluator(plan=True)
    report = {
        "validation": document["plan_validation"],
        "planning": document["planning"],
    }
    _write_json(Path("plan-report.json"), report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_commit() -> None:
    import subprocess

    actor_plan()
    report = json.loads(Path("plan-report.json").read_text())
    if not report["validation"]["valid"]:
        raise SystemExit("plan.json is invalid; inspect plan-report.json")
    if not report["validation"]["supporting_models"]:
        raise SystemExit("plan.json has no supporting model")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", "Actor submits trajectory"], check=True)


def _run_local_evaluator(plan=True):
    from .theory import MODEL_RUNNER, parse_evaluator_output

    workspace = Path.cwd()
    config = json.loads((workspace / "physics-config.json").read_text())
    request = {
        "source": (workspace / "world_model.py").read_text(),
        "timeline": json.loads((workspace / "canonical-input.json").read_text())["timeline"],
        "plan": json.loads((workspace / "plan.json").read_text()) if plan else None,
        "planner_actions": config["planner_actions"],
        "max_depth": config["max_depth"],
        "max_nodes": config["max_nodes"],
    }
    from contextlib import redirect_stdout
    from io import StringIO

    output = StringIO()
    with redirect_stdout(output):
        import sys

        previous = sys.stdin
        try:
            sys.stdin = StringIO(json.dumps(request))
            exec(MODEL_RUNNER, {"__name__": "__main__"})  # noqa: S102
        finally:
            sys.stdin = previous
    return parse_evaluator_output(output.getvalue())


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_if_missing(path, content):
    if not path.exists():
        path.write_text(content)


def _write_if_changed(path, content):
    if not path.exists() or path.read_text() != content:
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
    "actor_backtest",
    "actor_commit",
    "actor_plan",
    "ensure_evaluator_ignore",
    "instrument_files",
    "write_actor_files",
]
