from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COMMITTED_PLAN = "committed-plan.json"
PROPOSED_PLANS = "proposed-plans.json"


def canonical_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"purpose", "models", "intents"}:
        raise ValueError("plan must contain exactly purpose, models, and intents")
    if value["purpose"] not in {"goal", "experiment"}:
        raise ValueError("plan purpose must be goal or experiment")
    models = value["models"]
    intents = value["intents"]
    if (
        not isinstance(models, list)
        or not models
        or not all(isinstance(item, str) and item for item in models)
    ):
        raise ValueError("plan models must be a non-empty string list")
    if len(set(models)) != len(models):
        raise ValueError("plan models must be unique")
    if value["purpose"] == "goal" and len(models) != 1:
        raise ValueError("goal plans must use exactly one model")
    if value["purpose"] == "experiment" and len(models) < 2:
        raise ValueError("experiment plans must use at least two models")
    if not isinstance(intents, list) or not intents:
        raise ValueError("committed plan must contain at least one intent")
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != {"action", "prediction"}:
            raise ValueError("every intent must contain exactly action and prediction")
        predictions = intent["prediction"]
        if not isinstance(predictions, dict) or set(predictions) != set(models):
            raise ValueError("every intent must predict once for every plan model")
    if value["purpose"] == "experiment":
        for index, intent in enumerate(intents):
            distinct = len({freeze(item) for item in intent["prediction"].values()})
            if index < len(intents) - 1 and distinct != 1:
                raise ValueError(
                    "experiment predictions must share one common prefix"
                )
            if index == len(intents) - 1 and distinct < 2:
                raise ValueError(
                    "an experiment must end with its first distinguishing action"
                )
    return {"purpose": value["purpose"], "models": models, "intents": intents}


def load_committed_plan(workspace: str | Path) -> dict[str, Any]:
    path = Path(workspace) / COMMITTED_PLAN
    if not path.is_file():
        raise ValueError(f"{COMMITTED_PLAN} is missing")
    try:
        return canonical_plan(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{COMMITTED_PLAN} is not valid JSON") from exc


def load_proposed_plans(workspace: str | Path) -> list[Any]:
    path = Path(workspace) / PROPOSED_PLANS
    if not path.is_file():
        raise ValueError(f"{PROPOSED_PLANS} is missing")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{PROPOSED_PLANS} is not valid JSON") from exc
    if not isinstance(value, list):
        raise TypeError(f"{PROPOSED_PLANS} must contain a finite JSON list")
    return value


def selected_plan_id(proposals: list[Any], committed: Any) -> str:
    matches = [
        index
        for index, proposal in enumerate(proposals, start=1)
        if proposal == committed
    ]
    if not matches:
        raise ValueError(
            "committed-plan.json is not present in proposed-plans.json"
        )
    if len(matches) != 1:
        raise ValueError("the committed plan appears more than once in proposed-plans.json")
    return f"plan-{matches[0]}"


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


__all__ = [
    "COMMITTED_PLAN",
    "PROPOSED_PLANS",
    "canonical_plan",
    "freeze",
    "load_committed_plan",
    "load_proposed_plans",
    "selected_plan_id",
]
