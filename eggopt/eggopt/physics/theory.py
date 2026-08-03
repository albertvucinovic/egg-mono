from __future__ import annotations

import json
from typing import Any

MODEL_RUNNER = r"""
import copy
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
raw_plan = request.get("plan")
actions = request.get("planner_actions", [])
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
orphan_rewards = sorted(set(rewards) - set(steps))
if orphan_rewards:
    raise ValueError(f"reward suffixes require matching steps: {orphan_rewards}")
if not isinstance(timeline, list) or not timeline:
    raise ValueError("timeline must be a non-empty list")
if not isinstance(actions, list):
    raise TypeError("planner_actions must be a finite list")
max_depth = request["max_depth"]
max_nodes = request["max_nodes"]
if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
    raise ValueError("max_depth must be a positive integer")
if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
    raise ValueError("max_nodes must be a positive integer")


def freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def canonical_plan(value):
    if not isinstance(value, list) or not value:
        raise ValueError("plan must be a non-empty JSON list")
    plan = []
    for transition in value:
        if not isinstance(transition, dict) or set(transition) != {"state", "action", "next_state"}:
            raise ValueError("every plan transition must contain exactly state, action, and next_state")
        plan.append({"state": transition["state"], "action": transition["action"], "next_state": transition["next_state"]})
    for previous, current in zip(plan, plan[1:]):
        if previous["next_state"] != current["state"]:
            raise ValueError("plan transitions must form one continuous trajectory")
    return plan


def predict(step, state, action):
    state_arg = copy.deepcopy(state)
    action_arg = copy.deepcopy(action)
    state_before = copy.deepcopy(state_arg)
    action_before = copy.deepcopy(action_arg)
    predicted = step(state_arg, action_arg)
    if state_arg != state_before or action_arg != action_before:
        raise ValueError("step functions must not mutate their arguments")
    return predicted


def finite_reward(reward, state):
    value = float(reward(copy.deepcopy(state)))
    if not math.isfinite(value):
        raise ValueError("reward must be finite")
    return value


reports = {}
for suffix, step in sorted(steps.items()):
    mismatches = []
    matches = 0
    for index, item in enumerate(timeline[1:], start=1):
        try:
            predicted = predict(step, item["state"], item["action"])
            if predicted == item["next_state"]:
                matches += 1
            else:
                mismatches.append({"transition": index, "prediction": predicted, "actual": item["next_state"]})
        except (TypeError, ValueError, RuntimeError, KeyError, AttributeError) as exc:
            mismatches.append({"transition": index, "error": str(exc)})
    reports[suffix] = {"matches": matches, "mismatches": mismatches}

surviving = [suffix for suffix, report in reports.items() if not report["mismatches"]]
current = timeline[-1].get("next_state", timeline[-1])
plan = None
plan_error = None
supporting = []
plan_predictions = []
plan_model_errors = []
if raw_plan is not None:
    try:
        plan = canonical_plan(raw_plan)
        if len(plan) > max_depth:
            raise ValueError(f"plan has {len(plan)} transitions; limit is {max_depth}")
        if plan[0]["state"] != current:
            raise ValueError("the first plan state must equal the canonical current state")
        plan_predictions = []
        for item in plan:
            predicted = {}
            errors = {}
            for suffix in surviving:
                try:
                    predicted[suffix] = predict(
                        steps[suffix], item["state"], item["action"]
                    )
                except (TypeError, ValueError, RuntimeError, KeyError, AttributeError) as exc:
                    predicted[suffix] = None
                    errors[suffix] = str(exc)
            plan_predictions.append(predicted)
            plan_model_errors.append(errors)
        for suffix in surviving:
            if all(
                predictions[suffix] == item["next_state"]
                for predictions, item in zip(plan_predictions, plan)
            ):
                supporting.append(suffix)
        if not supporting:
            raise ValueError("no Timeline-consistent step model reproduces the complete plan")
    except (TypeError, ValueError, RuntimeError, KeyError, AttributeError) as exc:
        plan_error = str(exc)


def goal_suggestion(suffix):
    step = steps[suffix]
    reward = rewards[suffix]
    baseline = finite_reward(reward, current)
    best_reward = baseline
    best_trajectory = None
    frontier = deque([(current, ())])
    seen = {freeze(current)}
    nodes = 0
    while frontier and nodes < max_nodes:
        state, trajectory = frontier.popleft()
        nodes += 1
        if trajectory:
            value = finite_reward(reward, state)
            if value > best_reward:
                best_reward = value
                best_trajectory = trajectory
        if len(trajectory) >= max_depth:
            continue
        for action in actions:
            next_state = predict(step, state, action)
            key = freeze(next_state)
            if key in seen:
                continue
            seen.add(key)
            frontier.append((next_state, trajectory + ({"state": state, "action": action, "next_state": next_state},)))
    return list(best_trajectory) if best_trajectory is not None else None


def distinction_suggestion(left, right):
    left_step = steps[left]
    right_step = steps[right]
    frontier = deque([(current, current, ())])
    seen = {(freeze(current), freeze(current))}
    nodes = 0
    while frontier and nodes < max_nodes:
        left_state, right_state, trajectory = frontier.popleft()
        nodes += 1
        if left_state != right_state or len(trajectory) >= max_depth:
            continue
        for action in actions:
            left_next = predict(left_step, left_state, action)
            right_next = predict(right_step, right_state, action)
            next_transition = {"state": left_state, "action": action, "next_state": left_next}
            next_trajectory = trajectory + (next_transition,)
            if left_next != right_next:
                return list(next_trajectory)
            key = (freeze(left_next), freeze(right_next))
            if key not in seen:
                seen.add(key)
                frontier.append((left_next, right_next, next_trajectory))
    return None

suggestions = []
planning_models = [suffix for suffix in surviving if suffix in rewards]
for suffix in planning_models:
    suggestion = goal_suggestion(suffix)
    if suggestion:
        suggestions.append({"kind": "reward", "models": [suffix], "plan": suggestion})
for left, right in itertools.combinations(planning_models, 2):
    suggestion = distinction_suggestion(left, right)
    if suggestion:
        suggestions.append({"kind": "distinction", "models": [left, right], "plan": suggestion})

result = {
    "backtest": {"models": reports, "surviving_models": surviving},
    "plan_validation": {
        "valid": plan is not None and plan_error is None,
        "error": plan_error,
        "supporting_models": supporting,
        "predictions": plan_predictions,
        "model_errors": plan_model_errors,
        "plan": plan,
    },
    "planning": {
        "eligible_models": planning_models,
        "suggestions": suggestions,
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


def evaluator_file_script(request_path: str) -> str:
    """Return a compact evaluator that loads its potentially large inputs from files."""

    request_literal = repr(str(request_path))
    loader = (
        "import io, json, sys\n"
        "from pathlib import Path\n"
        f"request = json.loads(Path({request_literal}).read_text())\n"
        'request["source"] = Path(request.pop("source_path")).read_text()\n'
        'timeline = json.loads(Path(request.pop("timeline_path")).read_text())\n'
        'request["timeline"] = timeline["timeline"]\n'
        'request["plan"] = json.loads(Path(request.pop("plan_path")).read_text())\n'
        "sys.stdin = io.StringIO(json.dumps(request, allow_nan=False, "
        'separators=(",", ":"), sort_keys=True))\n'
    )
    return loader + MODEL_RUNNER


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


__all__ = [
    "evaluator_file_script",
    "evaluator_script",
    "parse_evaluator_output",
    "parse_evaluator_receipt",
]
