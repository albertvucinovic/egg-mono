"""Optional Eggthreads hierarchy and fake leaf Producer substrate."""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass

from eggflow import Task
from eggthreads import (
    ThreadsDB,
    append_message,
    create_child_thread,
    create_root_thread,
    set_thread_tools_enabled,
)

from .core import Producer

_ROOTS_SCHEMA = b"eggopt.CreateRunRoots:v1\0"
_PRODUCER_THREAD_SCHEMA = b"eggopt.CreateProducerThread:v1\0"
_RUN_PRODUCER_SCHEMA = b"eggopt.RunThreadProducer:v1\0"

__all__ = [
    "CreateProducerThread",
    "CreateRunRoots",
    "RunRoots",
    "RunThreadProducer",
    "ThreadInput",
    "ThreadOutput",
    "ThreadProducer",
    "ThreadProducerSpec",
]


@dataclass(frozen=True)
class RunRoots:
    """Authoritative cached references to one study and strategy hierarchy."""

    study_thread_id: str
    strategy_thread_id: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.study_thread_id, "study_thread_id")
        _validate_nonempty_string(self.strategy_thread_id, "strategy_thread_id")


@dataclass(frozen=True)
class ThreadProducerSpec:
    """Pickle-safe configuration for one restricted Producer child thread."""

    name: str
    system_prompt: str = ""
    model_key: str | None = None
    tools_enabled: bool = False

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.name, "name")
        if not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string")
        if self.model_key is not None and not isinstance(self.model_key, str):
            raise TypeError("model_key must be a string or None")
        if not isinstance(self.tools_enabled, bool):
            raise TypeError("tools_enabled must be a bool")


@dataclass(frozen=True)
class ThreadInput:
    """Typed text input for a process-local thread-backed Producer."""

    prompt: str

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")


@dataclass(frozen=True)
class ThreadOutput:
    """Typed cached output and its authoritative Producer child reference."""

    thread_id: str
    content: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.thread_id, "thread_id")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")


@dataclass
class CreateRunRoots(Task):
    """Create one study root and strategy child without scanning by name."""

    threads_db_path: str
    study_name: str
    strategy_name: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.study_name, "study_name")
        _validate_nonempty_string(self.strategy_name, "strategy_name")

    def get_cache_key(self) -> str:
        return _cache_key(
            _ROOTS_SCHEMA,
            (self.threads_db_path, self.study_name, self.strategy_name),
        )

    def run(self) -> RunRoots:
        db = ThreadsDB(self.threads_db_path)
        try:
            db.init_schema()
            study_thread_id = create_root_thread(db, name=self.study_name)
            set_thread_tools_enabled(db, study_thread_id, False)
            strategy_thread_id = create_child_thread(
                db, study_thread_id, name=self.strategy_name
            )
            set_thread_tools_enabled(db, strategy_thread_id, False)
            return RunRoots(study_thread_id, strategy_thread_id)
        finally:
            db.conn.close()


@dataclass
class CreateProducerThread(Task):
    """Create one configured child under an authoritative cached parent ID."""

    threads_db_path: str
    parent_thread_id: str
    spec: ThreadProducerSpec

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.parent_thread_id, "parent_thread_id")
        if not isinstance(self.spec, ThreadProducerSpec):
            raise TypeError("spec must be a ThreadProducerSpec")

    def get_cache_key(self) -> str:
        return _cache_key(
            _PRODUCER_THREAD_SCHEMA,
            (self.threads_db_path, self.parent_thread_id, self.spec),
        )

    def run(self) -> str:
        db = ThreadsDB(self.threads_db_path)
        try:
            db.init_schema()
            thread_id = create_child_thread(
                db,
                self.parent_thread_id,
                name=self.spec.name,
                initial_model_key=self.spec.model_key,
            )
            if self.spec.system_prompt:
                append_message(
                    db, thread_id, role="system", content=self.spec.system_prompt
                )
            set_thread_tools_enabled(db, thread_id, self.spec.tools_enabled)
            return thread_id
        finally:
            db.conn.close()


@dataclass
class RunThreadProducer(Task):
    """Create/reuse a child, drive fake production, and record its transcript."""

    threads_db_path: str
    parent_thread_id: str
    spec: ThreadProducerSpec
    drive: Producer[ThreadInput, str]
    drive_identity: str
    value: ThreadInput

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.parent_thread_id, "parent_thread_id")
        if not isinstance(self.spec, ThreadProducerSpec):
            raise TypeError("spec must be a ThreadProducerSpec")
        _validate_producer(self.drive, "drive")
        _validate_nonempty_string(self.drive_identity, "drive_identity")
        if not isinstance(self.value, ThreadInput):
            raise TypeError("value must be a ThreadInput")

    def get_cache_key(self) -> str:
        return _cache_key(
            _RUN_PRODUCER_SCHEMA,
            (
                self.threads_db_path,
                self.parent_thread_id,
                self.spec,
                self.drive_identity,
                self.value,
            ),
        )

    def run(self):
        thread_id = yield CreateProducerThread(
            self.threads_db_path, self.parent_thread_id, self.spec
        )
        content = self.drive.produce(self.value)
        if not isinstance(content, str):
            raise TypeError("drive must produce a string")

        db = ThreadsDB(self.threads_db_path)
        try:
            db.init_schema()
            append_message(db, thread_id, role="user", content=self.value.prompt)
            append_message(db, thread_id, role="assistant", content=content)
        finally:
            db.conn.close()
        return ThreadOutput(thread_id=thread_id, content=content)


@dataclass(frozen=True)
class ThreadProducer:
    """Process-local fake leaf adapter that returns durable Eggflow tasks."""

    threads_db_path: str
    parent_thread_id: str
    spec: ThreadProducerSpec
    drive: Producer[ThreadInput, str]
    drive_identity: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.threads_db_path, "threads_db_path")
        _validate_nonempty_string(self.parent_thread_id, "parent_thread_id")
        if not isinstance(self.spec, ThreadProducerSpec):
            raise TypeError("spec must be a ThreadProducerSpec")
        _validate_producer(self.drive, "drive")
        _validate_nonempty_string(self.drive_identity, "drive_identity")

    def produce(self, value: ThreadInput) -> RunThreadProducer:
        if not isinstance(value, ThreadInput):
            raise TypeError("value must be a ThreadInput")
        return RunThreadProducer(
            threads_db_path=self.threads_db_path,
            parent_thread_id=self.parent_thread_id,
            spec=self.spec,
            drive=self.drive,
            drive_identity=self.drive_identity,
            value=value,
        )


def _cache_key(schema: bytes, values: tuple[object, ...]) -> str:
    try:
        serialized = pickle.dumps(values, protocol=5)
    except Exception as exc:
        raise TypeError("task cache-key values must be pickleable") from exc
    return hashlib.sha256(schema + serialized).hexdigest()


def _validate_nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _validate_producer(value: object, name: str) -> None:
    if not isinstance(value, Producer):
        raise TypeError(f"{name} must implement Producer")
