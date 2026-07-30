from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from eggflow import Task

from ..identity import canonical_value, digest_payload

Mutator = Task | Callable[["MutationContext"], Any]


@dataclass(frozen=True)
class MutationContext:
    """Domain input for producing one complete candidate."""

    parents: tuple[Any, ...]
    evidence: tuple[Mapping[str, Any], ...]
    objective: str
    generation: int
    full_validation_scores: tuple[Mapping[str, Any], ...] = ()
    last_candidate_result: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parents",
            tuple(
                canonical_value(value, what="mutation parent") for value in self.parents
            ),
        )

    def identity(self) -> Mapping[str, Any]:
        value = {
            "parents": self.parents,
            "evidence": self.evidence,
            "objective": self.objective,
            "generation": self.generation,
            "full_validation_scores": self.full_validation_scores,
        }
        if self.last_candidate_result is not None:
            value["last_candidate_result"] = self.last_candidate_result
        return canonical_value(value, what="mutation context")


@dataclass
class Mutate(Task):
    """Resolve a domain Mutator into one complete, durable candidate value."""

    mutator: Mutator = field(repr=False, compare=False)
    context: MutationContext

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.gepa.mutate.v2",
            {
                "mutator": _mutator_identity(self.mutator),
                "context": self.context.identity(),
            },
        )

    def run(self):
        if isinstance(self.mutator, Task):
            value = self.mutator
        else:
            value = self.mutator(self.context)
        if isinstance(value, Task):
            value = yield value
        elif inspect.isawaitable(value):
            value = yield _Await(value)
        return canonical_value(value, what="candidate")


@dataclass
class _Await(Task):
    cacheable = False
    value: Any = field(repr=False, compare=False)

    async def run(self):
        return await self.value


def _mutator_identity(mutator: Mutator) -> Mapping[str, str]:
    if isinstance(mutator, Task):
        return {
            "module": mutator.__class__.__module__,
            "name": mutator.__class__.__qualname__,
            "key": mutator.get_cache_key(),
        }
    module = getattr(mutator, "__module__", mutator.__class__.__module__)
    name = getattr(mutator, "__qualname__", mutator.__class__.__qualname__)
    if not module or name == "<lambda>":
        raise TypeError(
            "mutator must have a stable identity; use a named callable or Task"
        )
    identity = {"module": module, "name": name}
    cache_key = getattr(mutator, "get_cache_key", None)
    if callable(cache_key):
        identity["key"] = cache_key()
    return identity


__all__ = ["Mutate", "MutationContext", "Mutator"]
