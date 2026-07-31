from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COMMITTED_PLAN = "committed-plan.json"


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
    if not isinstance(intents, list) or not intents:
        raise ValueError("committed plan must contain at least one intent")
    for intent in intents:
        if not isinstance(intent, dict) or "action" not in intent:
            raise ValueError("every intent must contain an action")
        predictions = intent.get("prediction")
        if not isinstance(predictions, dict) or set(predictions) != set(models):
            raise ValueError("every intent must predict once for every plan model")
    return {"purpose": value["purpose"], "models": models, "intents": intents}


def load_committed_plan(workspace: str | Path) -> dict[str, Any]:
    path = Path(workspace) / COMMITTED_PLAN
    if not path.is_file():
        raise ValueError(f"{COMMITTED_PLAN} is missing")
    try:
        return canonical_plan(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{COMMITTED_PLAN} is not valid JSON") from exc


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


__all__ = ["COMMITTED_PLAN", "canonical_plan", "freeze", "load_committed_plan"]
