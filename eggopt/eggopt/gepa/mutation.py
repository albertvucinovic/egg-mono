from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from eggflow import Task

from ..actor_critic import ActorCritic, Agent
from ..identity import canonical_candidate, canonical_json, digest_payload
from .tools import gepa_safe_tools

DEFAULT_MUTATION_SYSTEM_PROMPT = (
    "You are the mutation agent in an optimization study. Follow each user "
    "request, analyze its evidence, and return the requested candidate mutation."
)


@dataclass(frozen=True)
class Mutation:
    updates: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.updates, Mapping) or not self.updates:
            raise TypeError("mutation updates must be a non-empty mapping")
        object.__setattr__(self, "updates", dict(canonical_candidate(self.updates)))


@dataclass(frozen=True)
class Mutator:
    """A GEPA mutation Actor checked by a deterministic Critic Task."""

    agent: Agent
    instruction: str
    max_correction_turns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("mutation instruction must be a non-empty string")
        if (
            isinstance(self.max_correction_turns, bool)
            or not isinstance(self.max_correction_turns, int)
            or self.max_correction_turns < 0
        ):
            raise ValueError("max_correction_turns must be a non-negative integer")

    @classmethod
    def eggthreads(
        cls,
        *,
        llm: Any,
        identity: Mapping[str, Any],
        instruction: str,
        tools: Any = None,
        allowed_tools: set[str] | frozenset[str] | None = None,
        model_key: str | None = None,
        models_path: str = "models.json",
        runner_config: Any = None,
        auto_approve_tools: bool = False,
        max_correction_turns: int = 0,
        context_limit: int | None = None,
        system_prompt: str = DEFAULT_MUTATION_SYSTEM_PROMPT,
    ) -> Mutator:
        kwargs = {
            "model_key": model_key,
            "models_path": models_path,
            "auto_approve_tools": auto_approve_tools,
            "context_limit": context_limit,
            "system_prompt": system_prompt,
        }
        registry, allowed = gepa_safe_tools(tools, allowed_tools=allowed_tools)
        kwargs["tools"] = registry
        kwargs["allowed_tools"] = allowed
        if runner_config is not None:
            kwargs["runner_config"] = runner_config
        return cls(
            Agent(llm, identity, **kwargs),
            instruction.strip(),
            max_correction_turns,
        )

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "agent": self.agent.identity,
            "instruction": self.instruction,
            "max_correction_turns": self.max_correction_turns,
            "system_prompt": self.agent.system_prompt,
        }


@dataclass
class Mutate(Task):
    mutator: Mutator = field(repr=False, compare=False)
    parents: tuple[Mapping[str, str], ...]
    evidence: tuple[Mapping[str, Any], ...]
    objective: str
    generation: int

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.gepa.mutate.v1",
            {
                "mutator": self.mutator.identity,
                "parents": [canonical_candidate(parent) for parent in self.parents],
                "evidence": json.loads(
                    canonical_json(self.evidence, what="mutation evidence")
                ),
                "objective": self.objective,
                "generation": self.generation,
            },
        )

    def run(self):
        request = MutationRequest(self.parents, self.evidence, self.objective)
        result = yield ActorCritic(
            actor=self.mutator.agent,
            critic=ValidateMutation(tuple(self.parents[0])),
            actor_prompt=lambda round_number, state: (
                request.prompt(self.mutator.instruction)
                if round_number == 1
                else state["feedback"]
            ),
            max_rounds=self.mutator.max_correction_turns + 1,
            names=("Mutation", "Validation"),
        )
        if not result.accepted:
            raise ValueError(f"mutation remained invalid: {result.feedback}")
        return _mutation(result.answer, tuple(self.parents[0]))


@dataclass(frozen=True)
class MutationRequest:
    parents: tuple[Mapping[str, str], ...]
    evidence: tuple[Mapping[str, Any], ...]
    objective: str

    def prompt(self, instruction: str) -> str:
        body = {
            "objective": self.objective,
            "selected_parents": [dict(parent) for parent in self.parents],
            "evaluation_evidence": list(self.evidence),
            "components_to_update": list(self.parents[0]),
        }
        return (
            f"{instruction}\n\n"
            f"{json.dumps(body, indent=2, sort_keys=True)}\n\n"
            "Return only strict JSON with exactly the key 'mutations', containing "
            "one object that updates only the listed components."
        )


@dataclass
class ValidateMutation(Task):
    components: tuple[str, ...]
    answer: Any = None

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.gepa.validate-mutation.v1",
            {"components": self.components},
        )

    def run(self) -> Mapping[str, str]:
        try:
            _mutation(self.answer, self.components)
        except (TypeError, ValueError) as exc:
            components = ", ".join(self.components)
            reason = " ".join(str(exc).split())[:300]
            return {
                "decision": "revise",
                "feedback": (
                    f"Your previous response could not be accepted: {reason}. "
                    "Return only strict JSON with exactly the key 'mutations', "
                    f"containing one object that updates only: {components}. "
                    "Do not include Markdown or commentary."
                ),
            }
        return {"decision": "accept", "feedback": "Valid mutation."}


def _mutation(answer: Any, components: Sequence[str]) -> Mutation:
    if not isinstance(answer, str):
        raise ValueError("mutation response must be a JSON string")
    try:
        payload = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise ValueError("mutation response must be strict JSON") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"mutations"}:
        raise ValueError("mutation JSON must contain only 'mutations'")
    values = payload["mutations"]
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("mutation JSON must contain exactly one mutation")
    mutation = Mutation(values[0])
    unexpected = set(mutation.updates) - set(components)
    if unexpected:
        raise ValueError(
            f"mutation updated unrequested components: {sorted(unexpected)}"
        )
    return mutation


__all__ = [
    "DEFAULT_MUTATION_SYSTEM_PROMPT",
    "Mutate",
    "Mutation",
    "MutationRequest",
    "Mutator",
    "ValidateMutation",
]
