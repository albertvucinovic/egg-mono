from __future__ import annotations

import json
from typing import Any

MODEL_RUNNER = r"""
import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

request = json.loads(sys.stdin.read())
source = request["source"]
timeline = request["timeline"]
raw_plans = request.get("plans", [])
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
actions = {name[8:]: value for name, value in vars(module).items() if name.startswith("actions_") and name[8:] and callable(value)}
if not steps:
    raise ValueError("world_model.py defines no step_<suffix> functions")
orphan_actions = sorted(set(actions) - set(steps))
if orphan_actions:
    raise ValueError(f"actions suffixes require matching steps; orphan actions={orphan_actions}")
models = {suffix: (steps[suffix], actions.get(suffix)) for suffix in sorted(steps)}

if not isinstance(timeline, list) or not timeline:
    raise ValueError("timeline must be a non-empty list")
if not isinstance(raw_plans, list):
    raise TypeError("plans must be a finite list")
max_depth = request["max_depth"]
max_nodes = request["max_nodes"]
if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
    raise ValueError("max_depth must be a positive integer")
if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
    raise ValueError("max_nodes must be a positive integer")
validation_nodes = sum(max(1, len(plan.get("intents", ()))) if isinstance(plan, dict) else 1 for plan in raw_plans)
if validation_nodes > max_nodes:
    raise ValueError(f"submitted plans require {validation_nodes} validation nodes; limit is {max_nodes}")


def freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def canonical_plan(value):
    if not isinstance(value, dict) or set(value) != {"purpose", "models", "intents"}:
        raise ValueError("plan must contain exactly purpose, models, and intents")
    if value["purpose"] not in {"goal", "experiment"}:
        raise ValueError("plan purpose must be goal or experiment")
    selected = value["models"]
    intents = value["intents"]
    if not isinstance(selected, list) or not selected or not all(isinstance(item, str) and item for item in selected):
        raise ValueError("plan models must be a non-empty string list")
    if len(set(selected)) != len(selected):
        raise ValueError("plan models must be unique")
    if value["purpose"] == "goal" and len(selected) != 1:
        raise ValueError("goal plans must use exactly one model")
    if value["purpose"] == "experiment" and len(selected) < 2:
        raise ValueError("experiment plans must use at least two models")
    if not isinstance(intents, list) or not intents:
        raise ValueError("committed plan must contain at least one intent")
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != {"action", "prediction"}:
            raise ValueError("every intent must contain exactly action and prediction")
        predictions = intent["prediction"]
        if not isinstance(predictions, dict) or set(predictions) != set(selected):
            raise ValueError("every intent must predict once for every plan model")
    if value["purpose"] == "experiment":
        for index, intent in enumerate(intents):
            distinct = len({freeze(item) for item in intent["prediction"].values()})
            if index < len(intents) - 1 and distinct != 1:
                raise ValueError("experiment predictions must share one common prefix")
            if index == len(intents) - 1 and distinct < 2:
                raise ValueError("an experiment must end with its first distinguishing action")
    return {"purpose": value["purpose"], "models": selected, "intents": intents}


def recorded_action(value):
    if isinstance(value, dict) and set(value) == {"action", "prediction"} and isinstance(value["prediction"], dict):
        return value["action"]
    return value


def action_map(state, generate=None):
    if not isinstance(state, dict):
        raise TypeError("model states must expose legal actions in a mapping")
    values = generate(copy.deepcopy(state)) if generate is not None else state.get(request["legal_actions_key"], ())
    if not isinstance(values, (list, tuple)):
        raise TypeError("legal actions must be a finite list or tuple")
    return {freeze(action): action for action in values}


def predict(step, state, action):
    state_arg = copy.deepcopy(state)
    action_arg = copy.deepcopy(action)
    state_before = copy.deepcopy(state_arg)
    action_before = copy.deepcopy(action_arg)
    predicted = step(state_arg, action_arg)
    if state_arg != state_before or action_arg != action_before:
        raise ValueError("step functions must not mutate their arguments")
    return predicted


reports = {}
for suffix, (step, _generate) in models.items():
    mismatches = []
    matches = 0
    for index, item in enumerate(timeline[1:], start=1):
        try:
            predicted = predict(step, item["state"], recorded_action(item["action"]))
            if predicted == item["next_state"]:
                matches += 1
            else:
                mismatches.append({"transition": index, "prediction": predicted, "actual": item["next_state"]})
        except (TypeError, ValueError, RuntimeError, KeyError, AttributeError) as exc:
            mismatches.append({"transition": index, "error": str(exc)})
    reports[suffix] = {"matches": matches, "mismatches": mismatches}

surviving = [suffix for suffix, report in reports.items() if not report["mismatches"]]
current = timeline[-1].get("next_state", timeline[-1])


def validate_plan(raw):
    plan = canonical_plan(raw)
    if len(plan["intents"]) > max_depth:
        raise ValueError(f"plan has {len(plan['intents'])} intents; limit is {max_depth}")
    unknown = sorted(set(plan["models"]) - set(models))
    if unknown:
        raise ValueError(f"plan references unknown models: {unknown}")
    contradicted = sorted(set(plan["models"]) - set(surviving))
    if contradicted:
        raise ValueError(f"plan references models contradicted by the Timeline: {contradicted}")
    states = {suffix: copy.deepcopy(current) for suffix in plan["models"]}
    for intent_index, intent in enumerate(plan["intents"], start=1):
        action = intent["action"]
        next_states = {}
        for suffix in plan["models"]:
            step, generate = models[suffix]
            if freeze(action) not in action_map(states[suffix], generate):
                raise ValueError(f"intent {intent_index} action is not legal under model {suffix!r}")
            predicted = predict(step, states[suffix], action)
            claimed = intent["prediction"][suffix]
            if predicted != claimed:
                raise ValueError(f"intent {intent_index} prediction for model {suffix!r} does not match step_{suffix}")
            next_states[suffix] = predicted
        states = next_states
    return plan

valid_plans = []
invalid_plans = []
for index, raw in enumerate(raw_plans, start=1):
    plan_id = f"plan-{index}"
    try:
        valid_plans.append({"plan_id": plan_id, "plan": validate_plan(raw)})
    except (TypeError, ValueError, RuntimeError, KeyError, AttributeError) as exc:
        invalid_plans.append({"plan_id": plan_id, "error": str(exc)})

result = {
    "backtest": {
        "models": reports,
        "surviving_models": surviving,
    },
    "planning": {
        "submitted_plans": len(raw_plans),
        "valid_plans": valid_plans,
        "invalid_plans": invalid_plans,
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
        'request["plans"] = json.loads(Path(request.pop("plans_path")).read_text())\n'
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


__all__ = [
    "evaluator_file_script",
    "evaluator_script",
    "parse_evaluator_output",
    "parse_evaluator_receipt",
]
