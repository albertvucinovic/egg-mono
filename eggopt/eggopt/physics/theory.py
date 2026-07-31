from __future__ import annotations

import json
from typing import Any

MODEL_RUNNER = r"""
import hashlib
import importlib.util
import itertools
import json
import math
import os
import sys
import tempfile
from collections import deque
from pathlib import Path

request = json.loads(sys.stdin.read())
source = request["source"]
timeline = request["timeline"]
work = Path(request.get("work_dir", ".physics-evaluation"))
work.mkdir(parents=True, exist_ok=True)
path = work / ("world_model_" + hashlib.sha256(source.encode()).hexdigest()[:12] + ".py")
path.write_text(source)
spec = importlib.util.spec_from_file_location("physics_world_model", path)
if spec is None or spec.loader is None:
    raise ValueError("world model could not be loaded")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
steps = {name[5:]: value for name, value in vars(module).items() if name.startswith("step_") and name[5:] and callable(value)}
rewards = {name[7:]: value for name, value in vars(module).items() if name.startswith("reward_") and name[7:] and callable(value)}
if not steps:
    raise ValueError("world_model.py defines no step_<suffix> functions")
missing = sorted(set(steps) - set(rewards))
orphan = sorted(set(rewards) - set(steps))
if missing or orphan:
    raise ValueError(f"step/reward suffixes must match; missing rewards={missing}, orphan rewards={orphan}")
models = {suffix: (steps[suffix], rewards[suffix]) for suffix in sorted(steps)}

def freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value

def legal(state):
    if not isinstance(state, dict):
        raise TypeError("model states must expose legal actions in a mapping")
    return tuple(request["legal_actions"].get(str(action), ()) if False else state.get(request["legal_actions_key"], ()))

def goal_plan(step, reward, start):
    frontier = deque([(start, ())])
    seen = {freeze(start)}
    baseline = float(reward(start))
    if not math.isfinite(baseline):
        raise ValueError("reward must be finite")
    best = (baseline, ())
    nodes = 0
    while frontier and nodes < request["max_nodes"]:
        state, path = frontier.popleft()
        nodes += 1
        score = float(reward(state))
        if not math.isfinite(score):
            raise ValueError("reward must be finite")
        if score > best[0]:
            best = (score, path)
        if len(path) >= request["max_depth"]:
            continue
        for action in legal(state):
            next_state = step(state, action)
            key = freeze(next_state)
            if key in seen:
                continue
            seen.add(key)
            frontier.append((next_state, path + ({"action": action, "prediction": next_state},)))
    return list(best[1]) if best[0] > baseline and best[1] else None

def discriminate(subset, start):
    frontier = deque([({suffix: start for suffix in subset}, ())])
    seen = {freeze({suffix: start for suffix in subset})}
    nodes = 0
    while frontier and nodes < request["max_nodes"]:
        states, path = frontier.popleft()
        nodes += 1
        if len(path) >= request["max_depth"]:
            continue
        sets = [set(legal(state)) for state in states.values()]
        common = set.intersection(*sets) if sets else set()
        for action in sorted(common, key=repr):
            next_states = {suffix: models[suffix][0](states[suffix], action) for suffix in subset}
            intent = {"action": action, "prediction": next_states}
            next_path = path + (intent,)
            if len({freeze(value) for value in next_states.values()}) > 1:
                return list(next_path)
            key = freeze(next_states)
            if key not in seen:
                seen.add(key)
                frontier.append((next_states, next_path))
    return None

reports = {}
for suffix, (step, reward) in models.items():
    mismatches = []
    matches = 0
    for index, item in enumerate(timeline[1:], start=1):
        try:
            predicted = step(item["state"], item["action"])
            if predicted == item["next_state"]:
                matches += 1
            else:
                mismatches.append({"transition": index, "prediction": predicted, "actual": item["next_state"]})
        except (TypeError, ValueError, RuntimeError, KeyError, AttributeError) as exc:
            mismatches.append({"transition": index, "error": str(exc)})
    reports[suffix] = {"matches": matches, "mismatches": mismatches}

current = timeline[-1].get("next_state", timeline[-1])
goals = {suffix: goal_plan(step, reward, current) for suffix, (step, reward) in models.items()}
discrimination = []
suffixes = tuple(models)
for size in range(2, len(suffixes) + 1):
    for subset in itertools.combinations(suffixes, size):
        plan = discriminate(subset, current)
        if plan is not None:
            discrimination.append({"models": list(subset), "plan": plan})
plans = []
for suffix, plan in goals.items():
    if plan:
        plans.append({"purpose": "goal", "models": [suffix], "intents": [{"action": item["action"], "prediction": {suffix: item["prediction"]}} for item in plan]})
for item in discrimination:
    plans.append({"purpose": "experiment", "models": item["models"], "intents": item["plan"]})
result = {
    "backtest": {
        "models": reports,
        "surviving_models": [suffix for suffix, report in reports.items() if not report["mismatches"]],
    },
    "planning": {
        "goal_plans": goals,
        "discrimination_plans": discrimination,
        "plans": plans,
    },
}
output_path = request.get("output_path")
if output_path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + output.name + ".", dir=output.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            handle = -1
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except Exception:
        if handle >= 0:
            os.close(handle)
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
        raise
    print("__EGG_PHYSICS_REPORT__" + json.dumps({"path": str(output)}, separators=(",", ":"), sort_keys=True))
else:
    print("__EGG_PHYSICS_RESULT__" + json.dumps(result, separators=(",", ":"), sort_keys=True))
"""


def evaluator_script(request: dict[str, Any]) -> str:
    """Return a self-contained trusted evaluator script for ``python_exec``."""

    payload = json.dumps(
        request, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return f"import io, sys\nsys.stdin = io.StringIO({payload!r})\n" + MODEL_RUNNER


def parse_evaluator_output(output: str) -> dict[str, Any]:
    marker = "__EGG_PHYSICS_RESULT__"
    line = next(
        (line for line in reversed(str(output).splitlines()) if marker in line), None
    )
    if line is None:
        raise ValueError(f"trusted model evaluator failed:\n{output}")
    try:
        return json.loads(line.split(marker, 1)[1])
    except json.JSONDecodeError as exc:
        raise ValueError("trusted model evaluator returned invalid JSON") from exc


def parse_evaluator_receipt(output: str) -> str:
    """Return the report path from a compact trusted-evaluator receipt."""

    marker = "__EGG_PHYSICS_REPORT__"
    line = next(
        (line for line in reversed(str(output).splitlines()) if marker in line), None
    )
    if line is None:
        raise ValueError(f"trusted model evaluator did not write its report:\n{output}")
    try:
        path = json.loads(line.split(marker, 1)[1])["path"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("trusted model evaluator returned an invalid receipt") from exc
    if not isinstance(path, str) or not path:
        raise ValueError("trusted model evaluator returned an invalid report path")
    return path


__all__ = ["evaluator_script", "parse_evaluator_output", "parse_evaluator_receipt"]
