"""Optional Eggflow adapter for durable execution of synchronous Producers."""

from __future__ import annotations

import hashlib
import pickle
import struct
from dataclasses import dataclass
from typing import Generic, TypeVar

from eggflow import Task

from .core import Producer

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

_CACHE_SCHEMA = b"eggopt.ProduceTask:v1\0"


@dataclass
class ProduceTask(Task, Generic[InputT, OutputT]):
    """Cache one Producer result under explicit semantics and serialized input."""

    producer: Producer[InputT, OutputT]
    producer_identity: str
    value: InputT

    def __post_init__(self) -> None:
        _validate_producer(self.producer)
        _validate_identity(self.producer_identity)

    def run(self) -> OutputT:
        """Invoke the synchronous Producer exactly once on a cache miss."""

        return self.producer.produce(self.value)

    def get_cache_key(self) -> str:
        """Key by adapter schema, caller-owned identity, and pickled input."""

        try:
            serialized_value = pickle.dumps(self.value, protocol=5)
        except Exception as exc:
            raise TypeError(
                "ProduceTask value must be pickleable for cache identity"
            ) from exc
        value_digest = hashlib.sha256(serialized_value).digest()
        encoded_identity = self.producer_identity.encode("utf-8")
        key_material = (
            _CACHE_SCHEMA
            + struct.pack(">Q", len(encoded_identity))
            + encoded_identity
            + value_digest
        )
        return hashlib.sha256(key_material).hexdigest()


@dataclass(frozen=True)
class EggflowProducer(Generic[InputT, OutputT]):
    """Wrap a synchronous Producer so production yields a cacheable Eggflow Task."""

    producer: Producer[InputT, OutputT]
    producer_identity: str

    def __post_init__(self) -> None:
        _validate_producer(self.producer)
        _validate_identity(self.producer_identity)

    def produce(self, value: InputT) -> ProduceTask[InputT, OutputT]:
        """Create the durable task for ``value`` without executing it."""

        return ProduceTask(
            producer=self.producer,
            producer_identity=self.producer_identity,
            value=value,
        )


def _validate_producer(value: object) -> None:
    if not isinstance(value, Producer):
        raise TypeError("producer must implement Producer")


def _validate_identity(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("producer_identity must be a string")
    if not value:
        raise ValueError("producer_identity must not be empty")
