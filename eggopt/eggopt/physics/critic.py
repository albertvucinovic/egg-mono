from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, TaskError

from ..actor_critic import Critique
from ..identity import digest_payload
from ..thread_tool import ThreadTool, ThreadToolResult
from .instruments import write_actor_files
from .planning import canonical_plan, freeze, load_committed_plan
from .theory import evaluator_file_script, parse_evaluator_receipt


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
    evaluator_timeout_sec: float = 300.0
    workspace: str | None = None
    outer_context: str | None = None
    head: str | None = None
    critic_thread_id: str | None = None
    max_actions: int = 100

    def get_cache_key(self):
        return digest_payload(
            "eggopt.physics.domain-critic.v1.file-inputs",
            {
                "identity": self.identity,
                "head": self.head,
                "max_actions": self.max_actions,
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
                "evaluator_timeout_sec": self.evaluator_timeout_sec,
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
                repository,
                state_root,
                timeline,
                actions,
                "world_model.py is missing from the submitted Git HEAD",
            )

        try:
            report_path = _evaluation_report_path(self.head)
            request_path = _evaluation_request_path(self.head)
            request = {
                "source_path": "world_model.py",
                "timeline_path": "canonical-input.json",
                "legal_actions_key": self.legal_actions_key,
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
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
                input_files=(request_path, "world_model.py", "canonical-input.json"),
                output_files=(report_path,),
            )
            if not isinstance(result, ThreadToolResult):
                raise TypeError("trusted evaluator returned no durable file result")
            if parse_evaluator_receipt(result.output) != report_path:
                raise ValueError("trusted evaluator receipt named an unexpected report")
            evaluation = _evaluation_report(repository / report_path)
            committed = load_committed_plan(repository)
            committed = canonical_plan(committed)
        except (OSError, TaskError, TypeError, ValueError, RuntimeError) as exc:
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
                "committed-plan.json does not exactly match any plan independently "
                "generated from the submitted world_model.py and canonical Timeline",
                evaluation,
            )
        if not set(committed["models"]) <= set(backtest["surviving_models"]):
            return self._revise(
                repository,
                state_root,
                timeline,
                actions,
                "The committed plan references a model with one or more Timeline "
                "mismatches. Inspect trusted-report.json under "
                "evaluation.backtest.models, repair or remove the contradicted model, "
                "then rerun backtest.py and plan.py.",
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
                "The first committed action is not listed in the canonical current "
                "state's legal actions. Rerun plan.py from the latest "
                "canonical-input.json and choose a newly returned plan",
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
                (
                    "The trusted application detected the goal after executing the "
                    "committed plan. The Physics run is complete."
                    if resolution == "won"
                    else "The trusted real-action budget is exhausted. No further Actor "
                    "proposal can execute; inspect trusted-report.json for the final "
                    "Timeline and execution report."
                ),
            )
        return Critique.revise(_execution_feedback(resolution))

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
            "The trusted Critic rejected the submitted Git HEAD before executing any "
            f"real action. Reason: {error}. Read trusted-report.json (stage=validation) "
            "and canonical-input.json. Correct world_model.py or select a plan newly "
            "returned by plan.py, run the local checks, then finish this turn with "
            "python commit.py plan-N and an otherwise clean repository."
        )


def _execution_feedback(resolution: str) -> str:
    guidance = {
        "wrong_prediction": (
            "No selected model predicted the observed next public state. The mismatched "
            "transition is now permanently appended to canonical-input.json. Inspect "
            "trusted-report.json.executed, revise state grounding and/or transition "
            "mechanisms so the full Timeline backtests exactly, then generate and "
            "commit a new plan."
        ),
        "models_discriminated": (
            "The Critic executed through the first intent where the selected models "
            "disagreed, then stopped after observing reality. Inspect "
            "trusted-report.json.compatible_models and .executed, retain or revise the "
            "hypotheses supported by that observation, rerun both instruments, and "
            "commit the next goal plan or experiment."
        ),
        "plan_exhausted": (
            "Every committed intent ran without a prediction mismatch, but the trusted "
            "application did not report the goal. Treat that outcome as evidence about "
            "reward/goal inference. Inspect trusted-report.json and the appended "
            "Timeline, revise the utility or mechanism if needed, and commit another "
            "planner-returned plan."
        ),
    }
    return f"Trusted execution stopped with resolution={resolution}. " + guidance.get(
        resolution,
        "Inspect trusted-report.json and canonical-input.json, revise the theory, "
        "rerun backtest.py and plan.py, and commit another planner-returned plan.",
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
    planning = value.get("planning")
    if not isinstance(backtest, dict) or not isinstance(planning, dict):
        raise TypeError("trusted evaluator report is missing backtest or planning")
    if not isinstance(backtest.get("surviving_models"), list):
        raise TypeError("trusted evaluator report has invalid surviving_models")
    if not isinstance(planning.get("plans"), list):
        raise TypeError("trusted evaluator report has invalid plans")
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


def sync_state(repository, state_root, timeline, actions, report, domain_information):
    write_state(repository, timeline, actions, report)
    if state_root != repository:
        write_state(state_root, timeline, actions, report)
    write_actor_files(repository, timeline, domain_information)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=repr) + "\n")


__all__ = ["PhysicsCritic", "read_state", "sync_state", "write_state"]
