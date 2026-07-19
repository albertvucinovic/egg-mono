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
    StrategyDecision,
    StrategyInput,
)
from .evaluation import CaseRequest
from .runtime import (
    OperationResult,
    ProposalResult,
    StepResult,
    StrategyRunInput,
    StrategyRunResult,
)

StateT = TypeVar("StateT")
CaseT = TypeVar("CaseT")

_CREATE_THREAD_SCHEMA = b"eggopt.CreateRuntimeThread:v1\0"
_OPERATION_SCHEMA = b"eggopt.RuntimeOperationTask:v1\0"
_CASE_GROUP_SCHEMA = b"eggopt.RuntimeCaseGroupTask:v1\0"
_PROPOSAL_SCHEMA = b"eggopt.RuntimeProposalTask:v1\0"
_RUN_SCHEMA = b"eggopt.HierarchicalRuntimeTask:v1\0"

__all__ = ["HierarchicalRuntime", "HierarchicalRuntimeTask"]


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
class _OperationTask(Task):
    threads_db_path: str
    parent_thread_id: str
    name: str
    producer: Producer
    producer_identity: str
    value: object
    thread_id: str | None = None

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
                self.thread_id,
                _pickle_digest(self.value, "operation value"),
            ),
        )

    def run(self):
        thread_id = self.thread_id
        if thread_id is None:
            thread_id = yield _CreateThread(
                self.threads_db_path, self.name, self.parent_thread_id
            )
        result = self.producer.produce(self.value)
        if isinstance(result, Task) or inspect.iscoroutine(result):
            result = yield result
        return OperationResult(thread_id, result)


@dataclass
class _RunCaseOperation(Task, Generic[CaseT]):
    threads_db_path: str
    evaluation_thread_id: str
    thread_id: str
    index: int
    candidate: Candidate
    case: CaseT
    case_producer: Producer
    case_identity: str

    def get_cache_key(self) -> str:
        return _cache_key(
            _CASE_GROUP_SCHEMA,
            (
                self.threads_db_path,
                self.evaluation_thread_id,
                self.thread_id,
                self.index,
                self.case_identity,
                _pickle_digest((self.candidate, self.case), "case operation"),
            ),
        )

    def run(self):
        result = self.case_producer.produce(
            CaseRequest(self.candidate, self.case)
        )
        if isinstance(result, Task) or inspect.iscoroutine(result):
            result = yield result
        if not isinstance(result, CaseEvidence):
            raise TypeError("case_producer must produce CaseEvidence values")
        return OperationResult(self.thread_id, result)


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

    async def run(self) -> tuple[OperationResult[CaseEvidence], ...]:
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
                    _RunCaseOperation(
                        self.threads_db_path,
                        self.evaluation_thread_id,
                        thread_ids[index],
                        index,
                        self.candidate,
                        case,
                        self.case_producer,
                        self.case_identity,
                    ),
                    "case",
                    index,
                ).execute()

        results = await asyncio.gather(
            *(run_case(index, case) for index, case in enumerate(self.cases))
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
            _OperationTask(
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
        if not isinstance(production.value, Candidate):
            raise TypeError("candidate_producer must produce a Candidate")

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
        evidence = tuple(result.value for result in case_results)
        base = Observation(production.value, cases=evidence)
        aggregation = yield keyed(
            _OperationTask(
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

    def get_cache_key(self) -> str:
        return _cache_key(
            _RUN_SCHEMA,
            (
                self.threads_db_path,
                self.strategy_identity,
                self.candidate_identity,
                self.case_identity,
                self.aggregate_identity,
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

        state = self.value.state
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
            Proposal(parents=(self.value.seed,), instruction="seed"),
            self.value.cases,
            self.candidate_producer,
            self.candidate_identity,
            self.case_producer,
            self.case_identity,
            self.aggregate,
            self.aggregate_identity,
            self.value.max_concurrent_cases,
        )
        step_results.append(
            StepResult("S000", seed_step_thread_id, None, state, (seed_result,))
        )
        observations = (seed_result.aggregation.value,)
        proposal_number = 1

        for step_number in range(1, self.value.max_steps + 1):
            step_id = f"S{step_number:03d}"
            step_thread_id = yield _CreateThread(
                self.threads_db_path, f"Step {step_id}", strategy_thread_id
            )
            transition = yield keyed(
                _OperationTask(
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
                    self.value.cases,
                    self.candidate_producer,
                    self.candidate_identity,
                    self.case_producer,
                    self.case_identity,
                    self.aggregate,
                    self.aggregate_identity,
                    self.value.max_concurrent_cases,
                )
                proposal_results.append(proposal_result)
                proposal_number += 1
            observations = tuple(
                result.aggregation.value for result in proposal_results
            )
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
        )


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
