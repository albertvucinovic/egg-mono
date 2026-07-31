from __future__ import annotations

import json
from pathlib import Path

from .planning import canonical_plan

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

ACTOR_INSTRUCTIONS = """# Physics Actor

`world_model.py` is your current theory. Improve it. Every hypothesis is a
matching `step_<suffix>(state, action)` and `reward_<suffix>(state)` pair.
Canonical evidence is copied into `canonical-input.json` as an initial state and
append-only `{state, action, next_state}` transitions.

Use the supplied instruments:

- `python backtest.py`: evaluate all model hypotheses over the Timeline.
- `python plan.py`: find goal plans for every valid model and shortest
  discrimination plans for model subsets, including common setup prefixes.
- `python commit.py PLAN_ID`: select a canonical non-empty plan from the latest
  plan report, write `committed-plan.json`, and make the Git commit. This must be
  your final mutation before answering; the Critic rejects a dirty repository.

An intent combines an action with predictions keyed by model suffix. You cannot
execute real actions. The trusted Critic pulls HEAD and independently repeats the
pipeline in its assigned Eggthread sandbox. Deleting `.git` requests restoration
from the Critic history; it never rewinds canonical reality.
"""

BACKTEST_WRAPPER = """from eggopt.physics import actor_backtest

if __name__ == "__main__":
    actor_backtest()
"""
PLAN_WRAPPER = """from eggopt.physics import actor_plan

if __name__ == "__main__":
    actor_plan()
"""
COMMIT_WRAPPER = """import sys
from eggopt.physics import actor_commit

if __name__ == "__main__":
    actor_commit(sys.argv[1] if len(sys.argv) > 1 else "")
"""


def write_actor_files(
    workspace: str | Path, timeline, domain_information: str = ""
) -> None:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = ACTOR_INSTRUCTIONS
    if domain_information.strip():
        instructions += (
            "\n## Domain information\n\n" + domain_information.strip() + "\n"
        )
    _write_if_missing(workspace / "INSTRUCTIONS.md", instructions)
    _write_json(workspace / "canonical-input.json", {"timeline": timeline})
    _write_if_missing(workspace / ".gitignore", "scratch/\n__pycache__/\n*.pyc\n")
    _write_if_missing(workspace / "world_model.py", WORLD_MODEL_TEMPLATE)
    _write_if_missing(workspace / "backtest.py", BACKTEST_WRAPPER)
    _write_if_missing(workspace / "plan.py", PLAN_WRAPPER)
    _write_if_missing(workspace / "commit.py", COMMIT_WRAPPER)


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


__all__ = [
    "ACTOR_INSTRUCTIONS",
    "WORLD_MODEL_TEMPLATE",
    "actor_backtest",
    "actor_commit",
    "actor_plan",
    "write_actor_files",
]
