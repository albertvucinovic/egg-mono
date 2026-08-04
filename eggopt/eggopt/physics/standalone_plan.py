"""Plan against the executable hypotheses in ``world_model.py``.

This standard-library-only file is copied to an Actor repository as ``plan.py``.
It is starter code, not a protected runtime file: the Actor may inspect, edit,
replace, or delete it, and may create any other planning program it finds useful.
The trusted Critic does not trust this file; it independently validates the
committed ``world_model.py`` and ``plan.json`` before executing real actions.

MODEL CAPABILITIES
==================

Functions sharing a suffix belong to one model.  For a model named ``main``:

``step_main(state, action)``
    Required. Return the complete predicted next public state.
    Every Timeline-consistent step model can participate in pairwise distinction
    search; neither a reward nor a goal is required for that search.

``reward_main(state)``
    Optional. Return a finite score for partial progress. Reward search performs
    bounded breadth-first search and returns the shortest first trajectory to the
    highest reward found within the search bounds.

``goal_main(state)``
    Optional. Return ``True`` exactly at the model's final goal. A model with a
    goal can use A* search.

``heuristic_main(state)``
    Optional and meaningful only with ``goal_main``. Return a finite,
    nonnegative estimate of the number of actions remaining. Return zero at a
    goal. Without it A* uses ``h = 0`` and becomes uniform-cost/BFS goal search.
    Actor-authored heuristics are treated as fallible, so a heuristic-guided
    result is not reported as proven shortest.

``subgoal_main(state)``
    Optional diagnostic label. It explains phases of a found A* path but never
    excludes actions or states. Subgoals are soft guidance/explanation here, not
    mandatory ordered checkpoints.

SEARCH MODES
============

``auto`` (default)
    Try A* when a goal exists. If no goal trajectory is found within the bounds,
    fall back to reward BFS when a reward exists.

``astar``
    Search only explicit ``goal_*`` predicates, using matching ``heuristic_*``
    functions when present.

``reward``
    Run only bounded reward-maximizing BFS.

``all``
    Run both applicable searches so their suggestions can be compared.

In ``auto``, ``reward``, and ``all`` modes, the planner also uses bounded
breadth-first search to find a shortest trajectory whose prediction differs for
each pair of selected Timeline-consistent step models. This distinction search
uses only ``step_*`` functions, not ``reward_*`` or ``goal_*`` functions.

``--depth N`` and ``--max-nodes N`` are local Actor planning choices. The values
in ``physics-config.json`` are only defaults for a plain ``python plan.py`` run;
they are not ceilings. The enclosing Eggthreads tool call owns any wall-clock
timeout. This script deliberately adds no nested process or Physics-specific
timeout.

Reward and heuristic have different meanings: reward scores how desirable a
reached state is; a heuristic estimates remaining action cost. Reward is not
subtracted from A*'s ``g + h`` priority.

DEPTH CHOICE
============

``--depth N`` selects the local search horizon. It is a limit, not a requested
plan length. Start with the smallest plausible horizon and increase it when the
report says promising search was stopped by ``depth_limit``. If it says
``node_limit``, improve the model/heuristic, reduce branching, or deliberately
change ``--max-nodes`` instead of blindly increasing depth. You own both values
and may also edit this starter planner or replace it entirely.

EXAMPLES
========

    python plan.py --list
    python plan.py
    python plan.py --model main --search astar --depth 8
    python plan.py --model main --search reward --depth 4
    python plan.py --model main --search all --depth 12 --max-nodes 50000

The command validates the current ``plan.json``, writes ``plan-report.json``, and
prints the same report. Suggestions are advisory: copy or transform a useful
trajectory into ``plan.json``, rerun the checks, and commit a clean repository.
You may instead construct ``plan.json`` directly or use entirely different
planning code.
"""

import argparse
import copy
import hashlib
import heapq
import importlib.util
import itertools
import json
import math
import os
import subprocess
import tempfile
from collections import deque
from itertools import pairwise
from pathlib import Path
from typing import Any

EXPECTED_MODEL_ERRORS = (
    TypeError,
    ValueError,
    RuntimeError,
    KeyError,
    AttributeError,
)
SEARCH_MODES = ("auto", "astar", "reward", "all")


def freeze(value: Any) -> Any:
    """Return a hashable representation of a JSON-like public state."""

    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


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


def _pure_unary(function, state, *, name: str):
    argument = copy.deepcopy(state)
    before = copy.deepcopy(argument)
    result = function(argument)
    if argument != before:
        raise ValueError(f"{name} must not mutate its argument")
    return result


def predict(step, state, action):
    state_argument = copy.deepcopy(state)
    action_argument = copy.deepcopy(action)
    state_before = copy.deepcopy(state_argument)
    action_before = copy.deepcopy(action_argument)
    predicted = step(state_argument, action_argument)
    if state_argument != state_before or action_argument != action_before:
        raise ValueError("step functions must not mutate their arguments")
    return predicted


def finite_reward(reward, state) -> float:
    value = float(_pure_unary(reward, state, name="reward functions"))
    if not math.isfinite(value):
        raise ValueError("reward must be finite")
    return value


def goal_reached(goal, state) -> bool:
    value = _pure_unary(goal, state, name="goal functions")
    if not isinstance(value, bool):
        raise TypeError("goal must return a bool")
    return value


def remaining_cost(heuristic, state) -> float:
    if heuristic is None:
        return 0.0
    value = float(_pure_unary(heuristic, state, name="heuristic functions"))
    if not math.isfinite(value) or value < 0:
        raise ValueError("heuristic must be finite and nonnegative")
    return value


def _function_map(module, prefix: str) -> dict[str, Any]:
    offset = len(prefix)
    return {
        name[offset:]: value
        for name, value in vars(module).items()
        if name.startswith(prefix) and name[offset:] and callable(value)
    }


def load_world_model(source: str, work_dir: str | Path):
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    path = work / f"world_model_{digest}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(
        f"physics_world_model_{digest}", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("world model could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    steps = _function_map(module, "step_")
    rewards = _function_map(module, "reward_")
    goals = _function_map(module, "goal_")
    heuristics = _function_map(module, "heuristic_")
    subgoals = _function_map(module, "subgoal_")
    if not steps:
        raise ValueError("world_model.py defines no step_<model> functions")
    for name, functions in (
        ("reward", rewards),
        ("goal", goals),
        ("heuristic", heuristics),
        ("subgoal", subgoals),
    ):
        orphans = sorted(set(functions) - set(steps))
        if orphans:
            raise ValueError(f"{name} models require matching steps: {orphans}")
    orphan_heuristics = sorted(set(heuristics) - set(goals))
    if orphan_heuristics:
        raise ValueError(
            f"heuristic models require matching goals: {orphan_heuristics}"
        )
    return steps, rewards, goals, heuristics, subgoals


def backtest_models(steps, timeline) -> dict[str, dict[str, Any]]:
    reports = {}
    for model, step in sorted(steps.items()):
        mismatches = []
        matches = 0
        for index, item in enumerate(timeline[1:], start=1):
            try:
                predicted = predict(step, item["state"], item["action"])
                if predicted == item["next_state"]:
                    matches += 1
                else:
                    mismatches.append(
                        {
                            "transition": index,
                            "prediction": predicted,
                            "actual": item["next_state"],
                        }
                    )
            except EXPECTED_MODEL_ERRORS as exc:
                mismatches.append({"transition": index, "error": str(exc)})
        reports[model] = {"matches": matches, "mismatches": mismatches}
    return reports


def validate_plan(raw_plan, current, surviving, steps):
    plan = None
    error = None
    supporting = []
    predictions = []
    model_errors = []
    if raw_plan is not None:
        try:
            plan = canonical_plan(raw_plan)
            if plan[0]["state"] != current:
                raise ValueError(
                    "the first plan state must equal the canonical current state"
                )
            for item in plan:
                predicted = {}
                errors = {}
                for model in surviving:
                    try:
                        predicted[model] = predict(
                            steps[model], item["state"], item["action"]
                        )
                    except EXPECTED_MODEL_ERRORS as exc:
                        predicted[model] = None
                        errors[model] = str(exc)
                predictions.append(predicted)
                model_errors.append(errors)
            supporting = [
                model
                for model in surviving
                if all(
                    predicted[model] == item["next_state"]
                    for predicted, item in zip(predictions, plan)
                )
            ]
            if not supporting:
                raise ValueError(
                    "no Timeline-consistent step model reproduces the complete plan"
                )
        except EXPECTED_MODEL_ERRORS as exc:
            error = str(exc)
    return {
        "valid": plan is not None and error is None,
        "error": error,
        "supporting_models": supporting,
        "predictions": predictions,
        "model_errors": model_errors,
        "plan": plan,
    }


def _search_outcome(frontier, nodes: int, max_nodes: int, depth_limited: bool):
    if frontier and nodes >= max_nodes:
        return "node_limit"
    if depth_limited:
        return "depth_limit"
    return "frontier_exhausted"


def reward_search(model, step, reward, current, actions, search_depth, max_nodes):
    baseline = finite_reward(reward, current)
    best_reward = baseline
    best_trajectory = None
    frontier = deque([(current, ())])
    seen = {freeze(current)}
    nodes = 0
    deepest = 0
    depth_limited = False
    while frontier and nodes < max_nodes:
        state, trajectory = frontier.popleft()
        nodes += 1
        deepest = max(deepest, len(trajectory))
        if trajectory:
            value = finite_reward(reward, state)
            if value > best_reward:
                best_reward = value
                best_trajectory = trajectory
        if len(trajectory) >= search_depth:
            depth_limited = True
            continue
        for action in actions:
            next_state = predict(step, state, action)
            key = freeze(next_state)
            if key in seen:
                continue
            seen.add(key)
            transition = {
                "state": state,
                "action": action,
                "next_state": next_state,
            }
            frontier.append((next_state, trajectory + (transition,)))
    outcome = _search_outcome(frontier, nodes, max_nodes, depth_limited)
    diagnostic = {
        "kind": "reward",
        "model": model,
        "outcome": "improved_reward" if best_trajectory else outcome,
        "termination": outcome,
        "nodes_expanded": nodes,
        "deepest_expanded": deepest,
        "baseline_reward": baseline,
        "best_reward": best_reward,
    }
    suggestion = None
    if best_trajectory:
        suggestion = {
            "kind": "reward",
            "models": [model],
            "plan": list(best_trajectory),
            "baseline_reward": baseline,
            "reward": best_reward,
            "search": diagnostic,
        }
    return suggestion, diagnostic


def _subgoal_phases(function, current, trajectory):
    if function is None:
        return []
    states = [current, *(item["next_state"] for item in trajectory)]
    phases = []
    previous = object()
    for depth, state in enumerate(states):
        label = _pure_unary(function, state, name="subgoal functions")
        if label is not None and not isinstance(label, str):
            raise TypeError("subgoal must return a string or None")
        if label != previous:
            if label is not None:
                phases.append({"depth": depth, "label": label})
            previous = label
    return phases


def astar_search(
    model,
    step,
    goal,
    heuristic,
    subgoal,
    current,
    actions,
    search_depth,
    max_nodes,
):
    counter = itertools.count()
    initial_h = remaining_cost(heuristic, current)
    initial_goal = goal_reached(goal, current)
    if initial_goal and initial_h != 0:
        raise ValueError("heuristic must return zero at a goal")
    if initial_goal:
        return None, {
            "kind": "astar",
            "model": model,
            "outcome": "start_is_goal",
            "termination": "goal",
            "nodes_expanded": 0,
            "deepest_expanded": 0,
            "optimality": (
                "proven_shortest" if heuristic is None else "not_proven_actor_heuristic"
            ),
        }

    frontier = [(initial_h, 0, next(counter), current, ())]
    best_cost = {freeze(current): 0}
    nodes = 0
    deepest = 0
    depth_limited = False
    while frontier and nodes < max_nodes:
        _priority, cost, _order, state, trajectory = heapq.heappop(frontier)
        if best_cost.get(freeze(state)) != cost:
            continue
        nodes += 1
        deepest = max(deepest, cost)
        h = remaining_cost(heuristic, state)
        if goal_reached(goal, state):
            if h != 0:
                raise ValueError("heuristic must return zero at a goal")
            plan = list(trajectory)
            diagnostic = {
                "kind": "astar",
                "model": model,
                "outcome": "goal_found",
                "termination": "goal",
                "nodes_expanded": nodes,
                "deepest_expanded": deepest,
                "path_cost": cost,
                "optimality": (
                    "proven_shortest"
                    if heuristic is None
                    else "not_proven_actor_heuristic"
                ),
            }
            suggestion = {
                "kind": "goal",
                "models": [model],
                "plan": plan,
                "search": diagnostic,
            }
            try:
                suggestion["subgoals"] = _subgoal_phases(
                    subgoal, current, plan
                )
            except EXPECTED_MODEL_ERRORS as exc:
                suggestion["subgoal_error"] = str(exc)
            return suggestion, diagnostic
        if cost >= search_depth:
            depth_limited = True
            continue
        for action in actions:
            next_state = predict(step, state, action)
            next_cost = cost + 1
            key = freeze(next_state)
            if next_cost >= best_cost.get(key, math.inf):
                continue
            next_h = remaining_cost(heuristic, next_state)
            if goal_reached(goal, next_state) and next_h != 0:
                raise ValueError("heuristic must return zero at a goal")
            best_cost[key] = next_cost
            transition = {
                "state": state,
                "action": action,
                "next_state": next_state,
            }
            next_trajectory = trajectory + (transition,)
            heapq.heappush(
                frontier,
                (
                    next_cost + next_h,
                    next_cost,
                    next(counter),
                    next_state,
                    next_trajectory,
                ),
            )
    outcome = _search_outcome(frontier, nodes, max_nodes, depth_limited)
    return None, {
        "kind": "astar",
        "model": model,
        "outcome": outcome,
        "termination": outcome,
        "nodes_expanded": nodes,
        "deepest_expanded": deepest,
        "optimality": (
            "proven_shortest" if heuristic is None else "not_proven_actor_heuristic"
        ),
    }


def distinction_search(left, right, steps, current, actions, search_depth, max_nodes):
    frontier = deque([(current, current, ())])
    seen = {(freeze(current), freeze(current))}
    nodes = 0
    while frontier and nodes < max_nodes:
        left_state, right_state, trajectory = frontier.popleft()
        nodes += 1
        if len(trajectory) >= search_depth:
            continue
        for action in actions:
            left_next = predict(steps[left], left_state, action)
            right_next = predict(steps[right], right_state, action)
            transition = {
                "state": left_state,
                "action": action,
                "next_state": left_next,
            }
            next_trajectory = trajectory + (transition,)
            if left_next != right_next:
                return list(next_trajectory)
            key = (freeze(left_next), freeze(right_next))
            if key not in seen:
                seen.add(key)
                frontier.append((left_next, right_next, next_trajectory))
    return None


def _positive_integer(value, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def evaluate_request(request: dict[str, Any]) -> dict[str, Any]:
    source = request["source"]
    timeline = request["timeline"]
    raw_plan = request.get("plan")
    if not isinstance(timeline, list) or not timeline:
        raise ValueError("timeline must be a non-empty list")
    mode = str(request.get("search") or "none")
    if mode not in (*SEARCH_MODES, "none"):
        raise ValueError(f"unknown search mode: {mode}")
    actions = request.get("planner_actions", [])
    if mode != "none" and not isinstance(actions, list):
        raise TypeError("planner_actions must be a finite list")
    requested_model = request.get("model")
    if requested_model is not None and (
        not isinstance(requested_model, str) or not requested_model
    ):
        raise ValueError("model must be a non-empty string")

    steps, rewards, goals, heuristics, subgoals = load_world_model(
        source, request.get("work_dir", ".physics-evaluation")
    )
    if requested_model is not None and requested_model not in steps:
        raise ValueError(f"unknown model: {requested_model}")
    reports = backtest_models(steps, timeline)
    surviving = [
        model for model, report in reports.items() if not report["mismatches"]
    ]
    current = timeline[-1].get("next_state", timeline[-1])
    validation = validate_plan(raw_plan, current, surviving, steps)
    capabilities = {
        model: {
            "step": True,
            "reward": model in rewards,
            "goal": model in goals,
            "heuristic": model in heuristics,
            "subgoal": model in subgoals,
            "timeline_consistent": model in surviving,
        }
        for model in sorted(steps)
    }
    selected = [
        model
        for model in surviving
        if requested_model is None or model == requested_model
    ]
    suggestions = []
    searches = []
    search_depth = (
        _positive_integer(request["search_depth"], name="search_depth")
        if mode != "none"
        else None
    )
    max_nodes = (
        _positive_integer(request["max_nodes"], name="max_nodes")
        if mode != "none"
        else None
    )
    if mode != "none" and actions:
        for model in selected:
            goal_suggestion = None
            goal_diagnostic = None
            if mode in {"auto", "astar", "all"} and model in goals:
                try:
                    goal_suggestion, goal_diagnostic = astar_search(
                        model,
                        steps[model],
                        goals[model],
                        heuristics.get(model),
                        subgoals.get(model),
                        current,
                        actions,
                        search_depth,
                        max_nodes,
                    )
                    searches.append(goal_diagnostic)
                    if goal_suggestion:
                        suggestions.append(goal_suggestion)
                except EXPECTED_MODEL_ERRORS as exc:
                    goal_diagnostic = {
                        "kind": "astar",
                        "model": model,
                        "outcome": "error",
                        "error": str(exc),
                    }
                    searches.append(goal_diagnostic)
            use_reward = mode in {"reward", "all"} or (
                mode == "auto"
                and model in rewards
                and goal_suggestion is None
                and (goal_diagnostic or {}).get("outcome") != "start_is_goal"
            )
            if use_reward and model in rewards:
                try:
                    suggestion, diagnostic = reward_search(
                        model,
                        steps[model],
                        rewards[model],
                        current,
                        actions,
                        search_depth,
                        max_nodes,
                    )
                    searches.append(diagnostic)
                    if suggestion:
                        suggestions.append(suggestion)
                except EXPECTED_MODEL_ERRORS as exc:
                    searches.append(
                        {
                            "kind": "reward",
                            "model": model,
                            "outcome": "error",
                            "error": str(exc),
                        }
                    )
        if mode in {"auto", "reward", "all"}:
            for left, right in itertools.combinations(selected, 2):
                try:
                    plan = distinction_search(
                        left,
                        right,
                        steps,
                        current,
                        actions,
                        search_depth,
                        max_nodes,
                    )
                    if plan:
                        suggestions.append(
                            {
                                "kind": "distinction",
                                "models": [left, right],
                                "plan": plan,
                            }
                        )
                except EXPECTED_MODEL_ERRORS as exc:
                    searches.append(
                        {
                            "kind": "distinction",
                            "models": [left, right],
                            "outcome": "error",
                            "error": str(exc),
                        }
                    )

    return {
        "backtest": {"models": reports, "surviving_models": surviving},
        "plan_validation": validation,
        "planning": {
            "capabilities": capabilities,
            "eligible_models": selected,
            "search_mode": mode,
            "search_depth": search_depth,
            "max_nodes": max_nodes,
            "searches": searches,
            "suggestions": suggestions,
        },
    }


def _atomic_json(path: str | Path, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix="." + output.name + ".", dir=output.parent
    )
    try:
        with os.fdopen(handle, "w") as stream:
            handle = -1
            json.dump(value, stream, indent=2, sort_keys=True)
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


def emit_evaluator_result(request: dict[str, Any]) -> None:
    result = evaluate_request(request)
    output_path = request.get("output_path")
    if output_path:
        _atomic_json(output_path, result)
        print(
            "__EGG_PHYSICS_REPORT__"
            + json.dumps(
                {"path": str(output_path)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            "__EGG_PHYSICS_RESULT__"
            + json.dumps(result, separators=(",", ":"), sort_keys=True)
        )


def _configuration() -> dict[str, Any]:
    value = json.loads(Path("physics-config.json").read_text())
    required = ("planner_actions", "default_search_depth", "default_max_nodes")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise ValueError("physics-config.json is missing required configuration")
    return value


def workspace_request(
    *,
    include_plan: bool,
    search: str,
    model: str | None = None,
    depth: int | None = None,
    max_nodes: int | None = None,
) -> dict[str, Any]:
    config = _configuration()
    search_depth = (
        _positive_integer(config["default_search_depth"], name="default_search_depth")
        if depth is None
        else _positive_integer(depth, name="search_depth")
    )
    return {
        "source": Path("world_model.py").read_text(),
        "timeline": json.loads(Path("canonical-input.json").read_text())["timeline"],
        "plan": json.loads(Path("plan.json").read_text()) if include_plan else None,
        "planner_actions": config["planner_actions"],
        "search_depth": search_depth,
        "max_nodes": _positive_integer(
            config["default_max_nodes"] if max_nodes is None else max_nodes,
            name="max_nodes",
        ),
        "search": search,
        "model": model,
        "work_dir": ".physics-evaluation",
    }


def run_backtest() -> dict[str, Any]:
    document = evaluate_request(
        workspace_request(include_plan=False, search="none")
    )
    report = document["backtest"]
    _atomic_json("backtest-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate plan.json and search executable world models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Open plan.py and read its module docstring for the complete guide.",
    )
    parser.add_argument("--search", choices=SEARCH_MODES, default="auto")
    parser.add_argument("--model", help="Search only one model suffix")
    parser.add_argument("--depth", type=int, help="Local search depth")
    parser.add_argument("--max-nodes", type=int, help="Node budget per search")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List detected model capabilities without running search",
    )
    return parser


def run_plan(argv: list[str] | None = None) -> dict[str, Any]:
    arguments = _parser().parse_args(argv)
    for name in ("depth", "max_nodes"):
        value = getattr(arguments, name)
        if value is not None and value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    request = workspace_request(
        include_plan=True,
        search="none" if arguments.list else arguments.search,
        model=arguments.model,
        depth=arguments.depth,
        max_nodes=arguments.max_nodes,
    )
    document = evaluate_request(request)
    if arguments.list:
        print(json.dumps(document["planning"]["capabilities"], indent=2, sort_keys=True))
        return document
    report = {
        "validation": document["plan_validation"],
        "planning": document["planning"],
    }
    _atomic_json("plan-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return report


def commit_plan(message: str = "Actor submits trajectory") -> None:
    report = run_plan([])
    if not report["validation"]["valid"]:
        raise SystemExit("plan.json is invalid; inspect plan-report.json")
    if not report["validation"]["supporting_models"]:
        raise SystemExit("plan.json has no supporting model")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", message], check=True)


if __name__ == "__main__":
    trusted_request = globals().get("_EGG_PHYSICS_REQUEST")
    if trusted_request is not None:
        emit_evaluator_result(trusted_request)
    else:
        run_plan()