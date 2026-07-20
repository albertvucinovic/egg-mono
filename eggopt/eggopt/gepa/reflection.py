from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eggflow import FlowExecutor, Task
from eggthreads import (
    ThreadsDB,
    append_message,
    create_child_thread,
    create_root_thread,
    list_children_with_meta,
    load_thread_projection,
)

from ._identity import canonical_candidate, canonical_json, digest_payload

_REFLECTION_OPERATION = "eggopt.gepa.reflect.v1"
_REFLECTION_REQUEST_KIND = "eggopt.gepa.reflection-request.v1"
_REFLECTION_RESPONSE_KIND = "eggopt.gepa.reflection-response.v1"


@dataclass(frozen=True)
class CandidateMutation:
    """Validated component updates returned as the reflection task result."""

    updates: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.updates, Mapping) or not self.updates:
            raise TypeError("mutation updates must be a non-empty mapping")
        normalized: dict[str, str] = {}
        for name, text in self.updates.items():
            if not isinstance(name, str) or not name:
                raise TypeError("mutation component names must be non-empty strings")
            if not isinstance(text, str):
                raise TypeError(f"mutation component {name!r} must be a string")
            normalized[name] = text
        object.__setattr__(self, "updates", normalized)


class ReflectionDrive(Protocol):
    """Injected drive for one persistent Eggthreads mutation conversation."""

    def __call__(
        self,
        conversation: "ReflectionConversation",
        request: Mapping[str, Any],
    ) -> CandidateMutation: ...


class ReflectionConversation:
    """The narrow persistence handle supplied to an injected reflection drive."""

    def __init__(self, db: ThreadsDB, thread_id: str, semantic_key: str) -> None:
        self.db = db
        self.thread_id = thread_id
        self.semantic_key = semantic_key
        self.response_message_id: str | None = None

    def append_assistant(self, content: str, mutation: CandidateMutation) -> str:
        """Persist an inspectable assistant answer plus its typed mutation."""

        if self.response_message_id is not None:
            raise RuntimeError("reflection drive already appended an assistant response")
        if not isinstance(content, str):
            raise TypeError("assistant response content must be a string")
        if not isinstance(mutation, CandidateMutation):
            raise TypeError("assistant response mutation must be CandidateMutation")
        self.response_message_id = append_message(
            self.db,
            self.thread_id,
            "assistant",
            content,
            extra={
                "eggopt_kind": _REFLECTION_RESPONSE_KIND,
                "semantic_key": self.semantic_key,
                "mutation": dict(mutation.updates),
            },
        )
        return self.response_message_id


@dataclass(frozen=True)
class ReflectionOccurrence:
    study_thread_id: str
    iteration_thread_id: str
    mutation_thread_id: str
    request_message_id: str
    response_message_id: str | None = None


@dataclass
class _ReflectionTask(Task):
    semantic_key: str
    threads_db: ThreadsDB
    study_thread_id: str
    request: dict[str, Any]
    drive: ReflectionDrive
    fail_after_response: Callable[[], None] | None = None

    def get_cache_key(self) -> str:
        # Physical thread IDs, database paths, names, and the live drive are
        # occurrence resources and intentionally do not participate.
        return self.semantic_key

    async def run(self) -> CandidateMutation:
        occurrence = _resolve_or_create_occurrence(
            self.threads_db, self.study_thread_id, self.semantic_key, self.request
        )
        persisted = _load_response(
            self.threads_db, occurrence.mutation_thread_id, self.semantic_key
        )
        if persisted is not None:
            return persisted[0]

        conversation = ReflectionConversation(
            self.threads_db, occurrence.mutation_thread_id, self.semantic_key
        )
        value = self.drive(conversation, dict(self.request))
        if inspect.isawaitable(value):
            value = await value
        mutation = _validate_mutation(value, self.request)
        if conversation.response_message_id is None:
            raise RuntimeError(
                "reflection drive must persist its assistant response with "
                "conversation.append_assistant()"
            )
        persisted = _load_response_message(
            self.threads_db,
            occurrence.mutation_thread_id,
            conversation.response_message_id,
            self.semantic_key,
        )
        if persisted != mutation:
            raise ValueError("persisted assistant mutation differs from drive result")
        if self.fail_after_response is not None:
            self.fail_after_response()
        return mutation

    async def recover(self) -> bool:
        # A completed response is already a durable typed semantic boundary.
        # run() will reuse it. No continuation is appropriate for that case.
        occurrence = _find_occurrence(self.threads_db, self.study_thread_id, self.semantic_key)
        if occurrence is not None:
            persisted = _load_response(
                self.threads_db, occurrence.mutation_thread_id, self.semantic_key
            )
            if persisted is not None:
                return True
        # The only real boundary exposed by this slice drives atomically in the
        # injected callable. A request-only thread is healthy pending work, so
        # it must not be continued speculatively.
        return True


class EggthreadsCandidateProposer:
    """Upstream ``custom_candidate_proposer`` backed by Eggthreads + Eggflow."""

    def __init__(
        self,
        executor: FlowExecutor,
        threads_db: ThreadsDB,
        *,
        drive: ReflectionDrive,
        reflector_id: str,
        reflector_version: str,
        reflector_config: Mapping[str, Any],
        study_thread_id: str | None = None,
        study_name: str = "GEPA Study",
        fail_after_response: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(reflector_id, str) or not reflector_id:
            raise ValueError("reflector_id must be a non-empty string")
        if not isinstance(reflector_version, str) or not reflector_version:
            raise ValueError("reflector_version must be a non-empty string")
        self.executor = executor
        self.threads_db = threads_db
        self.drive = drive
        self.reflector_id = reflector_id
        self.reflector_version = reflector_version
        self._reflector_config_json = canonical_json(
            reflector_config, what="reflector_config"
        )
        self.study_thread_id = study_thread_id or create_root_thread(threads_db, name=study_name)
        self.fail_after_response = fail_after_response

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        request = self._request(candidate, reflective_dataset, components_to_update)
        key = self.semantic_key(candidate, reflective_dataset, components_to_update)
        mutation = _run_sync(
            self.executor.run(
                _ReflectionTask(
                    semantic_key=key,
                    threads_db=self.threads_db,
                    study_thread_id=self.study_thread_id,
                    request=request,
                    drive=self.drive,
                    fail_after_response=self.fail_after_response,
                )
            )
        )
        if not isinstance(mutation, CandidateMutation):
            raise TypeError("reflection task must return CandidateMutation")
        return dict(mutation.updates)

    def semantic_key(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> str:
        components = _validate_components(candidate, components_to_update)
        dataset_json = canonical_json(reflective_dataset, what="reflective_dataset")
        return digest_payload(
            _REFLECTION_OPERATION,
            {
                "operation": _REFLECTION_OPERATION,
                "reflector": {
                    "id": self.reflector_id,
                    "version": self.reflector_version,
                    "config": self._reflector_config_json,
                },
                "candidate": canonical_candidate(candidate),
                "reflective_dataset": dataset_json,
                "components_to_update": components,
            },
        )

    def occurrence(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> ReflectionOccurrence | None:
        return _find_occurrence(
            self.threads_db,
            self.study_thread_id,
            self.semantic_key(candidate, reflective_dataset, components_to_update),
        )

    def _request(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> dict[str, Any]:
        components = _validate_components(candidate, components_to_update)
        # JSON round-trip makes the persisted request detached and concrete.
        dataset = json.loads(canonical_json(reflective_dataset, what="reflective_dataset"))
        return {
            "candidate": dict(canonical_candidate(candidate)),
            "reflective_dataset": dataset,
            "components_to_update": list(components),
        }


def _resolve_or_create_occurrence(
    db: ThreadsDB,
    study_thread_id: str,
    semantic_key: str,
    request: Mapping[str, Any],
) -> ReflectionOccurrence:
    found = _find_occurrence(db, study_thread_id, semantic_key)
    if found is not None:
        return found
    iteration_number = len(list_children_with_meta(db, study_thread_id)) + 1
    iteration_id = create_child_thread(
        db, study_thread_id, name=f"Iteration {iteration_number:03d}"
    )
    mutation_id = create_child_thread(db, iteration_id, name="Mutation")
    request_id = append_message(
        db,
        mutation_id,
        "user",
        _request_content(request),
        extra={
            "eggopt_kind": _REFLECTION_REQUEST_KIND,
            "semantic_key": semantic_key,
            "request": dict(request),
        },
    )
    return ReflectionOccurrence(
        study_thread_id=study_thread_id,
        iteration_thread_id=iteration_id,
        mutation_thread_id=mutation_id,
        request_message_id=request_id,
    )


def _find_occurrence(
    db: ThreadsDB, study_thread_id: str, semantic_key: str
) -> ReflectionOccurrence | None:
    for iteration_id, _name, _recap, _created in list_children_with_meta(db, study_thread_id):
        for mutation_id, _mname, _mrecap, _mcreated in list_children_with_meta(db, iteration_id):
            projection = _projection(db, mutation_id)
            request_id: str | None = None
            response_id: str | None = None
            for message in projection.messages:
                payload = message.payload
                if payload.get("semantic_key") != semantic_key:
                    continue
                if payload.get("eggopt_kind") == _REFLECTION_REQUEST_KIND:
                    request_id = message.msg_id
                elif payload.get("eggopt_kind") == _REFLECTION_RESPONSE_KIND:
                    response_id = message.msg_id
            if request_id is not None:
                return ReflectionOccurrence(
                    study_thread_id=study_thread_id,
                    iteration_thread_id=iteration_id,
                    mutation_thread_id=mutation_id,
                    request_message_id=request_id,
                    response_message_id=response_id,
                )
    return None


def _load_response(
    db: ThreadsDB, mutation_thread_id: str, semantic_key: str
) -> tuple[CandidateMutation, str] | None:
    for message in reversed(_projection(db, mutation_thread_id).messages):
        payload = message.payload
        if (
            payload.get("eggopt_kind") == _REFLECTION_RESPONSE_KIND
            and payload.get("semantic_key") == semantic_key
        ):
            # Structured payload is authority; content/name scanning is not.
            return CandidateMutation(payload.get("mutation")), message.msg_id
    return None


def _load_response_message(
    db: ThreadsDB,
    mutation_thread_id: str,
    response_message_id: str,
    semantic_key: str,
) -> CandidateMutation:
    for message in _projection(db, mutation_thread_id).messages:
        if message.msg_id != response_message_id:
            continue
        payload = message.payload
        if (
            payload.get("eggopt_kind") != _REFLECTION_RESPONSE_KIND
            or payload.get("semantic_key") != semantic_key
        ):
            raise ValueError("assistant response lacks reflection authority metadata")
        return CandidateMutation(payload.get("mutation"))
    raise ValueError("reflection drive response message was not persisted")


def _projection(db: ThreadsDB, thread_id: str):
    return load_thread_projection(db, thread_id, db.max_event_seq(thread_id))


def _validate_mutation(value: Any, request: Mapping[str, Any]) -> CandidateMutation:
    if not isinstance(value, CandidateMutation):
        raise TypeError("reflection drive must return CandidateMutation")
    allowed = set(request["components_to_update"])
    unexpected = set(value.updates) - allowed
    if unexpected:
        raise ValueError(f"mutation updated unrequested components: {sorted(unexpected)}")
    return value


def _validate_components(
    candidate: Mapping[str, str], components_to_update: Sequence[str]
) -> tuple[str, ...]:
    canonical_candidate(candidate)
    if not components_to_update:
        raise ValueError("components_to_update must not be empty")
    components: list[str] = []
    for component in components_to_update:
        if not isinstance(component, str) or component not in candidate:
            raise ValueError(f"unknown candidate component: {component!r}")
        components.append(component)
    return tuple(components)


def _request_content(request: Mapping[str, Any]) -> str:
    prompt = "Reflect on the structured GEPA evidence and return typed component updates.\n"
    return prompt + json.dumps(request, sort_keys=True, ensure_ascii=False)


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError(
        "Eggopt's synchronous GEPA proposer cannot run inside an active asyncio loop"
    )
