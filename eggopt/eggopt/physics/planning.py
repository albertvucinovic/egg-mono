from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

PLAN = "plan.json"


def canonical_plan(value: Any) -> list[dict[str, Any]]:
    """Return one canonical non-empty ``state, action, next_state`` trajectory."""

    if not isinstance(value, list) or not value:
        raise ValueError("plan must be a non-empty JSON list")
    plan = []
    for transition in value:
        if not isinstance(transition, dict) or set(transition) != {
            "state",
            "action",
            "next_state",
        }:
            raise ValueError(
                "every plan transition must contain exactly state, action, and next_state"
            )
        plan.append(
            {
                "state": transition["state"],
                "action": transition["action"],
                "next_state": transition["next_state"],
            }
        )
    for previous, current in pairwise(plan):
        if previous["next_state"] != current["state"]:
            raise ValueError("plan transitions must form one continuous trajectory")
    return plan


def load_plan(workspace: str | Path) -> list[dict[str, Any]]:
    path = Path(workspace) / PLAN
    if not path.is_file():
        raise ValueError(f"{PLAN} is missing")
    try:
        return canonical_plan(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{PLAN} is not valid JSON") from exc


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


__all__ = ["PLAN", "canonical_plan", "freeze", "load_plan"]
