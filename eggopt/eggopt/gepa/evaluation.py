from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, Task
from eggthreads import (
    ThreadsDB,
    create_child_thread,
    get_context_limit,
    list_children_with_meta,
    set_context_limit,
    set_thread_working_directory,
)

from ..context import _bind_evaluation_runtime, _evaluation_scope
from ..identity import canonical_candidate, canonical_json, digest_payload

CaseT = TypeVar("CaseT")
OutputT = TypeVar("OutputT")
Candidate = dict[str, str]

_EVALUATION = "eggopt.gepa.evaluate.v2"


@dataclass(frozen=True)
class _EvaluationValue:
    score: float
    output: Any = None
    feedback: Any = ""
    evidence: Any = None

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("evaluator score must be a finite number")
        score = float(self.score)
        if not (-float("inf") < score < float("inf")):
            raise ValueError("evaluator score must be finite")
        _json_value(self.feedback, "feedback")
        object.__setattr__(self, "score", score)


def _json_value(value: Any, what: str) -> Any:
    return json.loads(canonical_json(value, what=what))


def _candidate_identity(candidate: Candidate) -> str:
    return digest_payload("eggopt.gepa.candidate.v1", canonical_candidate(candidate))


@dataclass
class _EnsureCandidateEvaluation(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    candidate: Candidate

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.gepa.ensure-candidate-evaluation.v1",
            canonical_candidate(self.candidate),
        )

    def run(self) -> str:
        number = 1 + sum(
            name.startswith("Candidate ") and name.endswith(" Evaluation")
            for _thread_id, name, *_rest in list_children_with_meta(
                self.threads, self.study_id
            )
        )
        return create_child_thread(
            self.threads,
            self.study_id,
            name=f"Candidate {number} Evaluation",
            inherit_tools_config=False,
        )


@dataclass
class _EnsureCaseEvaluation(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    candidate_thread_id: str
    run_root: Path
    candidate: Candidate
    case_identity: Any

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.gepa.ensure-case-evaluation.v1",
            {
                "candidate": canonical_candidate(self.candidate),
                "case": self.case_identity,
            },
        )

    def run(self) -> tuple[str, str, str]:
        workspace = _case_workspace(self.run_root, self.candidate, self.case_identity)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "innerContext").mkdir(exist_ok=True)
        thread_id = create_child_thread(
            self.threads,
            self.candidate_thread_id,
            name=f"{_semantic_name(self.case_identity, 'Case')} Evaluation",
        )
        try:
            workspace.relative_to(Path.cwd().resolve())
        except ValueError:
            # Eggthreads deliberately refuses cwd escapes. The durable
            # workspace reference remains recorded for domain-owned Tasks.
            pass
        else:
            set_thread_working_directory(
                self.threads,
                thread_id,
                str(workspace),
                reason="GEPA case evaluation outerContext",
            )
        runtime_key = _case_evaluation_identity(self.candidate, self.case_identity)
        return thread_id, str(workspace), runtime_key


@dataclass
class _EvaluateCase(Task):
    evaluator: Any = field(repr=False, compare=False)
    candidate: Candidate
    case: Any = field(repr=False, compare=False)
    evaluator_identity: Any
    case_identity: Any
    node: tuple[str, str, str]
    context_limit: int | None = None

    def get_cache_key(self) -> str:
        return digest_payload(
            _EVALUATION,
            {
                "evaluator": canonical_json(
                    self.evaluator_identity, what="evaluator identity"
                ),
                "candidate": canonical_candidate(self.candidate),
                "example": canonical_json(self.case_identity, what="case identity"),
            },
        )

    def run(self):
        context = {
            "evaluation_thread_id": self.node[0],
            "outer_context": self.node[1],
            "inner_context": str(Path(self.node[1]) / "innerContext"),
            "_runtime_key": self.node[2],
            "_evaluation_key": self.get_cache_key(),
            "_context_limit": self.context_limit,
        }
        with _evaluation_scope(context):
            factory = getattr(self.evaluator, "task", None)
            if callable(factory):
                value = yield factory(dict(self.candidate), self.case)
            else:
                value = self.evaluator(dict(self.candidate), self.case)
                if isinstance(value, Task):
                    value = yield value
                elif inspect.isawaitable(value):
                    value = yield _Await(value)
            return _as_native_evaluation(value)


@dataclass
class _Await(Task):
    cacheable = False
    awaitable: Any = field(repr=False, compare=False)

    async def run(self):
        return await self.awaitable


@dataclass(frozen=True)
class _CandidateEvaluation(Generic[OutputT]):
    evaluations: tuple[_EvaluationValue, ...]
    candidate_thread_id: str
    case_thread_ids: tuple[str, ...]

    @property
    def scores(self) -> tuple[float, ...]:
        return tuple(item.score for item in self.evaluations)

    @property
    def outputs(self) -> tuple[OutputT | None, ...]:
        return tuple(item.output for item in self.evaluations)

    @property
    def feedback(self) -> tuple[Any, ...]:
        return tuple(_feedback(item) for item in self.evaluations)


@dataclass
class _EvaluateCandidate(Task, Generic[CaseT, OutputT]):
    cacheable = False

    flow: FlowExecutor = field(repr=False, compare=False)
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    run_root: Path
    candidate: Candidate
    cases: list[CaseT] = field(repr=False, compare=False)
    case_identities: tuple[Any, ...]
    evaluator: Any = field(repr=False, compare=False)
    evaluator_identity: Any
    max_concurrency: int | None
    context_limit: int | None
    progress: Callable[[Mapping[str, Any]], None] | None = field(
        default=None, repr=False, compare=False
    )
    stage: str = "evaluation"

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.gepa.evaluate-batch.v1",
            {
                "candidate": canonical_candidate(self.candidate),
                "cases": self.case_identities,
                "evaluator": self.evaluator_identity,
                "context_limit": self.context_limit,
            },
        )

    def run(self):
        candidate_thread_id = yield _EnsureCandidateEvaluation(
            self.threads,
            self.study_id,
            self.candidate,
        )
        case_nodes = yield [
            _EnsureCaseEvaluation(
                self.threads,
                candidate_thread_id,
                self.run_root,
                self.candidate,
                identity,
            )
            for identity in self.case_identities
        ]
        for _thread_id, _workspace, runtime_key in case_nodes:
            _bind_evaluation_runtime(runtime_key, self.threads)
        if self.context_limit is not None:
            for thread_id, _workspace, _runtime_key in case_nodes:
                if get_context_limit(self.threads, thread_id) != self.context_limit:
                    set_context_limit(
                        self.threads,
                        thread_id,
                        self.context_limit,
                        reason="GEPA evaluator context budget",
                    )
        tasks = [
            _EvaluateCase(
                self.evaluator,
                self.candidate,
                case,
                self.evaluator_identity,
                identity,
                node,
                self.context_limit,
            )
            for case, identity, node in zip(
                self.cases, self.case_identities, case_nodes, strict=True
            )
        ]
        if self.progress is not None:
            self.progress(
                {
                    "kind": "candidate_evaluation_started",
                    "stage": self.stage,
                    "candidate_thread_id": candidate_thread_id,
                    "candidate": dict(self.candidate),
                    "case_count": len(tasks),
                }
            )
        values: list[Any] = []
        width = len(tasks) if self.max_concurrency is None else self.max_concurrency
        for start in range(0, len(tasks), max(1, width)):
            batch = yield tasks[start : start + max(1, width)]
            values.extend(batch)
            if self.progress is not None:
                for index, value in enumerate(batch, start=start):
                    evaluation = _as_native_evaluation(value)
                    self.progress(
                        {
                            "kind": "case_evaluation",
                            "stage": self.stage,
                            "candidate_thread_id": candidate_thread_id,
                            "candidate": dict(self.candidate),
                            "case": self.case_identities[index],
                            "case_number": index + 1,
                            "case_count": len(tasks),
                            "score": evaluation.score,
                            "feedback": _feedback(evaluation),
                            "evaluation_thread_id": case_nodes[index][0],
                        }
                    )
        evaluations = tuple(_as_native_evaluation(value) for value in values)
        if self.progress is not None:
            self.progress(
                {
                    "kind": "candidate_evaluation",
                    "stage": self.stage,
                    "candidate_thread_id": candidate_thread_id,
                    "candidate": dict(self.candidate),
                    "aggregate_score": fmean(item.score for item in evaluations),
                    "case_count": len(evaluations),
                }
            )
        return _CandidateEvaluation(
            evaluations,
            candidate_thread_id,
            tuple(node[0] for node in case_nodes),
        )


def _as_native_evaluation(value: Any) -> _EvaluationValue:
    if hasattr(value, "score"):
        return _EvaluationValue(
            value.score,
            getattr(value, "output", None),
            _json_value(getattr(value, "feedback", ""), "feedback"),
            getattr(value, "evidence", None),
        )
    if isinstance(value, tuple):
        if len(value) != 2:
            raise TypeError("evaluator tuple must be (score, feedback)")
        return _EvaluationValue(value[0], feedback=_json_value(value[1], "feedback"))
    return _EvaluationValue(value)


def _feedback(evaluation: _EvaluationValue) -> Any:
    return evaluation.feedback


def _case_evaluation_identity(candidate: Candidate, case_identity: Any) -> str:
    return digest_payload(
        "eggopt.gepa.case.v1",
        {"candidate": canonical_candidate(candidate), "case": case_identity},
    )


def _case_workspace(root: Path, candidate: Candidate, case_identity: Any) -> Path:
    return (
        root
        / "workspaces"
        / f"candidate-{_candidate_identity(candidate).rsplit(':', 1)[-1][:10]}"
        / f"case-{_short_identity(case_identity)}"
        / "outerContext"
    )


def _short_identity(value: Any) -> str:
    digest = digest_payload("eggopt.case.v1", value).rsplit(":", 1)[-1][:10]
    return f"{_semantic_name(value, 'case').casefold()}-{digest}"


def _semantic_name(value: Any, fallback: str) -> str:
    if isinstance(value, Mapping):
        for key in ("name", "id", "case_id"):
            if value.get(key):
                value = value[key]
                break
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-._")
    return text[:48] or fallback


def _new_call_count(flow, candidate, cases, case_ids, evaluator, evaluator_identity):
    count = 0
    for case, case_identity in zip(cases, case_ids, strict=True):
        task = _EvaluateCase(
            evaluator,
            candidate,
            case,
            evaluator_identity,
            case_identity,
            ("budget-only", "budget-only", "budget-only"),
        )
        row = flow.store.get(task.get_cache_key())
        if row is None or row["status"] != "COMPLETED":
            count += 1
    return count


def _completed_evaluator_calls(flow: FlowExecutor) -> int:
    prefix = f"{_EVALUATION}:"
    return int(
        flow.store.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='COMPLETED' AND cache_key LIKE ?",
            (prefix + "%",),
        ).fetchone()[0]
    )
