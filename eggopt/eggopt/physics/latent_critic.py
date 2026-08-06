from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, TaskError

from ..actor_critic import Critique
from ..identity import digest_payload
from ..thread_tool import ThreadTool, ThreadToolResult
from .critic import read_state, sync_state
from .latent_theory import evaluator_file_script, parse_evaluator_receipt
from .lifecycle import classify_terminal_state, terminal_feedback
from .modes import LATENT, PhysicsMode


@dataclass
class LatentPhysicsCritic(Task):
    """Trusted latent-state Critic, optionally verifying complete public states."""

    tools: Any = field(repr=False, compare=False)
    execute: Any = field(repr=False, compare=False)
    validate_action: Any = field(repr=False, compare=False)
    is_goal: Any = field(repr=False, compare=False)
    identity: Any
    mode: PhysicsMode = LATENT
    terminal_outcome: Any = field(default=None, repr=False, compare=False)
    evaluator_timeout_sec: float = 300.0
    workspace: str | None = None
    outer_context: str | None = None
    head: str | None = None
    critic_thread_id: str | None = None
    max_actions: int = 100

    def get_cache_key(self):
        return digest_payload(
            "eggopt.physics.latent-critic.v1",
            {
                "identity": self.identity,
                "mode": self.mode.name,
                "head": self.head,
                "max_actions": self.max_actions,
                "evaluator_timeout_sec": self.evaluator_timeout_sec,
            },
        )

    def run(self):
        if not self.mode.latent:
            raise ValueError("LatentPhysicsCritic requires latent=True")
        if self.workspace is None or self.critic_thread_id is None:
            raise RuntimeError(
                "LatentPhysicsCritic requires its assigned repository and thread"
            )
        repository = Path(self.workspace)
        state_root = Path(self.outer_context) if self.outer_context else repository
        state = read_state(state_root)
        timeline = tuple(state["timeline"])
        actions = int(state["actions"])
        current = timeline[-1].get("next_state", timeline[-1])
        terminal = self._terminal_outcome(current)
        if terminal is not None:
            return self._accept_terminal(
                repository, state_root, timeline, actions, terminal, executed=[]
            )
        if not (repository / "world_model.py").is_file():
            return self._revise(
                repository,
                state_root,
                timeline,
                actions,
                "world_model.py is missing from the submitted Git HEAD",
            )

        try:
            plan = _load_latent_plan(repository)
            evaluation = yield from self._evaluate_proposal(repository)
            if not evaluation.get("valid"):
                raise ValueError(evaluation.get("error") or "latent plan is invalid")
            if evaluation.get("actions") != plan["actions"]:
                raise ValueError("trusted latent evaluation changed submitted actions")
            validation_states = (
                evaluation["public_states"][:-1]
                if self.mode.verified
                else (current,)
            )
            for state_for_action, action in zip(
                validation_states, plan["actions"], strict=False
            ):
                validated = self.validate_action(
                    state=state_for_action, action=action
                )
                if validated is not None:
                    raise TypeError("validate_action must return None or raise")
        except (OSError, TaskError, TypeError, ValueError, RuntimeError) as exc:
            return self._revise(repository, state_root, timeline, actions, str(exc))

        executed = []
        resolution = "plan_exhausted"
        model = evaluation["model"]
        latent_states = evaluation["latent_states"]
        public_states = evaluation["public_states"]
        for index, action in enumerate(plan["actions"]):
            if actions >= self.max_actions:
                resolution = "max_actions"
                break
            current = timeline[-1].get("next_state", timeline[-1])
            validated = self.validate_action(state=current, action=action)
            if validated is not None:
                raise TypeError("validate_action must return None or raise")
            effect = self.execute(
                timeline=timeline,
                action=action,
                workspace=str(repository),
            )
            if not isinstance(effect, Task):
                raise TypeError("Physics execute must construct an Eggflow Task")
            try:
                next_state = yield effect
            except TaskError as exc:
                if exc.is_terminal:
                    raise
                return self._revise(
                    repository,
                    state_root,
                    timeline,
                    actions,
                    "The domain rejected the submitted action before producing an "
                    f"observation: {exc}",
                )

            actual = {"state": current, "action": action, "next_state": next_state}
            timeline += (actual,)
            executed.append(actual)
            actions += 1
            outcome = self._terminal_outcome(next_state)
            if outcome is not None:
                resolution = outcome
                break

            if self.mode.verified:
                if next_state != public_states[index + 1]:
                    resolution = "wrong_prediction"
                    break
            else:
                observed = yield from self._evaluate_observation(
                    repository,
                    model=model,
                    timeline=timeline,
                    expected_latent=latent_states[index + 1],
                    expected_public=None,
                    index=index,
                )
                if not observed.get("valid") or not observed.get("latent_matches"):
                    resolution = "wrong_prediction"
                    break

        report = {
            "stage": "execution",
            "head": self.head,
            "mode": self.mode.name,
            "evaluation": evaluation,
            "plan": plan,
            "executed": executed,
            "resolution": resolution,
            "actions": actions,
        }
        sync_state(repository, state_root, timeline, actions, report)
        if resolution in {"won", "max_actions"} or self._terminal_outcome(
            timeline[-1].get("next_state", timeline[-1])
        ) is not None:
            return Critique.accept(
                {
                    "stopping_reason": resolution,
                    "timeline": timeline,
                    "actions": actions,
                    "report": report,
                },
                _completion_feedback(resolution),
            )
        return Critique.revise(
            "Trusted latent execution stopped with "
            f"resolution={resolution}. Inspect trusted-report.json and revise the "
            "committed latent model and plan."
        )

    def _evaluate_proposal(self, repository):
        timeline = read_state(
            Path(self.outer_context) if self.outer_context else repository
        )["timeline"]
        request = {
            "kind": "proposal",
            "verified": self.mode.verified,
            "source_path": "world_model.py",
            "timeline_path": ".trusted/latent-inputs/proposal-timeline.json",
            "plan_path": "plan.json",
            "work_dir": ".trusted/latent-work",
            "output_path": _report_path(self.head, "proposal"),
        }
        _write_json(repository / request["timeline_path"], timeline)
        return (yield from self._evaluate(repository, request))

    def _evaluate_observation(
        self,
        repository,
        *,
        model,
        timeline,
        expected_latent,
        expected_public,
        index,
    ):
        request = {
            "kind": "observation",
            "verified": self.mode.verified,
            "source_path": "world_model.py",
            "timeline_path": (
                f".trusted/latent-inputs/observation-{index + 1}-timeline.json"
            ),
            "model": model,
            "expected_latent": expected_latent,
            "expected_public": expected_public,
            "work_dir": ".trusted/latent-work",
            "output_path": _report_path(self.head, f"observation-{index + 1}"),
        }
        _write_json(repository / request["timeline_path"], timeline)
        return (yield from self._evaluate(repository, request))

    def _evaluate(self, repository, request):
        request_path = _request_path(self.head, request["kind"], request["output_path"])
        _write_json(repository / request_path, request)
        result = yield ThreadTool(
            self.tools,
            self.critic_thread_id,
            "python_exec",
            {
                "script": evaluator_file_script(request_path),
                "timeout": self.evaluator_timeout_sec,
            },
            origin="eggopt.physics.latent-trusted-evaluator",
            input_files=tuple(
                item
                for item in (
                    request_path,
                    "world_model.py",
                    request["timeline_path"],
                    request.get("plan_path"),
                )
                if item
            ),
            output_files=(request["output_path"],),
        )
        if not isinstance(result, ThreadToolResult):
            raise TypeError("trusted latent evaluator returned no durable file result")
        if parse_evaluator_receipt(result.output) != request["output_path"]:
            raise ValueError("trusted latent evaluator receipt named an unexpected report")
        return json.loads((repository / request["output_path"]).read_text())

    def _terminal_outcome(self, state) -> str | None:
        return classify_terminal_state(
            state,
            is_goal=self.is_goal,
            terminal_outcome=self.terminal_outcome,
        )

    def _accept_terminal(
        self, repository, state_root, timeline, actions, resolution, *, executed
    ):
        report = {
            "stage": "execution",
            "head": self.head,
            "mode": self.mode.name,
            "plan": None,
            "executed": executed,
            "resolution": resolution,
            "actions": actions,
        }
        sync_state(repository, state_root, timeline, actions, report)
        return Critique.accept(
            {
                "stopping_reason": resolution,
                "timeline": timeline,
                "actions": actions,
                "report": report,
            },
            terminal_feedback(resolution),
        )

    def _revise(self, repository, state_root, timeline, actions, error):
        report = {
            "stage": "validation",
            "head": self.head,
            "mode": self.mode.name,
            "error": error,
        }
        sync_state(repository, state_root, timeline, actions, report)
        return Critique.revise(
            "The trusted latent Critic rejected the submitted Git HEAD before "
            f"executing a real action. Reason: {error}. Read trusted-report.json, "
            "correct world_model.py or plan.json, and submit a new clean commit."
        )


def _load_latent_plan(repository: Path) -> dict[str, Any]:
    value = json.loads((repository / "plan.json").read_text())
    if not isinstance(value, dict) or set(value) != {"model", "actions"}:
        raise ValueError("latent plan must contain exactly model and actions")
    if not isinstance(value["model"], str) or not value["model"]:
        raise ValueError("latent plan model must be non-empty")
    if not isinstance(value["actions"], list) or not value["actions"]:
        raise ValueError("latent plan actions must be a non-empty list")
    return value


def _report_path(head: str | None, stage: str) -> str:
    value = str(head or "").strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("latent Physics Critic requires a full Git HEAD")
    return f".trusted/latent-evaluations/{value}-{stage}.json"


def _request_path(head: str | None, kind: str, output_path: str) -> str:
    digest = digest_payload(
        "eggopt.physics.latent-request.v1",
        {"head": head, "kind": kind, "output": output_path},
    )
    return f".trusted/latent-requests/{digest}.json"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=repr) + "\n")


def _completion_feedback(resolution: str) -> str:
    if resolution == "max_actions":
        return "The trusted real-action budget is exhausted."
    return terminal_feedback(resolution)


__all__ = ["LatentPhysicsCritic"]