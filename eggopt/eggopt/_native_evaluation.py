from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, Task
from eggthreads import (
    ThreadsDB,
    append_message,
    create_child_thread,
    set_thread_working_directory,
)

from ._context import _bind_evaluation_runtime, _evaluation_scope
from ._identity import canonical_candidate, canonical_json, digest_payload

CaseT = TypeVar("CaseT")
OutputT = TypeVar("OutputT")
Candidate = dict[str, str]

_EVALUATION = "eggopt.native-gepa.evaluate.v2"
_STRUCTURE_EVENT = "eggopt.native-gepa.structure.v1"


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
    return digest_payload(
        "eggopt.native-gepa.candidate.v1", canonical_candidate(candidate)
    )


@dataclass
class _EnsureCandidateEvaluation(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    study_id: str
    run_root: Path
    candidate: Candidate

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.ensure-candidate-evaluation.v1",
            canonical_candidate(self.candidate),
        )

    def run(self) -> str:
        identity = _candidate_identity(self.candidate)
        persisted = _structure_node(self.threads, "candidate", identity)
        if persisted is not None:
            return persisted
        number = _child_count(self.threads, self.study_id, "candidate") + 1
        thread_id = create_child_thread(
            self.threads,
            self.study_id,
            name=f"Candidate {number} Evaluation",
        )
        _record_structure(
            self.threads,
            thread_id,
            kind="candidate",
            identity=identity,
            parent_id=self.study_id,
            payload={"candidate": self.candidate},
        )
        return thread_id


@dataclass
class _EnsureCaseEvaluation(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    candidate_thread_id: str
    run_root: Path
    candidate: Candidate
    case_identity: Any

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.ensure-case-evaluation.v1",
            {
                "candidate": canonical_candidate(self.candidate),
                "case": self.case_identity,
            },
        )

    def run(self) -> tuple[str, str, str]:
        identity = _case_evaluation_identity(self.candidate, self.case_identity)
        persisted = _structure_node(self.threads, "case", identity)
        workspace = _case_workspace(
            self.run_root, self.candidate, self.case_identity
        )
        if persisted is not None:
            runtime_key = _case_evaluation_identity(self.candidate, self.case_identity)
            _bind_evaluation_runtime(runtime_key, self.threads)
            return persisted, str(workspace), runtime_key
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
                reason="NativeGEPA case evaluation outerContext",
            )
        _record_structure(
            self.threads,
            thread_id,
            kind="case",
            identity=identity,
            parent_id=self.candidate_thread_id,
            payload={
                "case": self.case_identity,
                "workspace": str(workspace),
            },
        )
        runtime_key = _case_evaluation_identity(self.candidate, self.case_identity)
        _bind_evaluation_runtime(runtime_key, self.threads)
        return thread_id, str(workspace), runtime_key


@dataclass
class _EvaluateCase(Task):
    evaluator: Any = field(repr=False, compare=False)
    candidate: Candidate
    case: Any = field(repr=False, compare=False)
    evaluator_identity: Any
    case_identity: Any
    node: tuple[str, str, str]

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


@dataclass
class _RecordCaseEvaluation(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    thread_id: str
    evaluation_key: str
    evaluation: _EvaluationValue

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.record-case-evaluation.v1", self.evaluation_key
        )

    def run(self) -> None:
        summary = {
            "score": self.evaluation.score,
            "feedback": _feedback(self.evaluation),
        }
        if not _has_message(self.threads, self.thread_id, self.get_cache_key()):
            append_message(
                self.threads,
                self.thread_id,
                "system",
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                extra={
                    "eggopt_kind": "eggopt.native-gepa.case-result.v1",
                    "semantic_key": self.get_cache_key(),
                },
            )
        self.threads.append_event(
            event_id=digest_payload(
                "eggopt.native-gepa.case-result.v1", self.evaluation_key
            ),
            thread_id=self.thread_id,
            type_="eggopt.native-gepa.case-result.v1",
            payload={
                "evaluation_key": self.evaluation_key,
                "score": self.evaluation.score,
                "feedback": _feedback(self.evaluation),
            },
        )


@dataclass
class _RecordCandidateEvaluation(Task):
    threads: ThreadsDB = field(repr=False, compare=False)
    thread_id: str
    case_thread_ids: tuple[str, ...]
    case_identities: tuple[Any, ...]
    evaluations: tuple[_EvaluationValue, ...]

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.record-candidate-evaluation.v1",
            {
                "thread": self.thread_id,
                "cases": self.case_identities,
                "scores": [item.score for item in self.evaluations],
                "feedback": [_feedback(item) for item in self.evaluations],
            },
        )

    def run(self) -> None:
        summary = {
            "aggregate_score": fmean(item.score for item in self.evaluations),
            "cases": [
                {
                    "case": identity,
                    "score": evaluation.score,
                    "feedback": _feedback(evaluation),
                    "evaluation_thread_id": thread_id,
                }
                for identity, evaluation, thread_id in zip(
                    self.case_identities,
                    self.evaluations,
                    self.case_thread_ids,
                    strict=True,
                )
            ],
        }
        if not _has_message(self.threads, self.thread_id, self.get_cache_key()):
            append_message(
                self.threads,
                self.thread_id,
                "system",
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                extra={
                    "eggopt_kind": "eggopt.native-gepa.candidate-result.v1",
                    "semantic_key": self.get_cache_key(),
                },
            )
        self.threads.append_event(
            event_id=digest_payload(
                "eggopt.native-gepa.candidate-result.v1", self.get_cache_key()
            ),
            thread_id=self.thread_id,
            type_="eggopt.native-gepa.candidate-result.v1",
            payload=summary,
        )


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

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.native-gepa.evaluate-batch.v1",
            {
                "candidate": canonical_candidate(self.candidate),
                "cases": self.case_identities,
                "evaluator": self.evaluator_identity,
            },
        )

    def run(self):
        candidate_thread_id = yield _EnsureCandidateEvaluation(
            self.threads,
            self.study_id,
            self.run_root,
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
        tasks = [
            _EvaluateCase(
                self.evaluator,
                self.candidate,
                case,
                self.evaluator_identity,
                identity,
                node,
            )
            for case, identity, node in zip(
                self.cases, self.case_identities, case_nodes, strict=True
            )
        ]
        values: list[Any] = []
        width = len(tasks) if self.max_concurrency is None else self.max_concurrency
        for start in range(0, len(tasks), max(1, width)):
            values.extend((yield tasks[start : start + max(1, width)]))
        evaluations = tuple(_as_native_evaluation(value) for value in values)
        yield [
            _RecordCaseEvaluation(self.threads, node[0], task.get_cache_key(), evaluation)
            for node, task, evaluation in zip(case_nodes, tasks, evaluations, strict=True)
        ]
        yield _RecordCandidateEvaluation(
            self.threads,
            candidate_thread_id,
            tuple(node[0] for node in case_nodes),
            self.case_identities,
            evaluations,
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
        "eggopt.native-gepa.case.v1",
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
    return (text[:48] or fallback)


def _record_structure(
    db: ThreadsDB,
    thread_id: str,
    *,
    kind: str,
    identity: str,
    parent_id: str,
    payload: Mapping[str, Any],
) -> None:
    db.append_event(
        event_id=digest_payload(_STRUCTURE_EVENT, {"kind": kind, "identity": identity}),
        thread_id=thread_id,
        type_=_STRUCTURE_EVENT,
        payload={
            "kind": kind,
            "identity": identity,
            "thread_id": thread_id,
            "parent_id": parent_id,
            **payload,
        },
    )


def _structure_node(db: ThreadsDB, kind: str, identity: str) -> str | None:
    row = db.conn.execute(
        "SELECT json_extract(payload_json, '$.thread_id') FROM events "
        "WHERE type=? AND json_extract(payload_json, '$.kind')=? "
        "AND json_extract(payload_json, '$.identity')=? "
        "ORDER BY event_seq LIMIT 1",
        (_STRUCTURE_EVENT, kind, identity),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _child_count(db: ThreadsDB, parent_id: str, kind: str) -> int:
    return int(
        db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type=? "
            "AND json_extract(payload_json, '$.kind')=? "
            "AND json_extract(payload_json, '$.parent_id')=?",
            (_STRUCTURE_EVENT, kind, parent_id),
        ).fetchone()[0]
    )


def _has_message(db: ThreadsDB, thread_id: str, semantic_key: str) -> bool:
    return (
        db.conn.execute(
            "SELECT 1 FROM events WHERE thread_id=? AND type='msg.create' "
            "AND json_extract(payload_json, '$.semantic_key')=? LIMIT 1",
            (thread_id, semantic_key),
        ).fetchone()
        is not None
    )

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
