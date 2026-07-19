"""Optional durable map-and-aggregate evaluation Producer composition."""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass
from typing import Generic, TypeVar

from eggflow import Task

from .core import CaseEvidence, Observation, Producer
from .eggflow import ProduceTask
from .evaluation import CaseRequest, EvaluationRequest

CaseT = TypeVar("CaseT")

_EVALUATION_SCHEMA = b"eggopt.EvaluationTask:v1\0"

__all__ = ["EvaluationProducer", "EvaluationTask"]


@dataclass
class EvaluationTask(Task, Generic[CaseT]):
    """Durably map ordered cases and aggregate their complete evidence."""

    case_producer: Producer[CaseRequest[CaseT], CaseEvidence | Task]
    case_identity: str
    aggregate: Producer[Observation, Observation | Task]
    aggregate_identity: str
    request: EvaluationRequest[CaseT]

    def __post_init__(self) -> None:
        _validate_producer(self.case_producer, "case_producer")
        _validate_identity(self.case_identity, "case_identity")
        _validate_producer(self.aggregate, "aggregate")
        _validate_identity(self.aggregate_identity, "aggregate_identity")
        if not isinstance(self.request, EvaluationRequest):
            raise TypeError("request must be an EvaluationRequest")

    def get_cache_key(self) -> str:
        try:
            serialized_request = pickle.dumps(self.request, protocol=5)
        except Exception as exc:
            raise TypeError(
                "EvaluationTask request must be pickleable for cache identity"
            ) from exc
        request_digest = hashlib.sha256(serialized_request).digest()
        key_values = (
            self.case_identity,
            self.aggregate_identity,
            request_digest,
        )
        return hashlib.sha256(
            _EVALUATION_SCHEMA + pickle.dumps(key_values, protocol=5)
        ).hexdigest()

    def run(self):
        case_tasks = [
            ProduceTask(
                producer=self.case_producer,
                producer_identity=self.case_identity,
                value=CaseRequest(self.request.candidate, case),
            )
            for case in self.request.cases
        ]
        case_results = yield case_tasks
        if not all(isinstance(result, CaseEvidence) for result in case_results):
            raise TypeError("case_producer must produce CaseEvidence values")
        cases = tuple(case_results)
        base = Observation(candidate=self.request.candidate, cases=cases)
        result = yield ProduceTask(
            producer=self.aggregate,
            producer_identity=self.aggregate_identity,
            value=base,
        )
        if not isinstance(result, Observation):
            raise TypeError("aggregate must produce an Observation")
        if result.candidate != self.request.candidate:
            raise ValueError("aggregate must preserve the request candidate")
        if result.cases != cases:
            raise ValueError("aggregate must preserve all case evidence in order")
        return result


@dataclass(frozen=True)
class EvaluationProducer(Generic[CaseT]):
    """Process-local Producer adapter returning one durable evaluation Task."""

    case_producer: Producer[CaseRequest[CaseT], CaseEvidence | Task]
    case_identity: str
    aggregate: Producer[Observation, Observation | Task]
    aggregate_identity: str

    def __post_init__(self) -> None:
        _validate_producer(self.case_producer, "case_producer")
        _validate_identity(self.case_identity, "case_identity")
        _validate_producer(self.aggregate, "aggregate")
        _validate_identity(self.aggregate_identity, "aggregate_identity")

    def produce(self, request: EvaluationRequest[CaseT]) -> EvaluationTask[CaseT]:
        if not isinstance(request, EvaluationRequest):
            raise TypeError("request must be an EvaluationRequest")
        return EvaluationTask(
            case_producer=self.case_producer,
            case_identity=self.case_identity,
            aggregate=self.aggregate,
            aggregate_identity=self.aggregate_identity,
            request=request,
        )


def _validate_producer(value: object, name: str) -> None:
    if not isinstance(value, Producer):
        raise TypeError(f"{name} must implement Producer")


def _validate_identity(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
