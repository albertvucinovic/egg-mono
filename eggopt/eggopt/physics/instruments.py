from __future__ import annotations

import json
from pathlib import Path

from .planning import canonical_plan
from .theory import MODEL_RUNNER

WORLD_MODEL_TEMPLATE = '''"""Current competing hypotheses for one observed world.

Each hypothesis is a matching ``step_<suffix>`` and ``reward_<suffix>`` pair.
``step_*`` predicts the next complete public state from state plus action.
``reward_*`` returns a finite utility and thereby defines that model's goal.
"""


def step_1(state, action):
    raise NotImplementedError


def reward_1(state):
    return 0.0
'''

ACTOR_INSTRUCTIONS = """# Physics Actor runbook

## Your role

You are the theorist and planner in an iterative scientific-discovery loop. The
environment provides observations and legal actions, but it may not explain the
objects, mechanisms, or goal. Infer all three. Reality is authoritative; your
current theory is provisional.

Work like a physicist:

1. **Ground the state.** Decide which parts of each raw observation are useful
   entities, variables, and relations. A persistent prediction failure may mean
   that this representation—not just a transition rule—is wrong.
2. **Discover mechanisms.** Write executable hypotheses for how a legal action
   transforms one complete public state into the next.
3. **Infer utility.** State what progress means under each hypothesis. The goal
   is unknown to you even when the application has a private completion check.
4. **Test every belief against all recorded reality.** Preserve plausible
   alternatives instead of silently choosing one unsupported story.
5. **Plan in the model.** Prefer a short goal-directed plan when a model is
   credible. When important alternatives remain, prefer a short experiment
   whose predicted outcomes distinguish them.
6. **Commit before reality changes.** Freeze the chosen actions and exact
   predictions in Git. The trusted Critic—not you—then validates the commit and
   decides whether any real actions may run.

Your working directory is a Git repository. One Actor turn is one theory/plan
proposal, and it must end in one new, clean Git commit. Your chat answer is only
a brief completion signal; files at committed HEAD are the proposal.

## What is in this repository

- `INSTRUCTIONS.md`: this runbook plus application-specific domain information.
- `canonical-input.json`: the complete authoritative Timeline copied in for this
  turn. The first item is the initial observation. Every later item is an
  append-only `{state, action, next_state}` transition.
- `trusted-report.json`: when present, the previous Critic validation/execution
  report. It explains the last failure or the newest real evidence.
- `world_model.py`: your editable program of competing world-model hypotheses.
- `physics_runtime.py`: the generated, standard-library-only generic Physics
  instrument. It contains no domain implementation or hidden environment state.
- `physics-config.json`: the public planning limits for this Physics study.
- `backtest.py`: an untrusted local preview of the canonical backtest.
- `plan.py`: an untrusted local preview of canonical goal and experiment search.
- `backtest-report.json` and `plan-report.json`: regenerated local reports.
- `commit.py`: selects one plan from the latest plan report and commits the turn.
- `committed-plan.json`: the plan selected by `commit.py`; do not hand-invent a
  plan that `plan.py` did not return.
- `.trusted/`: Critic-owned synchronization state. It is not your scratch area.
- `scratch/`: ignored workspace for notes, visualizations, and temporary code.

You may inspect normal Git history and diffs to understand how theory and
evidence evolved. Never inspect hidden environment implementation/state or Egg's
internal `.egg` data. Do not call the real environment directly.

## The world-model contract

`world_model.py` is the single editable program containing both state grounding
and mechanisms. Define one or more matching function pairs:

```python
def step_<suffix>(state, action):
    # Return the complete predicted next public state.
    ...

def reward_<suffix>(state):
    # Return a finite numeric utility under this hypothesis.
    ...
```

The non-empty suffix names a hypothesis; for example, `step_door` pairs only
with `reward_door`. Every step must have one reward with the same suffix and no
reward may be orphaned.

For each hypothesis:

- `step_*` must be deterministic for the same inputs, must not mutate its
  arguments, and must return the **complete** public state—not merely a latent
  summary or changed fields.
- The returned state must expose the configured legal-actions field. Search
  expands only actions that your simulated state declares legal.
- `reward_*` must return a finite number. The generic planner searches for a
  reachable state with utility strictly greater than the current state's
  utility; encode inferred progress accordingly.
- Keep genuinely plausible alternatives as separate suffixes. Repair or remove
  hypotheses contradicted by the Timeline, but use counterexamples to reconsider
  both the representation and the mechanism before adding patches.
- The model is untrusted code. Local success is advice, not authorization.

Timeline transitions record the **committed intent** in their `action` field,
not necessarily a bare action identifier. Read the domain section to learn its
shape. A hypothesis is historically consistent only when
`step_*(transition["state"], transition["action"])` exactly equals the recorded
`transition["next_state"]` for every real transition.

## The plan contract

The planner emits only non-empty canonical plans:

```json
{
  "purpose": "goal or experiment",
  "models": ["suffix", "..."],
  "intents": [
    {
      "action": "domain action value",
      "prediction": {
        "suffix": "complete predicted next public state"
      }
    }
  ]
}
```

Every intent predicts exactly once for every suffix listed in `models`.

- A **goal** plan is produced for one hypothesis when search finds a path to
  strictly higher reward.
- An **experiment** plan is produced for two or more hypotheses when search finds
  a common legal action sequence whose predictions eventually diverge. It may
  contain a shared setup prefix. The first divergent intent is executed too; the
  resulting observation is the experiment's evidence.

The generic planner searches only to its configured depth/node limits. If it
returns no useful plan, revise the representation, transition functions, reward
functions, or model set; do not fabricate `committed-plan.json`.

## Required procedure for every turn

1. Run `git status --short` and `git log --oneline -5`. Establish the committed
   starting point and inspect the newest controller/Critic commit.
2. Read `INSTRUCTIONS.md`, `canonical-input.json`, and, if present,
   `trusted-report.json`. Treat the Timeline as immutable evidence. Focus on
   transitions or predictions implicated by the latest report.
3. Inspect `world_model.py` and the previous committed plan. State several
   plausible explanations when evidence underdetermines the mechanism.
4. Edit `world_model.py`. You may create helpers inside it and use `scratch/` for
   analysis, but the matching `step_*`/`reward_*` pairs are the evaluated API.
5. Run `python backtest.py`. Read `backtest-report.json`. For every mismatch,
   determine whether the representation, mechanism, action interpretation, or
   goal hypothesis is wrong. Repeat editing and backtesting until at least the
   models you intend to use explain the full Timeline.
6. Run `python plan.py`. Read `plan-report.json`, including `goal_plans`,
   `discrimination_plans`, and `canonical_plans`. Prefer useful progress with few
   real actions: a robust goal plan when justified, otherwise an informative
   experiment. Repeat model/backtest/plan work as needed.
7. Choose one listed `plan-N` and run `python commit.py plan-N`. This writes the
   exact corresponding `committed-plan.json`, stages all non-ignored changes,
   and creates the required Actor commit.
8. Run `git status --short` and `git show --stat --oneline HEAD`. The status must
   be empty and HEAD must be the new commit for this turn. Make **no edits after
   `commit.py`**. Then answer briefly that the committed proposal is ready.

Never stop after merely explaining what you would do. Do the file edits,
instrument runs, plan selection, and commit in the same turn. Never execute a
real action yourself.

## What happens after you answer

The trusted Critic operates on committed Git history, not your live reasoning:

1. It rejects a dirty repository or a turn that did not create a new commit.
2. It pulls the exact Actor HEAD into an independent Critic repository.
3. It loads committed `world_model.py` and independently reruns the original
   evaluator in the Critic Eggthread sandbox against the canonical Timeline.
   Your editable helper scripts and local reports are not trusted inputs.
4. It requires matching model pairs, valid finite rewards, a valid non-empty
   `committed-plan.json`, and exact equality with one independently generated
   canonical plan. Every selected model must survive the full backtest, and the
   first action must be legal in the actual current state.
5. Only after those checks does it execute intents through the trusted domain
   adapter. Each real transition is appended permanently to the Timeline.
6. Execution stops immediately on a wrong prediction, after the first intent
   where the selected models predicted different outcomes, when the plan ends,
   when the trusted application reports the goal, or when the action budget is
   exhausted.
7. The Critic writes the new canonical input and report, commits trusted state,
   synchronizes that commit back here, and either asks this same persistent Actor
   to revise or accepts the run. Wrong predictions and discriminating outcomes
   are evidence, not exceptional failures.

Typical resolutions are:

- `wrong_prediction`: no selected prediction matched reality; revise grounding
  or mechanism using the appended counterexample.
- `models_discriminated`: a branching experiment ran; retain/revise hypotheses
  according to the observed branch and replan.
- `plan_exhausted`: the predicted plan ran without a trusted win; revise the
  reward/goal theory or extend the mechanism and plan.
- `won`: the trusted application detected completion; the run is accepted.
- `max_actions`: the irreversible real-action budget is exhausted; the run ends.

Validation errors also return for revision with details in
`trusted-report.json`. Address the actual report rather than hiding or editing
the evidence.

## Git, caching, and recovery

Git is part of the protocol, not bookkeeping. Each Actor turn is one proposal
identified by its commit HEAD; trusted evaluation is cached by that HEAD plus
the evaluator configuration. Reusing an already evaluated HEAD may replay the
cached result. A new commit is therefore required for a new proposal, while
irrelevant commits waste evaluation budget.

Commit every intended non-ignored file and leave no dirty files. Put disposable
work under `scratch/` or add an appropriate ignore rule before the final commit.
Do not rewrite, reset, or amend trusted history merely to make evidence vanish.

If the Actor repository becomes unusable and you deliberately want the trusted
copy restored, delete `.git` and answer without pretending to have submitted a
proposal. The Critic will restore its last pulled history and overlay the latest
canonical state, then ask you for a fresh clean commit. This can discard local
work and **never** rewinds real actions or the append-only Timeline.
"""

BACKTEST_WRAPPER = """from physics_runtime import actor_backtest

if __name__ == "__main__":
    actor_backtest()
"""
PLAN_WRAPPER = """from physics_runtime import actor_plan

if __name__ == "__main__":
    actor_plan()
"""
COMMIT_WRAPPER = """import sys
from physics_runtime import actor_commit

if __name__ == "__main__":
    actor_commit(sys.argv[1] if len(sys.argv) > 1 else "")
"""

_RUNTIME_SUPPORT = r'''


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _configuration():
    value = json.loads(Path("physics-config.json").read_text())
    required = ("legal_actions_key", "max_depth", "max_nodes", "evaluator_timeout_sec")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise ValueError("physics-config.json is missing required configuration")
    return value


def _request(output_path):
    config = _configuration()
    timeline = json.loads(Path("canonical-input.json").read_text())["timeline"]
    return {
        "source": Path("world_model.py").read_text(),
        "timeline": timeline,
        "legal_actions_key": config["legal_actions_key"],
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


def _run_local_evaluator():
    request, timeout = _request(".physics-evaluation/result.json")
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


def canonical_plan(value):
    if not isinstance(value, dict) or set(value) != {"purpose", "models", "intents"}:
        raise ValueError("plan must contain exactly purpose, models, and intents")
    if value["purpose"] not in {"goal", "experiment"}:
        raise ValueError("plan purpose must be goal or experiment")
    models = value["models"]
    intents = value["intents"]
    if not isinstance(models, list) or not models or not all(
        isinstance(item, str) and item for item in models
    ):
        raise ValueError("plan models must be a non-empty string list")
    if not isinstance(intents, list) or not intents:
        raise ValueError("committed plan must contain at least one intent")
    for intent in intents:
        if not isinstance(intent, dict) or "action" not in intent:
            raise ValueError("every intent must contain an action")
        predictions = intent.get("prediction")
        if not isinstance(predictions, dict) or set(predictions) != set(models):
            raise ValueError("every intent must predict once for every plan model")
    return {"purpose": value["purpose"], "models": models, "intents": intents}


def actor_backtest():
    report = _run_local_evaluator()["backtest"]
    _write_json("backtest-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_plan():
    planning = _run_local_evaluator()["planning"]
    plans = [
        {"plan_id": f"plan-{index}", "plan": canonical_plan(plan)}
        for index, plan in enumerate(planning["plans"], start=1)
    ]
    report = {**planning, "canonical_plans": plans}
    _write_json("plan-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_commit(plan_id):
    report_path = Path("plan-report.json")
    if not report_path.is_file():
        raise SystemExit("Run python plan.py before commit.py")
    report = json.loads(report_path.read_text())
    selected = next(
        (
            item["plan"]
            for item in report.get("canonical_plans", ())
            if item["plan_id"] == plan_id
        ),
        None,
    )
    if selected is None:
        raise SystemExit(f"Unknown plan_id: {plan_id!r}")
    _write_json("committed-plan.json", canonical_plan(selected))
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"Actor commits {plan_id}"], check=True)


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
    legal_actions_key: str,
    max_depth: int,
    max_nodes: int,
    evaluator_timeout_sec: float,
) -> dict[str, str]:
    """Return the generic, domain-free Actor instrument bundle."""

    config = json.dumps(
        _instrument_configuration(
            legal_actions_key=legal_actions_key,
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
    legal_actions_key: str = "legal_actions",
    max_depth: int = 8,
    max_nodes: int = 10_000,
    evaluator_timeout_sec: float = 300.0,
) -> dict[str, object]:
    """Return the public configuration shared by local and trusted evaluators."""

    return {
        "evaluator_timeout_sec": evaluator_timeout_sec,
        "legal_actions_key": legal_actions_key,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    }


def write_actor_files(
    workspace: str | Path,
    timeline,
    domain_information: str = "",
    *,
    legal_actions_key: str = "legal_actions",
    max_depth: int = 8,
    max_nodes: int = 10_000,
    evaluator_timeout_sec: float = 300.0,
    refresh_instruments: bool = True,
) -> None:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = ACTOR_INSTRUCTIONS
    if domain_information.strip():
        instructions += (
            "\n## Domain information\n\n" + domain_information.strip() + "\n"
        )
    _write_if_changed(workspace / "INSTRUCTIONS.md", instructions)
    _write_json(workspace / "canonical-input.json", {"timeline": timeline})
    _write_if_missing(
        workspace / ".gitignore",
        "scratch/\n__pycache__/\n*.pyc\n.physics-evaluation/\n",
    )
    _write_if_missing(workspace / "world_model.py", WORLD_MODEL_TEMPLATE)
    if refresh_instruments:
        for name, content in instrument_files(
            legal_actions_key=legal_actions_key,
            max_depth=max_depth,
            max_nodes=max_nodes,
            evaluator_timeout_sec=evaluator_timeout_sec,
        ).items():
            _write_if_changed(workspace / name, content)


def actor_backtest() -> None:
    document = _run_local_evaluator()
    report = document["backtest"]
    _write_json(Path("backtest-report.json"), report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_plan() -> None:
    document = _run_local_evaluator()
    planning = document["planning"]
    plans = [
        {"plan_id": f"plan-{index}", "plan": canonical_plan(plan)}
        for index, plan in enumerate(planning["plans"], start=1)
    ]
    report = {**planning, "canonical_plans": plans}
    _write_json(Path("plan-report.json"), report)
    print(json.dumps(report, indent=2, sort_keys=True))


def actor_commit(plan_id: str) -> None:
    import subprocess

    report_path = Path("plan-report.json")
    if not report_path.is_file():
        raise SystemExit("Run python plan.py before commit.py")
    report = json.loads(report_path.read_text())
    selected = next(
        (
            item["plan"]
            for item in report.get("canonical_plans", ())
            if item["plan_id"] == plan_id
        ),
        None,
    )
    if selected is None:
        raise SystemExit(f"Unknown plan_id: {plan_id!r}")
    canonical_plan(selected)
    _write_json(Path("committed-plan.json"), selected)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"Actor commits {plan_id}"], check=True)


def _run_local_evaluator():
    from .theory import MODEL_RUNNER, parse_evaluator_output

    workspace = Path.cwd()
    timeline = json.loads((workspace / "canonical-input.json").read_text())["timeline"]
    request = {
        "source": (workspace / "world_model.py").read_text(),
        "timeline": timeline,
        "legal_actions_key": "legal_actions",
        "max_depth": 8,
        "max_nodes": 10_000,
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
    """Keep generated evaluator state out of Actor proposals."""

    path = Path(workspace) / ".gitignore"
    lines = path.read_text().splitlines() if path.is_file() else []
    if ".physics-evaluation/" not in lines:
        lines.append(".physics-evaluation/")
        path.write_text("\n".join(lines) + "\n")


__all__ = [
    "ACTOR_INSTRUCTIONS",
    "WORLD_MODEL_TEMPLATE",
    "actor_backtest",
    "actor_commit",
    "actor_plan",
    "ensure_evaluator_ignore",
    "instrument_files",
    "write_actor_files",
]
