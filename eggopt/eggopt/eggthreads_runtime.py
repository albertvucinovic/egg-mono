"""Optional replay-safe hierarchical Eggthreads strategy runtime."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import pickle
from dataclasses import dataclass
from typing import Generic, TypeVar

from eggflow import Task, keyed
from eggthreads import (
    ThreadsDB,
    append_message,
    create_child_thread,
    create_root_thread,
    set_thread_tools_enabled,
)

from .core import (
    Advance,
    Candidate,
    CaseEvidence,
    Observation,
    Producer,
    Proposal,
    Stop,
    StrategyInput,
)
from .evaluation import CaseRequest
from .gepa import (
    GEPAState,
    _validate_gepa_input,
    _validate_gepa_options,
    build_gepa_decision,
)
from .repair import ItemFailure
from .runtime import (
    OperationContext,
    OperationInput,
    OperationResult,
    ProposalResult,
    StepResult,
    StrategyRunInput,
    StrategyRunResult,
)

StateT = TypeVar("StateT")
CaseT = TypeVar("CaseT")

_CREATE_THREAD_SCHEMA = b"eggopt.CreateRuntimeThread:v1\0"
_OPERATION_SCHEMA = b"eggopt.RuntimeOperationTask:v2\0"
_CASE_GROUP_SCHEMA = b"eggopt.RuntimeCaseGroupTask:v2\0"
_PROPOSAL_SCHEMA = b"eggopt.RuntimeProposalTask:v2\0"
_RUN_SCHEMA = b"eggopt.HierarchicalRuntimeTask:v2\0"

__all__ = [
    "ContextualGEPAStrategy",
    "HierarchicalRuntime",
    "HierarchicalRuntimeTask",
    "OperationTask",
]


@dataclass
class _CreateThread(Task):
    threads_db_path: str
    name: str
    parent_thread_id: str | None = None

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.name, "name")
        if self.parent_thread_id is not None:
            _validate_nonempty_string(
                self.parent_thread_id, "parent_thread_id"
            )

    def get_cache_key(self) -> str:
        return _cache_key(
            _CREATE_THREAD_SCHEMA,
            (self.threads_db_path, self.parent_thread_id, self.name),
        )

    def run(self) -> str:
        db = ThreadsDB(self.threads_db_path)
        try:
            db.init_schema()
            if self.parent_thread_id is None:
                thread_id = create_root_thread(db, name=self.name)
            else:
                thread_id = create_child_thread(
                    db, self.parent_thread_id, name=self.name
                )
            set_thread_tools_enabled(db, thread_id, False)
            return thread_id
        finally:
            db.conn.close()


@dataclass
class OperationTask(Task):
    """Run one audited contextual Producer in an authoritative thread."""

    threads_db_path: str
    parent_thread_id: str
    name: str
    producer: Producer
    producer_identity: str
    value: object
    thread_id: str | None = None
    operation_key: object | None = None

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.parent_thread_id, "parent_thread_id")
        _validate_nonempty_string(self.name, "name")
        _validate_producer(self.producer, "producer")
        _validate_nonempty_string(self.producer_identity, "producer_identity")
        if self.thread_id is not None:
            _validate_nonempty_string(self.thread_id, "thread_id")

    def get_cache_key(self) -> str:
        return _cache_key(
            _OPERATION_SCHEMA,
            (
                self.threads_db_path,
                self.parent_thread_id,
                self.name,
                self.producer_identity,
                _producer_cache_identity(self.producer),
                self.thread_id,
                self.operation_key,
                _pickle_digest(self.value, "operation value"),
            ),
        )

    def run(self):
        thread_id = self.thread_id
        if thread_id is None:
            thread_id = yield _CreateThread(
                self.threads_db_path, self.name, self.parent_thread_id
            )
        input_digest = _digest_hex(self.value, "operation value")
        _append_operation_audit(
            self.threads_db_path,
            thread_id,
            self.name,
            self.producer_identity,
            input_digest,
            outcome="started",
        )
        role_input = OperationInput(
            OperationContext(thread_id, self.name), self.value
        )
        try:
            result = self.producer.produce(role_input)
            if isinstance(result, Task) or inspect.iscoroutine(result):
                result = yield result
        except Exception as exc:
            _append_operation_audit(
                self.threads_db_path,
                thread_id,
                self.name,
                self.producer_identity,
                input_digest,
                outcome="failed",
                failure_type=type(exc).__name__,
            )
            raise
        _append_operation_audit(
            self.threads_db_path,
            thread_id,
            self.name,
            self.producer_identity,
            input_digest,
            outcome="item_failure"
            if isinstance(result, ItemFailure)
            else "succeeded",
            output_digest=_digest_hex(result, "operation result"),
        )
        return OperationResult(thread_id, result)


@dataclass
class _CaseGroupTask(Task, Generic[CaseT]):
    threads_db_path: str
    evaluation_thread_id: str
    candidate: Candidate
    cases: tuple[CaseT, ...]
    case_producer: Producer
    case_identity: str
    max_concurrent_cases: int

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(
            self.evaluation_thread_id, "evaluation_thread_id"
        )
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be a Candidate")
        self.cases = tuple(self.cases)
        _validate_producer(self.case_producer, "case_producer")
        _validate_nonempty_string(self.case_identity, "case_identity")
        _validate_positive_integer(
            self.max_concurrent_cases, "max_concurrent_cases"
        )

    def get_cache_key(self) -> str:
        return _cache_key(
            _CASE_GROUP_SCHEMA,
            (
                self.threads_db_path,
                self.evaluation_thread_id,
                self.case_identity,
                self.max_concurrent_cases,
                _pickle_digest((self.candidate, self.cases), "case group"),
            ),
        )

    async def run(
        self,
    ) -> tuple[OperationResult[CaseEvidence | ItemFailure], ...]:
        semaphore = asyncio.Semaphore(self.max_concurrent_cases)
        thread_ids = []
        for index in range(len(self.cases)):
            thread_id = await keyed(
                _CreateThread(
                    self.threads_db_path,
                    f"Case K{index:03d}",
                    self.evaluation_thread_id,
                ),
                "case",
                index,
            ).execute()
            thread_ids.append(thread_id)

        async def run_case(index: int, case: CaseT):
            async with semaphore:
                return await keyed(
                    OperationTask(
                        self.threads_db_path,
                        self.evaluation_thread_id,
                        f"Case K{index:03d}",
                        self.case_producer,
                        self.case_identity,
                        CaseRequest(self.candidate, case),
                        thread_ids[index],
                    ),
                    "case",
                    index,
                ).execute()

        results = await asyncio.gather(
            *(run_case(index, case) for index, case in enumerate(self.cases))
        )
        if not all(
            isinstance(result.value, (CaseEvidence, ItemFailure))
            for result in results
        ):
            raise TypeError(
                "case_producer must produce CaseEvidence or ItemFailure"
            )
        return tuple(results)


@dataclass
class _ProposalTask(Task, Generic[CaseT]):
    threads_db_path: str
    step_thread_id: str
    proposal_id: str
    proposal: Proposal
    cases: tuple[CaseT, ...]
    candidate_producer: Producer
    candidate_identity: str
    case_producer: Producer
    case_identity: str
    aggregate: Producer
    aggregate_identity: str
    max_concurrent_cases: int

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.step_thread_id, "step_thread_id")
        _validate_nonempty_string(self.proposal_id, "proposal_id")
        if not isinstance(self.proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        self.cases = tuple(self.cases)
        for producer_name, identity_name in (
            ("candidate_producer", "candidate_identity"),
            ("case_producer", "case_identity"),
            ("aggregate", "aggregate_identity"),
        ):
            _validate_producer(getattr(self, producer_name), producer_name)
            _validate_nonempty_string(
                getattr(self, identity_name), identity_name
            )
        _validate_positive_integer(
            self.max_concurrent_cases, "max_concurrent_cases"
        )

    def get_cache_key(self) -> str:
        return _cache_key(
            _PROPOSAL_SCHEMA,
            (
                self.threads_db_path,
                self.step_thread_id,
                self.proposal_id,
                self.candidate_identity,
                self.case_identity,
                self.aggregate_identity,
                self.max_concurrent_cases,
                _pickle_digest((self.proposal, self.cases), "proposal"),
            ),
        )

    def run(self):
        proposal_thread_id = yield _CreateThread(
            self.threads_db_path,
            f"Proposal {self.proposal_id}",
            self.step_thread_id,
        )
        production = yield keyed(
            OperationTask(
                self.threads_db_path,
                proposal_thread_id,
                "Production",
                self.candidate_producer,
                self.candidate_identity,
                self.proposal,
            ),
            self.proposal_id,
            "production",
        )
        if isinstance(production.value, ItemFailure):
            return ProposalResult(
                self.proposal_id,
                proposal_thread_id,
                None,
                self.proposal,
                production,
                (),
                None,
            )
        if not isinstance(production.value, Candidate):
            raise TypeError(
                "candidate_producer must produce Candidate or ItemFailure"
            )

        evaluation_thread_id = yield _CreateThread(
            self.threads_db_path, "Evaluation", proposal_thread_id
        )
        case_results = yield _CaseGroupTask(
            self.threads_db_path,
            evaluation_thread_id,
            production.value,
            self.cases,
            self.case_producer,
            self.case_identity,
            self.max_concurrent_cases,
        )
        if any(
            isinstance(result.value, ItemFailure) for result in case_results
        ):
            return ProposalResult(
                self.proposal_id,
                proposal_thread_id,
                evaluation_thread_id,
                self.proposal,
                production,
                case_results,
                None,
            )
        evidence = tuple(result.value for result in case_results)
        base = Observation(production.value, cases=evidence)
        aggregation = yield keyed(
            OperationTask(
                self.threads_db_path,
                evaluation_thread_id,
                "Aggregation",
                self.aggregate,
                self.aggregate_identity,
                base,
            ),
            self.proposal_id,
            "aggregation",
        )
        if not isinstance(aggregation.value, Observation):
            raise TypeError("aggregate must produce an Observation")
        if aggregation.value.candidate != production.value:
            raise ValueError("aggregate must preserve the produced candidate")
        if aggregation.value.cases != evidence:
            raise ValueError(
                "aggregate must preserve all case evidence in order"
            )
        return ProposalResult(
            self.proposal_id,
            proposal_thread_id,
            evaluation_thread_id,
            self.proposal,
            production,
            case_results,
            aggregation,
        )


@dataclass
class HierarchicalRuntimeTask(Task, Generic[StateT, CaseT]):
    """One durable run of Eggopt's exact hierarchical runtime."""

    threads_db_path: str
    strategy: Producer
    strategy_identity: str
    candidate_producer: Producer
    candidate_identity: str
    case_producer: Producer
    case_identity: str
    aggregate: Producer
    aggregate_identity: str
    value: StrategyRunInput[StateT, CaseT]
    setup: Producer | None = None
    setup_identity: str | None = None
    setup_name: str = "Setup"

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        for producer_name, identity_name in (
            ("strategy", "strategy_identity"),
            ("candidate_producer", "candidate_identity"),
            ("case_producer", "case_identity"),
            ("aggregate", "aggregate_identity"),
        ):
            _validate_producer(getattr(self, producer_name), producer_name)
            _validate_nonempty_string(
                getattr(self, identity_name), identity_name
            )
        if not isinstance(self.value, StrategyRunInput):
            raise TypeError("value must be a StrategyRunInput")
        _validate_setup(self.setup, self.setup_identity, self.setup_name)

    def get_cache_key(self) -> str:
        return _cache_key(
            _RUN_SCHEMA,
            (
                self.threads_db_path,
                self.strategy_identity,
                self.candidate_identity,
                self.case_identity,
                self.aggregate_identity,
                self.setup_identity,
                self.setup_name if self.setup is not None else None,
                _pickle_digest(self.value, "StrategyRunInput"),
            ),
        )

    def run(self):
        study_thread_id = yield _CreateThread(
            self.threads_db_path, "StudyRoot"
        )
        strategy_thread_id = yield _CreateThread(
            self.threads_db_path, "StrategyRunRoot", study_thread_id
        )
        run_setup_thread_id = yield _CreateThread(
            self.threads_db_path, "RunSetup", strategy_thread_id
        )
        effective_value = self.value
        if self.setup is not None:
            setup_result = yield OperationTask(
                self.threads_db_path,
                run_setup_thread_id,
                self.setup_name,
                self.setup,
                self.setup_identity,
                self.value,
                operation_key=(self.setup_identity, self.setup_name),
            )
            if not isinstance(setup_result.value, StrategyRunInput):
                raise TypeError("setup must produce a StrategyRunInput")
            effective_value = setup_result.value

        state = effective_value.state
        observations: tuple[Observation, ...] = ()
        step_results: list[StepResult[StateT]] = []
        proposal_number = 0

        seed_step_thread_id = yield _CreateThread(
            self.threads_db_path, "Step S000", strategy_thread_id
        )
        seed_result = yield _ProposalTask(
            self.threads_db_path,
            seed_step_thread_id,
            "P000",
            Proposal(parents=(effective_value.seed,), instruction="seed"),
            effective_value.cases,
            self.candidate_producer,
            self.candidate_identity,
            self.case_producer,
            self.case_identity,
            self.aggregate,
            self.aggregate_identity,
            effective_value.max_concurrent_cases,
        )
        step_results.append(
            StepResult("S000", seed_step_thread_id, None, state, (seed_result,))
        )
        observations = _successful_observations((seed_result,))
        proposal_number = 1

        for step_number in range(1, effective_value.max_steps + 1):
            step_id = f"S{step_number:03d}"
            step_thread_id = yield _CreateThread(
                self.threads_db_path, f"Step {step_id}", strategy_thread_id
            )
            transition = yield keyed(
                OperationTask(
                    self.threads_db_path,
                    step_thread_id,
                    "StrategyTransition",
                    self.strategy,
                    self.strategy_identity,
                    StrategyInput(state, observations),
                ),
                step_id,
                "strategy",
            )
            decision = transition.value
            if not isinstance(decision, (Advance, Stop)):
                raise TypeError("strategy must produce Advance or Stop")
            state = decision.state
            if isinstance(decision, Stop):
                step_results.append(
                    StepResult(
                        step_id,
                        step_thread_id,
                        transition,
                        state,
                        (),
                        decision.reason,
                    )
                )
                break

            proposal_results = []
            for proposal in decision.proposals:
                proposal_id = f"P{proposal_number:03d}"
                proposal_result = yield _ProposalTask(
                    self.threads_db_path,
                    step_thread_id,
                    proposal_id,
                    proposal,
                    effective_value.cases,
                    self.candidate_producer,
                    self.candidate_identity,
                    self.case_producer,
                    self.case_identity,
                    self.aggregate,
                    self.aggregate_identity,
                    effective_value.max_concurrent_cases,
                )
                proposal_results.append(proposal_result)
                proposal_number += 1
            observations = _successful_observations(proposal_results)
            step_results.append(
                StepResult(
                    step_id,
                    step_thread_id,
                    transition,
                    state,
                    tuple(proposal_results),
                )
            )

        return StrategyRunResult(
            study_thread_id,
            strategy_thread_id,
            run_setup_thread_id,
            tuple(step_results),
            state,
        )


@dataclass(frozen=True)
class ContextualGEPAStrategy:
    """Run GEPA selectors as operation children of StrategyTransition."""

    threads_db_path: str
    select_parents: Producer[
        OperationInput[tuple[Observation, ...]], tuple[Observation, ...] | Task
    ]
    parent_identity: str
    select_evidence: Producer[
        OperationInput[Observation], tuple[CaseEvidence, ...] | Task
    ]
    evidence_identity: str
    instruction: str = "Revise the candidate using the selected evidence."
    proposals_per_parent: int = 1

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_producer(self.select_parents, "select_parents")
        _validate_nonempty_string(self.parent_identity, "parent_identity")
        _validate_producer(self.select_evidence, "select_evidence")
        _validate_nonempty_string(self.evidence_identity, "evidence_identity")
        _validate_gepa_options(self.instruction, self.proposals_per_parent)

    def cache_identity(self) -> tuple[object, ...]:
        """Return selector/config identity for an enclosing OperationTask."""

        return (
            self.parent_identity,
            self.evidence_identity,
            self.instruction,
            self.proposals_per_parent,
        )

    def produce(
        self, operation: OperationInput[StrategyInput[GEPAState]]
    ) -> Task | Stop[GEPAState]:
        if not isinstance(operation, OperationInput):
            raise TypeError("operation must be an OperationInput")
        value = operation.value
        if not isinstance(operation.context, OperationContext):
            raise TypeError("operation context must be an OperationContext")
        stop = _validate_gepa_input(value)
        if stop is not None:
            return stop
        return _ContextualGEPATask(
            self.threads_db_path,
            operation.context.thread_id,
            value,
            self.select_parents,
            self.parent_identity,
            self.select_evidence,
            self.evidence_identity,
            self.instruction,
            self.proposals_per_parent,
        )


@dataclass
class _ContextualGEPATask(Task):
    threads_db_path: str
    transition_thread_id: str
    value: StrategyInput[GEPAState]
    select_parents: Producer
    parent_identity: str
    select_evidence: Producer
    evidence_identity: str
    instruction: str
    proposals_per_parent: int

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(
            self.transition_thread_id, "transition_thread_id"
        )
        _validate_gepa_input(self.value)
        _validate_producer(self.select_parents, "select_parents")
        _validate_nonempty_string(self.parent_identity, "parent_identity")
        _validate_producer(self.select_evidence, "select_evidence")
        _validate_nonempty_string(
            self.evidence_identity, "evidence_identity"
        )
        _validate_gepa_options(self.instruction, self.proposals_per_parent)

    def get_cache_key(self) -> str:
        return _cache_key(
            b"eggopt.ContextualGEPATask:v1\0",
            (
                self.threads_db_path,
                self.transition_thread_id,
                self.parent_identity,
                self.evidence_identity,
                self.instruction,
                self.proposals_per_parent,
                _pickle_digest(self.value, "GEPA StrategyInput"),
            ),
        )

    def run(self):
        parents_result = yield OperationTask(
            self.threads_db_path,
            self.transition_thread_id,
            "ParentSelection",
            self.select_parents,
            self.parent_identity,
            self.value.observations,
        )
        try:
            parents = tuple(parents_result.value)
        except TypeError as exc:
            raise TypeError("selected parents must be an iterable") from exc
        if not all(isinstance(parent, Observation) for parent in parents):
            raise TypeError(
                "selected parents must contain only Observation"
            )
        if not parents:
            return build_gepa_decision(
                self.value,
                (),
                (),
                instruction=self.instruction,
                proposals_per_parent=self.proposals_per_parent,
            )

        evidence_by_parent = []
        for index, parent in enumerate(parents):
            result = yield keyed(
                OperationTask(
                    self.threads_db_path,
                    self.transition_thread_id,
                    f"EvidenceSelection {index:03d}",
                    self.select_evidence,
                    self.evidence_identity,
                    parent,
                ),
                "evidence",
                index,
            )
            try:
                evidence_by_parent.append(tuple(result.value))
            except TypeError as exc:
                raise TypeError("selected evidence must be an iterable") from exc
        return build_gepa_decision(
            self.value,
            parents,
            tuple(evidence_by_parent),
            instruction=self.instruction,
            proposals_per_parent=self.proposals_per_parent,
        )


@dataclass(frozen=True)
class HierarchicalRuntime(Generic[StateT, CaseT]):
    """Injectable ``Producer[StrategyRunInput, Task]`` hierarchy runtime."""

    threads_db_path: str
    strategy: Producer
    strategy_identity: str
    candidate_producer: Producer
    candidate_identity: str
    case_producer: Producer
    case_identity: str
    aggregate: Producer
    aggregate_identity: str
    setup: Producer | None = None
    setup_identity: str | None = None
    setup_name: str = "Setup"

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        for producer_name, identity_name in (
            ("strategy", "strategy_identity"),
            ("candidate_producer", "candidate_identity"),
            ("case_producer", "case_identity"),
            ("aggregate", "aggregate_identity"),
        ):
            _validate_producer(getattr(self, producer_name), producer_name)
            _validate_nonempty_string(
                getattr(self, identity_name), identity_name
            )
        _validate_setup(self.setup, self.setup_identity, self.setup_name)

    def produce(
        self, value: StrategyRunInput[StateT, CaseT]
    ) -> HierarchicalRuntimeTask[StateT, CaseT]:
        if not isinstance(value, StrategyRunInput):
            raise TypeError("value must be a StrategyRunInput")
        return HierarchicalRuntimeTask(
            self.threads_db_path,
            self.strategy,
            self.strategy_identity,
            self.candidate_producer,
            self.candidate_identity,
            self.case_producer,
            self.case_identity,
            self.aggregate,
            self.aggregate_identity,
            value,
            self.setup,
            self.setup_identity,
            self.setup_name,
        )


def _successful_observations(
    proposals: tuple[ProposalResult, ...] | list[ProposalResult],
) -> tuple[Observation, ...]:
    return tuple(
        proposal.aggregation.value
        for proposal in proposals
        if proposal.aggregation is not None
    )


def _append_operation_audit(
    threads_db_path: str,
    thread_id: str,
    semantic_name: str,
    producer_identity: str,
    input_digest: str,
    *,
    outcome: str,
    output_digest: str | None = None,
    failure_type: str | None = None,
) -> None:
    fields = [
        "eggopt.operation",
        f"name={semantic_name}",
        f"producer={producer_identity}",
        f"input_sha256={input_digest}",
        f"outcome={outcome}",
    ]
    if output_digest is not None:
        fields.append(f"output_sha256={output_digest}")
    if failure_type is not None:
        fields.append(f"failure_type={failure_type}")
    db = ThreadsDB(threads_db_path)
    try:
        db.init_schema()
        append_message(
            db,
            thread_id,
            role="system",
            content=" ".join(fields),
            extra={
                "no_api": True,
                "keep_user_turn": True,
                "origin": "eggopt.operation.audit",
                "eggopt_operation_audit": True,
            },
        )
    finally:
        db.conn.close()


def _digest_hex(value: object, name: str) -> str:
    try:
        encoded = pickle.dumps(value, protocol=5)
    except Exception as exc:
        raise TypeError(f"{name} must be pickleable for audit identity") from exc
    return hashlib.sha256(encoded).hexdigest()


def _producer_cache_identity(producer: object) -> object:
    identity = getattr(producer, "cache_identity", None)
    if identity is None:
        return None
    if not callable(identity):
        raise TypeError("producer cache_identity must be callable")
    return identity()


def _cache_key(schema: bytes, values: object) -> str:
    try:
        encoded = pickle.dumps(values, protocol=5)
    except Exception as exc:
        raise TypeError("runtime cache key values must be pickleable") from exc
    return hashlib.sha256(schema + encoded).hexdigest()


def _pickle_digest(value: object, name: str) -> bytes:
    try:
        encoded = pickle.dumps(value, protocol=5)
    except Exception as exc:
        raise TypeError(f"{name} must be pickleable for cache identity") from exc
    return hashlib.sha256(encoded).digest()


def _validate_setup(
    setup: object, setup_identity: object, setup_name: object
) -> None:
    if setup is None:
        if setup_identity is not None:
            raise ValueError("setup_identity requires setup")
        return
    _validate_producer(setup, "setup")
    _validate_nonempty_string(setup_identity, "setup_identity")
    _validate_nonempty_string(setup_name, "setup_name")


def _validate_producer(value: object, name: str) -> None:
    if not isinstance(value, Producer):
        raise TypeError(f"{name} must implement Producer")


def _validate_nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _validate_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")
