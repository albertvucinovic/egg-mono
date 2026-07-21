from __future__ import annotations

import asyncio
import inspect
import json
import pickle
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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

_REFLECTION_OPERATION = "eggopt.gepa.reflect.v2"
_REFLECTION_REQUEST_KIND = "eggopt.gepa.reflection-request.v1"
_REFLECTION_RESPONSE_KIND = "eggopt.gepa.reflection-response.v1"
_DEFAULT_REFLECTION_INSTRUCTION = (
    "Reflect on the structured GEPA evidence and return the requested typed mutations."
)


@dataclass(frozen=True)
class ReflectionProposal:
    """One proposed component replacement, understood structurally by upstream GEPA."""

    new_texts: dict[str, str]
    prompts: dict[str, Any] = field(default_factory=dict)
    raw_lm_outputs: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateMutation:
    """Validated component updates for one proposed candidate."""

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


@dataclass(frozen=True)
class CandidateMutations:
    """Ordered, non-empty, pickle-safe mutations from one informed turn."""

    items: tuple[CandidateMutation, ...]

    def __post_init__(self) -> None:
        try:
            normalized = tuple(self.items)
        except TypeError as exc:
            raise TypeError("mutations must be an iterable of CandidateMutation") from exc
        if not normalized:
            raise ValueError("mutations must not be empty")
        if not all(isinstance(item, CandidateMutation) for item in normalized):
            raise TypeError("mutations must contain only CandidateMutation values")
        object.__setattr__(self, "items", normalized)
        try:
            pickle.dumps(self)
        except Exception as exc:
            raise TypeError("mutations must be pickle-safe") from exc

    @classmethod
    def one(cls, mutation: CandidateMutation) -> CandidateMutations:
        return cls((mutation,))

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


class ReflectionDrive(Protocol):
    """Injected driver for one persistent Eggthreads conversation."""

    def start(
        self,
        conversation: "ReflectionConversation",
        request: Mapping[str, Any],
    ) -> CandidateMutation | CandidateMutations: ...

    def resume(
        self,
        conversation: "ReflectionConversation",
        request: Mapping[str, Any],
    ) -> CandidateMutation | CandidateMutations: ...


class ReflectionConversation:
    """The narrow persistence handle supplied to an injected reflection drive."""

    def __init__(self, db: ThreadsDB, thread_id: str, semantic_key: str) -> None:
        self.db = db
        self.thread_id = thread_id
        self.semantic_key = semantic_key
        self.response_message_id: str | None = None

    def append_assistant(
        self,
        content: str,
        mutations: CandidateMutation | CandidateMutations,
    ) -> str:
        """Persist an inspectable assistant answer plus typed mutations."""

        if self.response_message_id is not None:
            raise RuntimeError("reflection drive already appended an assistant response")
        if not isinstance(content, str):
            raise TypeError("assistant response content must be a string")
        normalized = _normalize_mutations(mutations)
        self.response_message_id = append_message(
            self.db,
            self.thread_id,
            "assistant",
            content,
            extra={
                "eggopt_kind": _REFLECTION_RESPONSE_KIND,
                "semantic_key": self.semantic_key,
                "mutations": [dict(item.updates) for item in normalized],
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
class _CachedReflectionTask(Task):
    semantic_key: str

    def get_cache_key(self) -> str:
        return self.semantic_key

    async def run(self) -> CandidateMutations:
        raise RuntimeError("completed reflection cache entry was not reused")


@dataclass
class _ReflectionTask(Task):
    semantic_key: str
    threads_db: ThreadsDB
    occurrence: ReflectionOccurrence
    request: dict[str, Any]
    drive: ReflectionDrive
    resume: bool
    fail_after_response: Callable[[], None] | None = None

    def get_cache_key(self) -> str:
        # Physical thread IDs, paths, and the live drive remain occurrence-only.
        return self.semantic_key

    async def run(self) -> CandidateMutations:
        persisted = _load_response(
            self.threads_db, self.occurrence.mutation_thread_id, self.semantic_key
        )
        if persisted is not None:
            return persisted[0]

        conversation = ReflectionConversation(
            self.threads_db,
            self.occurrence.mutation_thread_id,
            self.semantic_key,
        )
        recovery = getattr(self.drive, "continue_for_recovery", None)
        method = (
            recovery
            if self.resume and callable(recovery)
            else self.drive.resume
            if self.resume
            else self.drive.start
        )
        value = method(conversation, dict(self.request))
        if inspect.isawaitable(value):
            value = await value
        mutations = _validate_mutations(value, self.request)
        if conversation.response_message_id is None:
            raise RuntimeError(
                "reflection drive must persist its assistant response with "
                "conversation.append_assistant()"
            )
        persisted_mutations = _load_response_message(
            self.threads_db,
            self.occurrence.mutation_thread_id,
            conversation.response_message_id,
            self.semantic_key,
        )
        if persisted_mutations != mutations:
            raise ValueError("persisted assistant mutations differ from drive result")
        if self.fail_after_response is not None:
            self.fail_after_response()
        return mutations

    async def recover(self) -> bool:
        # run() reconciles a persisted typed response before invoking the drive.
        return True


class EggthreadsReflectionLM:
    """Stateful upstream ``ReflectionLM`` backed by Eggthreads and Eggflow."""

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
        reflection_instruction: str = _DEFAULT_REFLECTION_INSTRUCTION,
        fail_after_response: Callable[[], None] | None = None,
    ) -> None:
        """Bind a drive to a study; persist ``study_thread_id`` for restart."""

        if not isinstance(reflector_id, str) or not reflector_id:
            raise ValueError("reflector_id must be a non-empty string")
        if not isinstance(reflector_version, str) or not reflector_version:
            raise ValueError("reflector_version must be a non-empty string")
        if study_thread_id is not None and threads_db.get_thread(study_thread_id) is None:
            raise ValueError(f"study thread not found: {study_thread_id}")
        if (
            not isinstance(reflection_instruction, str)
            or not reflection_instruction.strip()
        ):
            raise ValueError("reflection_instruction must be a non-empty string")
        self.executor = executor
        self.threads_db = threads_db
        self.drive = drive
        self.reflector_id = reflector_id
        self.reflector_version = reflector_version
        self.reflection_instruction = reflection_instruction.strip()
        drive_identity = getattr(drive, "semantic_identity", {})
        self._reflector_config_json = canonical_json(
            {
                "reflector": reflector_config,
                "drive": drive_identity,
            },
            what="reflector_config",
        )
        if getattr(drive, "requires_study_thread", False) and study_thread_id is None:
            raise ValueError(
                "production reflection drive requires an explicit study_thread_id"
            )
        self.study_thread_id = study_thread_id or create_root_thread(
            threads_db, name=study_name
        )
        validate_study = getattr(drive, "validate_study", None)
        if callable(validate_study):
            validate_study(threads_db, self.study_thread_id)
        self.fail_after_response = fail_after_response

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        """Compatibility convenience for one mutation outside GEPA."""

        proposal, _next = self.reflect(
            candidate, reflective_dataset, components_to_update
        )
        return dict(proposal.new_texts)

    def semantic_key(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> str:
        job = (candidate, reflective_dataset, components_to_update)
        return self.semantic_key_many(candidate, [job])

    def occurrence(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> ReflectionOccurrence | None:
        job = (candidate, reflective_dataset, components_to_update)
        return self.occurrence_many(candidate, [job])

    def reflect(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
    ) -> tuple[ReflectionProposal, EggthreadsReflectionLM]:
        results = self.reflect_many(
            [(candidate, reflective_dataset, components_to_update)]
        )
        return results[0]

    def reflect_many(
        self,
        jobs: list[
            tuple[
                dict[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                list[str],
            ]
        ],
    ) -> list[tuple[ReflectionProposal, EggthreadsReflectionLM]]:
        return _run_sync(self.reflect_many_async(jobs))

    async def reflect_many_async(
        self,
        jobs: list[
            tuple[
                dict[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                list[str],
            ]
        ],
    ) -> list[tuple[ReflectionProposal, EggthreadsReflectionLM]]:
        if not jobs:
            return []
        outputs: list[tuple[ReflectionProposal, EggthreadsReflectionLM] | None] = [
            None for _ in jobs
        ]
        groups: dict[str, list[int]] = {}
        candidates: dict[str, dict[str, str]] = {}
        for index, (candidate, _dataset, _components) in enumerate(jobs):
            candidate_json = canonical_json(
                canonical_candidate(candidate), what="candidate"
            )
            groups.setdefault(candidate_json, []).append(index)
            candidates[candidate_json] = candidate

        for candidate_json, indices in groups.items():
            grouped_jobs = [jobs[index] for index in indices]
            mutations = await self._mutate_many_async(
                candidates[candidate_json], grouped_jobs
            )
            if len(mutations) != len(indices):
                raise ValueError(
                    "reflection drive returned "
                    f"{len(mutations)} mutations for {len(indices)} jobs"
                )
            for index, mutation in zip(indices, mutations, strict=True):
                outputs[index] = (
                    ReflectionProposal(new_texts=dict(mutation.updates)),
                    self,
                )
        if any(output is None for output in outputs):
            raise RuntimeError("reflection result mapping is incomplete")
        return [output for output in outputs if output is not None]

    def semantic_key_many(
        self,
        candidate: Mapping[str, str],
        jobs: Sequence[
            tuple[
                Mapping[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                Sequence[str],
            ]
        ],
    ) -> str:
        if not jobs:
            raise ValueError("reflection jobs must not be empty")
        ordered_jobs = []
        for job_candidate, dataset, components in jobs:
            if canonical_candidate(job_candidate) != canonical_candidate(candidate):
                raise ValueError("grouped reflection jobs must share one parent candidate")
            ordered_jobs.append(
                {
                    "candidate": canonical_candidate(job_candidate),
                    "reflective_dataset": json.loads(
                        canonical_json(dataset, what="reflective_dataset")
                    ),
                    "components_to_update": _validate_components(
                        job_candidate, components
                    ),
                }
            )
        return digest_payload(
            _REFLECTION_OPERATION,
            {
                "operation": _REFLECTION_OPERATION,
                "reflector": {
                    "id": self.reflector_id,
                    "version": self.reflector_version,
                    "config": self._reflector_config_json,
                },
                "instruction": self.reflection_instruction,
                "candidate": canonical_candidate(candidate),
                "jobs": ordered_jobs,
                "count": len(ordered_jobs),
            },
        )

    def occurrence_many(
        self,
        candidate: Mapping[str, str],
        jobs: Sequence[
            tuple[
                Mapping[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                Sequence[str],
            ]
        ],
    ) -> ReflectionOccurrence | None:
        return _find_occurrence(
            self.threads_db,
            self.study_thread_id,
            self.semantic_key_many(candidate, jobs),
        )

    def resume_uncommitted(self) -> CandidateMutations | None:
        """Resume the sole persisted request that lacks a typed response.

        Applications call this once before asking upstream GEPA to generate
        new work after a process interruption. It never appends a duplicate
        trigger and returns ``None`` when the study is clean.
        """

        pending = _uncommitted_occurrences(self.threads_db, self.study_thread_id)
        if not pending:
            return None
        if len(pending) != 1:
            raise RuntimeError("study has multiple uncommitted reflection requests")
        occurrence, request, key = pending[0]
        mutations = _run_sync(
            self.executor.run(
                _ReflectionTask(
                    semantic_key=key,
                    threads_db=self.threads_db,
                    occurrence=occurrence,
                    request=request,
                    drive=self.drive,
                    resume=_thread_has_prior_response(
                        self.threads_db, occurrence.mutation_thread_id
                    ),
                    fail_after_response=self.fail_after_response,
                )
            )
        )
        if not isinstance(mutations, CandidateMutations):
            raise TypeError("reflection task must return CandidateMutations")
        return mutations

    async def _mutate_many_async(
        self,
        candidate: Mapping[str, str],
        jobs: Sequence[
            tuple[
                Mapping[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                Sequence[str],
            ]
        ],
    ) -> CandidateMutations:
        request = self._request_many(candidate, jobs)
        key = self.semantic_key_many(candidate, jobs)
        occurrence = _find_occurrence(self.threads_db, self.study_thread_id, key)
        cached = self.executor.store.get(key)
        if occurrence is None and cached is not None and cached["status"] == "COMPLETED":
            mutations = await self.executor.run(_CachedReflectionTask(key))
        else:
            if occurrence is None:
                affinity_thread_id = _find_candidate_affinity(
                    self.threads_db, self.study_thread_id, candidate
                )
                if affinity_thread_id is None and _has_uncommitted_request(
                    self.threads_db, self.study_thread_id
                ):
                    raise RuntimeError(
                        "study has an uncommitted reflection request; resume it before "
                        "starting independent work"
                    )
                occurrence = _append_request(
                    self.threads_db,
                    self.study_thread_id,
                    key,
                    request,
                    affinity_thread_id=affinity_thread_id,
                )
            mutations = await self.executor.run(
                _ReflectionTask(
                    semantic_key=key,
                    threads_db=self.threads_db,
                    occurrence=occurrence,
                    request=request,
                    drive=self.drive,
                    resume=_thread_has_prior_response(
                        self.threads_db, occurrence.mutation_thread_id
                    ),
                    fail_after_response=self.fail_after_response,
                )
            )
        if not isinstance(mutations, CandidateMutations):
            raise TypeError("reflection task must return CandidateMutations")
        return mutations

    def _mutate_many(
        self,
        candidate: Mapping[str, str],
        jobs: Sequence[
            tuple[
                Mapping[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                Sequence[str],
            ]
        ],
    ) -> CandidateMutations:
        return _run_sync(self._mutate_many_async(candidate, jobs))

    def _request_many(
        self,
        candidate: Mapping[str, str],
        jobs: Sequence[
            tuple[
                Mapping[str, str],
                Mapping[str, Sequence[Mapping[str, Any]]],
                Sequence[str],
            ]
        ],
    ) -> dict[str, Any]:
        if not jobs:
            raise ValueError("reflection jobs must not be empty")
        job_records = []
        all_components: set[str] = set()
        for job_candidate, dataset, components in jobs:
            validated = _validate_components(job_candidate, components)
            all_components.update(validated)
            job_records.append(
                {
                    "reflective_dataset": json.loads(
                        canonical_json(dataset, what="reflective_dataset")
                    ),
                    "components_to_update": list(validated),
                }
            )
        return {
            "instruction": self.reflection_instruction,
            "candidate": dict(canonical_candidate(candidate)),
            "jobs": job_records,
            "components_to_update": sorted(all_components),
            "mutation_count": len(job_records),
        }


# Compatibility name for the first slice's public constructor.
EggthreadsCandidateProposer = EggthreadsReflectionLM


def _append_request(
    db: ThreadsDB,
    study_thread_id: str,
    semantic_key: str,
    request: Mapping[str, Any],
    *,
    affinity_thread_id: str | None,
) -> ReflectionOccurrence:
    if affinity_thread_id is None:
        iteration_number = len(list_children_with_meta(db, study_thread_id)) + 1
        iteration_id = create_child_thread(
            db, study_thread_id, name=f"Iteration {iteration_number:03d}"
        )
        mutation_id = create_child_thread(db, iteration_id, name="Mutation")
    else:
        mutation_id = affinity_thread_id
        iteration_id = _parent_thread_id(db, mutation_id)
        if iteration_id is None:
            raise ValueError(f"mutation thread has no iteration parent: {mutation_id}")
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
    for iteration_id, mutation_id in _mutation_threads(db, study_thread_id):
        request_id: str | None = None
        response_id: str | None = None
        for message in _projection(db, mutation_id).messages:
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


def _has_uncommitted_request(db: ThreadsDB, study_thread_id: str) -> bool:
    for _iteration_id, mutation_id in _mutation_threads(db, study_thread_id):
        pending: set[str] = set()
        for message in _projection(db, mutation_id).messages:
            payload = message.payload
            semantic_key = payload.get("semantic_key")
            if not isinstance(semantic_key, str):
                continue
            if payload.get("eggopt_kind") == _REFLECTION_REQUEST_KIND:
                pending.add(semantic_key)
            elif payload.get("eggopt_kind") == _REFLECTION_RESPONSE_KIND:
                pending.discard(semantic_key)
        if pending:
            return True
    return False


def _uncommitted_occurrences(
    db: ThreadsDB, study_thread_id: str
) -> list[tuple[ReflectionOccurrence, dict[str, Any], str]]:
    pending = []
    for iteration_id, mutation_id in _mutation_threads(db, study_thread_id):
        requests: dict[str, Any] = {}
        completed: set[str] = set()
        request_ids: dict[str, str] = {}
        for message in _projection(db, mutation_id).messages:
            payload = message.payload
            key = payload.get("semantic_key")
            if not isinstance(key, str):
                continue
            if payload.get("eggopt_kind") == _REFLECTION_REQUEST_KIND:
                request = payload.get("request")
                if isinstance(request, dict):
                    requests[key] = dict(request)
                    request_ids[key] = message.msg_id
            elif payload.get("eggopt_kind") == _REFLECTION_RESPONSE_KIND:
                completed.add(key)
        for key in requests.keys() - completed:
            pending.append(
                (
                    ReflectionOccurrence(
                        study_thread_id,
                        iteration_id,
                        mutation_id,
                        request_ids[key],
                    ),
                    requests[key],
                    key,
                )
            )
    return pending


def _find_candidate_affinity(
    db: ThreadsDB,
    study_thread_id: str,
    candidate: Mapping[str, str],
) -> str | None:
    target = dict(canonical_candidate(candidate))
    for _iteration_id, mutation_id in _mutation_threads(db, study_thread_id):
        for message in reversed(_projection(db, mutation_id).messages):
            for mutation in _response_mutations(message.payload):
                if mutation.updates == target:
                    return mutation_id
    return None


def _response_mutations(payload: Mapping[str, Any]) -> tuple[CandidateMutation, ...]:
    if payload.get("eggopt_kind") != _REFLECTION_RESPONSE_KIND:
        return ()
    raw_mutations = payload.get("mutations")
    if raw_mutations is None:
        # Read compatibility with response metadata written by Eggopt v0.1.0.
        raw_mutations = [payload.get("mutation")]
    if not isinstance(raw_mutations, list):
        return ()
    mutations: list[CandidateMutation] = []
    for raw in raw_mutations:
        try:
            mutations.append(CandidateMutation(raw))
        except (TypeError, ValueError):
            continue
    return tuple(mutations)


def _mutation_threads(db: ThreadsDB, study_thread_id: str):
    for iteration_id, _name, _recap, _created in list_children_with_meta(
        db, study_thread_id
    ):
        for mutation_id, _mname, _mrecap, _mcreated in list_children_with_meta(
            db, iteration_id
        ):
            yield iteration_id, mutation_id


def _parent_thread_id(db: ThreadsDB, thread_id: str) -> str | None:
    row = db.conn.execute(
        "SELECT parent_id FROM children WHERE child_id=?", (thread_id,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def _thread_has_prior_response(db: ThreadsDB, mutation_thread_id: str) -> bool:
    return any(
        message.payload.get("eggopt_kind") == _REFLECTION_RESPONSE_KIND
        for message in _projection(db, mutation_thread_id).messages
    )


def _load_response(
    db: ThreadsDB, mutation_thread_id: str, semantic_key: str
) -> tuple[CandidateMutations, str] | None:
    for message in reversed(_projection(db, mutation_thread_id).messages):
        payload = message.payload
        if (
            payload.get("eggopt_kind") == _REFLECTION_RESPONSE_KIND
            and payload.get("semantic_key") == semantic_key
        ):
            mutations = _response_mutations(payload)
            if not mutations:
                raise ValueError("assistant response has no valid mutations")
            return CandidateMutations(mutations), message.msg_id
    return None


def _load_response_message(
    db: ThreadsDB,
    mutation_thread_id: str,
    response_message_id: str,
    semantic_key: str,
) -> CandidateMutations:
    for message in _projection(db, mutation_thread_id).messages:
        if message.msg_id != response_message_id:
            continue
        payload = message.payload
        if (
            payload.get("eggopt_kind") != _REFLECTION_RESPONSE_KIND
            or payload.get("semantic_key") != semantic_key
        ):
            raise ValueError("assistant response lacks reflection authority metadata")
        mutations = _response_mutations(payload)
        if not mutations:
            raise ValueError("assistant response has no valid mutations")
        return CandidateMutations(mutations)
    raise ValueError("reflection drive response message was not persisted")


def _projection(db: ThreadsDB, thread_id: str):
    return load_thread_projection(db, thread_id, db.max_event_seq(thread_id))


def _normalize_mutations(
    value: CandidateMutation | CandidateMutations,
) -> CandidateMutations:
    if isinstance(value, CandidateMutations):
        return value
    if isinstance(value, CandidateMutation):
        return CandidateMutations.one(value)
    raise TypeError("reflection drive must return CandidateMutation(s)")


def _validate_mutations(
    value: Any, request: Mapping[str, Any]
) -> CandidateMutations:
    mutations = _normalize_mutations(value)
    expected_count = int(request["mutation_count"])
    if len(mutations) != expected_count:
        raise ValueError(
            f"reflection drive must return {expected_count} mutation(s), "
            f"got {len(mutations)}"
        )
    allowed = set(request["components_to_update"])
    for mutation in mutations:
        unexpected = set(mutation.updates) - allowed
        if unexpected:
            raise ValueError(
                f"mutation updated unrequested components: {sorted(unexpected)}"
            )
    return mutations


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
    instruction = request.get("instruction")
    if not isinstance(instruction, str) or not instruction:
        raise ValueError("reflection request requires an instruction")
    return instruction + "\n" + json.dumps(request, sort_keys=True, ensure_ascii=False)


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError(
        "Eggopt's synchronous GEPA reflector cannot run inside an active asyncio loop"
    )
