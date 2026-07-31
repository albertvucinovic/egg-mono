from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task

from ..actor_critic import Critique
from ..identity import digest_payload
from ..thread_tool import ThreadTool
from .instruments import write_actor_files
from .planning import canonical_plan, freeze, load_committed_plan
from .theory import evaluator_script, parse_evaluator_output


@dataclass
class PhysicsCritic(Task):
    """Trusted generic Physics pipeline executed in the assigned Critic Eggthread."""

    tools: Any = field(repr=False, compare=False)
    execute: Any = field(repr=False, compare=False)
    is_goal: Any = field(repr=False, compare=False)
    identity: Any
    domain_information: str = ""
    legal_actions_key: str = "legal_actions"
    max_depth: int = 8
    max_nodes: int = 10_000
    workspace: str | None = None
    outer_context: str | None = None
    head: str | None = None
    critic_thread_id: str | None = None
    max_actions: int = 100

    def get_cache_key(self):
        return digest_payload(
            "eggopt.physics.domain-critic.v1",
            {
                "identity": self.identity,
                "head": self.head,
                "max_actions": self.max_actions,
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
                "legal_actions_key": self.legal_actions_key,
            },
        )

    def run(self):
        if self.workspace is None or self.critic_thread_id is None:
            raise RuntimeError(
                "PhysicsCritic requires its assigned repository and thread"
            )
        repository = Path(self.workspace)
        state_root = Path(self.outer_context) if self.outer_context else repository
        state = read_state(state_root)
        timeline = tuple(state["timeline"])
        actions = int(state["actions"])
        source_path = repository / "world_model.py"
        if not source_path.is_file():
            return self._revise(
                repository, state_root, timeline, actions, "world_model.py is missing"
            )

        request = {
            "source": source_path.read_text(),
            "timeline": timeline,
            "legal_actions_key": self.legal_actions_key,
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
        }
        try:
            output = yield ThreadTool(
                self.tools,
                self.critic_thread_id,
                "python_exec",
                {"script": evaluator_script(request)},
                origin="eggopt.physics.trusted-evaluator",
            )
            evaluation = parse_evaluator_output(output)
            committed = load_committed_plan(repository)
            committed = canonical_plan(committed)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return self._revise(repository, state_root, timeline, actions, str(exc))

        backtest = evaluation["backtest"]
        planning = evaluation["planning"]
        plans = [canonical_plan(plan) for plan in planning["plans"]]
        if committed not in plans:
            return self._revise(
                repository,
                state_root,
                timeline,
                actions,
                "committed-plan.json is not one of the trusted planner results",
                evaluation,
            )
        if not set(committed["models"]) <= set(backtest["surviving_models"]):
            return self._revise(
                repository,
                state_root,
                timeline,
                actions,
                "The plan references a model that already contradicts the Timeline. "
                "The all-model planning report remains available for theory repair.",
                evaluation,
            )

        current = timeline[-1].get("next_state", timeline[-1])
        legal = set(current.get(self.legal_actions_key, ()))
        if committed["intents"][0]["action"] not in legal:
            return self._revise(
                repository,
                state_root,
                timeline,
                actions,
                "The first committed action is not legal in the canonical current state",
                evaluation,
            )

        executed = []
        compatible = set(committed["models"])
        resolution = "plan_exhausted"
        for intent in committed["intents"]:
            if actions >= self.max_actions:
                resolution = "max_actions"
                break
            effect = self.execute(
                timeline=timeline,
                intent=intent,
                workspace=str(repository),
            )
            if not isinstance(effect, Task):
                raise TypeError("Physics execute must construct an Eggflow Task")
            next_state = yield effect
            transition = {
                "state": timeline[-1].get("next_state", timeline[-1]),
                "action": intent,
                "next_state": next_state,
            }
            timeline += (transition,)
            executed.append(transition)
            actions += 1
            predictions = intent["prediction"]
            matching = {name for name in compatible if predictions[name] == next_state}
            branched = len({freeze(value) for value in predictions.values()}) > 1
            if not matching:
                compatible.clear()
                resolution = "wrong_prediction"
                break
            compatible = matching
            if branched:
                resolution = "models_discriminated"
                break
            if self.is_goal(next_state):
                resolution = "won"
                break

        report = {
            "stage": "execution",
            "head": self.head,
            **evaluation,
            "committed_plan": committed,
            "executed": executed,
            "resolution": resolution,
            "compatible_models": sorted(compatible),
            "actions": actions,
        }
        sync_state(
            repository, state_root, timeline, actions, report, self.domain_information
        )
        if resolution in {"won", "max_actions"}:
            return Critique.accept(
                {
                    "stopping_reason": resolution,
                    "timeline": timeline,
                    "actions": actions,
                    "report": report,
                },
                "Goal reached." if resolution == "won" else "Action budget exhausted.",
            )
        return Critique.revise(
            "The trusted plan executed until resolution. Read trusted-report.json and "
            "canonical-input.json, revise the theory, and commit another trusted plan. "
            f"Resolution: {resolution}."
        )

    def _revise(
        self, repository, state_root, timeline, actions, error, evaluation=None
    ):
        report = {
            "stage": "validation",
            "head": self.head,
            "error": error,
            "evaluation": evaluation,
        }
        sync_state(
            repository, state_root, timeline, actions, report, self.domain_information
        )
        return Critique.revise(
            f"Trusted Physics validation failed: {error}. Read trusted-report.json, "
            "fix the current theory or plan, make a clean Git commit, and answer again."
        )


def read_state(repository: Path) -> dict[str, Any]:
    path = repository / ".trusted" / "state.json"
    if not path.is_file():
        raise RuntimeError("trusted canonical state is missing")
    return json.loads(path.read_text())


def write_state(repository: Path, timeline, actions, report) -> None:
    trusted = repository / ".trusted"
    trusted.mkdir(parents=True, exist_ok=True)
    _write_json(
        trusted / "state.json",
        {"timeline": timeline, "actions": actions, "last_report": report},
    )
    _write_json(repository / "canonical-input.json", {"timeline": timeline})
    if report is not None:
        _write_json(repository / "trusted-report.json", report)


def sync_state(repository, state_root, timeline, actions, report, domain_information):
    write_state(repository, timeline, actions, report)
    if state_root != repository:
        write_state(state_root, timeline, actions, report)
    write_actor_files(repository, timeline, domain_information)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=repr) + "\n")


__all__ = ["PhysicsCritic", "read_state", "sync_state", "write_state"]
