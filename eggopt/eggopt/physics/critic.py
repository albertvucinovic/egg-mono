from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, TaskError

from ..actor_critic import Critique
from ..identity import digest_payload
from ..thread_tool import ThreadTool, ThreadToolResult
from .lifecycle import classify_terminal_state, terminal_feedback
from .planning import load_plan
from .theory import evaluator_file_script, parse_evaluator_receipt


@dataclass
class PhysicsCritic(Task):
    """Trusted generic Physics pipeline executed in the assigned Critic Eggthread."""

    tools: Any = field(repr=False, compare=False)
    execute: Any = field(repr=False, compare=False)
    validate_action: Any = field(repr=False, compare=False)
    is_goal: Any = field(repr=False, compare=False)
    identity: Any
    terminal_outcome: Any = field(default=None, repr=False, compare=False)
    evaluator_timeout_sec: float = 300.0
    workspace: str | None = None
    outer_context: str | None = None
    head: str | None = None
    critic_thread_id: str | None = None
    max_actions: int = 100

    def get_cache_key(self):
        return digest_payload(
            "eggopt.physics.domain-critic.v4.verify-only",
            {
                "identity": self.identity,
                "head": self.head,
                "max_actions": self.max_actions,
                "evaluator_timeout_sec": self.evaluator_timeout_sec,
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
        current = timeline[-1].get("next_state", timeline[-1])
        terminal = self._terminal_outcome(current)
        if terminal is not None:
            return self._accept_terminal(
                repository,
                state_root,
                timeline,
                actions,
                terminal,
                executed=[],
                evaluation=None,
                plan=None,
                supporting_models=[],
                matching_models=[],
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
            plan = load_plan(repository)
            if plan[0]["state"] != current:
                raise ValueError(
                    "the first plan state must equal the authoritative current state"
                )
            for transition in plan:
                validated = self.validate_action(
                    state=transition["state"], action=transition["action"]
                )
                if validated is not None:
                    raise TypeError("validate_action must return None or raise")
            evaluation = yield from self._evaluate(repository)
        except (OSError, TaskError, TypeError, ValueError, RuntimeError) as exc:
            return self._revise(repository, state_root, timeline, actions, str(exc))

        validation = evaluation["plan_validation"]
        if not validation["valid"] or validation["plan"] != plan:
            return self._revise(
                repository,
                state_root,
                timeline,
                actions,
                "plan.json failed independent trajectory validation: "
                + str(validation.get("error") or "validated plan differed"),
                evaluation,
            )

        executed = []
        resolution = "plan_exhausted"
        outcome = None
        surviving_models = set(evaluation["backtest"]["surviving_models"])
        matching_models = set(surviving_models)
        predictions = validation["predictions"]
        for index, transition in enumerate(plan):
            if actions >= self.max_actions:
                resolution = "max_actions"
                break
            current = timeline[-1].get("next_state", timeline[-1])
            if transition["state"] != current:
                raise RuntimeError(
                    "submitted plan no longer chains from authoritative reality"
                )
            validated = self.validate_action(
                state=current, action=transition["action"]
            )
            if validated is not None:
                raise TypeError("validate_action must return None or raise")
            effect = self.execute(
                timeline=timeline,
                action=transition["action"],
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
                    f"observation: {exc}. No transition was appended and the real-action "
                    "budget was not incremented. Correct plan.json and rerun its checks.",
                    evaluation,
                )
            actual = {
                "state": timeline[-1].get("next_state", timeline[-1]),
                "action": transition["action"],
                "next_state": next_state,
            }
            timeline += (actual,)
            executed.append(actual)
            actions += 1
            matching_models = {
                suffix
                for suffix in surviving_models
                if predictions[index].get(suffix) == next_state
            }
            outcome = self._terminal_outcome(next_state)
            if outcome is not None:
                resolution = outcome
                break
            if next_state != transition["next_state"]:
                resolution = "wrong_prediction"
                break

        report = {
            "stage": "execution",
            "head": self.head,
            **evaluation,
            "plan": plan,
            "supporting_models": validation["supporting_models"],
            "executed": executed,
            "resolution": resolution,
            "matching_models": sorted(matching_models),
            "actions": actions,
        }
        sync_state(
            repository,
            state_root,
            timeline,
            actions,
            report,
        )
        if resolution in {"won", "max_actions"} or outcome is not None:
            return Critique.accept(
                {
                    "stopping_reason": resolution,
                    "timeline": timeline,
                    "actions": actions,
                    "report": report,
                },
                (
                    "The trusted application detected the goal after executing the "
                    "submitted plan. The Physics run is complete."
                    if resolution == "won"
                    else (
                        "The trusted real-action budget is exhausted. No further Actor "
                        "proposal can execute; inspect trusted-report.json for the final "
                        "Timeline and execution report."
                        if resolution == "max_actions"
                        else "The trusted domain reported a terminal state "
                        f"({resolution}). No further real action is possible."
                    )
                ),
            )
        return Critique.revise(
            _execution_feedback(resolution, sorted(matching_models))
        )

    def _terminal_outcome(self, state) -> str | None:
        return classify_terminal_state(
            state,
            is_goal=self.is_goal,
            terminal_outcome=self.terminal_outcome,
        )

    def _accept_terminal(
        self,
        repository,
        state_root,
        timeline,
        actions,
        resolution,
        *,
        executed,
        evaluation,
        plan,
        supporting_models,
        matching_models,
    ):
        report = {
            "stage": "execution",
            "head": self.head,
            **(evaluation or {}),
            "plan": plan,
            "supporting_models": supporting_models,
            "executed": executed,
            "resolution": resolution,
            "matching_models": matching_models,
            "actions": actions,
        }
        sync_state(
            repository,
            state_root,
            timeline,
            actions,
            report,
        )
        return Critique.accept(
            {
                "stopping_reason": resolution,
                "timeline": timeline,
                "actions": actions,
                "report": report,
            },
            terminal_feedback(resolution),
        )

    def _evaluate(self, repository):
        report_path = _evaluation_report_path(self.head)
        request_path = _evaluation_request_path(self.head)
        request = {
            "source_path": "world_model.py",
            "timeline_path": "canonical-input.json",
            "plan_path": "plan.json",
            "search": "none",
            "work_dir": ".trusted/evaluator-work",
            "output_path": report_path,
        }
        _write_json(repository / request_path, request)
        result = yield ThreadTool(
            self.tools,
            self.critic_thread_id,
            "python_exec",
            {
                "script": evaluator_file_script(request_path),
                "timeout": self.evaluator_timeout_sec,
            },
            origin="eggopt.physics.trusted-evaluator",
            input_files=(
                request_path,
                "world_model.py",
                "canonical-input.json",
                "plan.json",
            ),
            output_files=(report_path,),
        )
        if not isinstance(result, ThreadToolResult):
            raise TypeError("trusted evaluator returned no durable file result")
        if parse_evaluator_receipt(result.output) != report_path:
            raise ValueError("trusted evaluator receipt named an unexpected report")
        return _evaluation_report(repository / report_path)

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
            repository,
            state_root,
            timeline,
            actions,
            report,
        )
        return Critique.revise(
            "The trusted Critic rejected the submitted Git HEAD before executing any "
            f"real action. Reason: {error}. Read trusted-report.json (stage=validation) "
            "and canonical-input.json. Correct world_model.py or plan.json, run the "
            "local checks as useful, then commit both world_model.py and plan.json with "
            "ordinary Git commands and leave the repository clean."
        )


def _execution_feedback(resolution: str, matching_models=()) -> str:
    if resolution == "wrong_prediction":
        return (
            "Trusted execution stopped with resolution=wrong_prediction. The observed "
            "transition is permanently appended to canonical-input.json. Models that "
            f"predicted the observed transition: {list(matching_models)}. Inspect "
            "trusted-report.json.executed, revise the theory, and submit a new plan.json."
        )
    return (
        "Trusted execution stopped with resolution=plan_exhausted. Every submitted "
        "transition matched, but the trusted application did not report the goal. "
        "Inspect the appended Timeline, reconsider the goal or extend the plan, and "
        "submit a new plan.json."
    )


def _evaluation_report_path(head: str | None) -> str:
    value = str(head or "").strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(
            "Physics Critic requires a full hexadecimal submitted Git HEAD"
        )
    return f".trusted/evaluations/{value}.json"


def _evaluation_request_path(head: str | None) -> str:
    value = _evaluation_report_path(head).rsplit("/", 1)[-1]
    return f".trusted/requests/{value}"


def _evaluation_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError("trusted evaluator report must be a JSON object")
    backtest = value.get("backtest")
    validation = value.get("plan_validation")
    planning = value.get("planning")
    if not all(isinstance(item, dict) for item in (backtest, validation, planning)):
        raise TypeError(
            "trusted evaluator report is missing backtest, plan_validation, or planning"
        )
    if not isinstance(backtest.get("surviving_models"), list):
        raise TypeError("trusted evaluator report has invalid surviving_models")
    if not isinstance(validation.get("supporting_models"), list):
        raise TypeError("trusted evaluator report has invalid supporting_models")
    if not isinstance(validation.get("predictions"), list):
        raise TypeError("trusted evaluator report has invalid plan predictions")
    if not isinstance(validation.get("model_errors"), list):
        raise TypeError("trusted evaluator report has invalid plan model_errors")
    if not isinstance(planning.get("suggestions"), list):
        raise TypeError("trusted evaluator report has invalid suggestions")
    return value


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


def sync_state(
    repository,
    state_root,
    timeline,
    actions,
    report,
):
    write_state(repository, timeline, actions, report)
    if state_root != repository:
        write_state(state_root, timeline, actions, report)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=repr) + "\n")


__all__ = ["PhysicsCritic", "read_state", "sync_state", "write_state"]
