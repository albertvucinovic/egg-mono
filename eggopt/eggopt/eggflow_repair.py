"""Optional durable composition for cumulative same-instance repair."""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass
from typing import Generic, TypeVar

from eggflow import Task, TaskError

from .core import Producer
from .eggflow import ProduceTask
from .repair import Accepted, ItemFailure, NeedsRepair, RepairInput

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

_REPAIR_SCHEMA = b"eggopt.RepairTask:v1\0"

__all__ = ["RepairTask", "RepairingProducer"]


@dataclass
class RepairTask(Task, Generic[InputT, OutputT]):
    """Durably produce, inspect, and feed expected invalid output back."""

    inner: Producer[RepairInput[InputT], OutputT]
    inner_identity: str
    inspect: Producer[OutputT, Accepted[OutputT] | NeedsRepair]
    inspect_identity: str
    max_repairs: int
    original: InputT

    def __post_init__(self) -> None:
        _validate_producer(self.inner, "inner")
        _validate_identity(self.inner_identity, "inner_identity")
        _validate_producer(self.inspect, "inspect")
        _validate_identity(self.inspect_identity, "inspect_identity")
        _validate_max_repairs(self.max_repairs)

    def get_cache_key(self) -> str:
        try:
            serialized_original = pickle.dumps(self.original, protocol=5)
        except Exception as exc:
            raise TypeError(
                "RepairTask original must be pickleable for cache identity"
            ) from exc
        original_digest = hashlib.sha256(serialized_original).digest()
        key_values = (
            self.inner_identity,
            self.inspect_identity,
            self.max_repairs,
            original_digest,
        )
        return hashlib.sha256(
            _REPAIR_SCHEMA + pickle.dumps(key_values, protocol=5)
        ).hexdigest()

    def run(self):
        feedback = ()
        for attempt in range(self.max_repairs + 1):
            try:
                output = yield ProduceTask(
                    producer=self.inner,
                    producer_identity=_attempt_identity(
                        self.inner_identity, attempt
                    ),
                    value=RepairInput(self.original, feedback),
                )
                inspection = yield ProduceTask(
                    producer=self.inspect,
                    producer_identity=_attempt_identity(
                        self.inspect_identity, attempt
                    ),
                    value=output,
                )
            except TaskError as error:
                if error.is_terminal:
                    return ItemFailure(
                        kind="terminal",
                        reason=str(error),
                        attempts=attempt + 1,
                    )
                raise

            if isinstance(inspection, Accepted):
                return inspection.value
            if not isinstance(inspection, NeedsRepair):
                raise TypeError(
                    "inspect must produce Accepted or NeedsRepair"
                )
            if attempt == self.max_repairs:
                return ItemFailure(
                    kind="repair_exhausted",
                    reason=inspection.feedback.text,
                    attempts=attempt + 1,
                )
            feedback = feedback + (inspection.feedback,)

        raise AssertionError("repair loop exhausted unexpectedly")


@dataclass(frozen=True)
class RepairingProducer(Generic[InputT, OutputT]):
    """Process-local Producer adapter returning one durable repair item Task."""

    inner: Producer[RepairInput[InputT], OutputT]
    inner_identity: str
    inspect: Producer[OutputT, Accepted[OutputT] | NeedsRepair]
    inspect_identity: str
    max_repairs: int

    def __post_init__(self) -> None:
        _validate_producer(self.inner, "inner")
        _validate_identity(self.inner_identity, "inner_identity")
        _validate_producer(self.inspect, "inspect")
        _validate_identity(self.inspect_identity, "inspect_identity")
        _validate_max_repairs(self.max_repairs)

    def produce(self, original: InputT) -> RepairTask[InputT, OutputT]:
        return RepairTask(
            inner=self.inner,
            inner_identity=self.inner_identity,
            inspect=self.inspect,
            inspect_identity=self.inspect_identity,
            max_repairs=self.max_repairs,
            original=original,
        )


def _attempt_identity(identity: str, attempt: int) -> str:
    return f"{identity}:repair-attempt:{attempt}"


def _validate_producer(value: object, name: str) -> None:
    if not isinstance(value, Producer):
        raise TypeError(f"{name} must implement Producer")


def _validate_identity(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _validate_max_repairs(value: object) -> None:
    if not isinstance(value, int):
        raise TypeError("max_repairs must be an integer")
    if value < 0:
        raise ValueError("max_repairs must be nonnegative")
