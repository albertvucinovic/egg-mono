import asyncio
import json
from dataclasses import dataclass
from pickle import dumps, loads

import pytest
from eggflow import FlowExecutor, Task, TaskStore
from eggthreads import ThreadsDB, get_parent, get_thread_tools_config

from eggopt import Producer
from eggopt.eggthreads import (
    CreateProducerThread,
    CreateRunRoots,
    RunRoots,
    RunThreadProducer,
    ThreadInput,
    ThreadOutput,
    ThreadProducer,
    ThreadProducerSpec,
)
import eggopt.eggthreads as eggthreads_adapter


@dataclass
class CountingDrive:
    prefix: str = "answer"
    calls: int = 0

    def produce(self, value: ThreadInput) -> str:
        self.calls += 1
        return f"{self.prefix}: {value.prompt}"


def _thread_count(db_path) -> int:
    db = ThreadsDB(db_path)
    try:
        db.init_schema()
        return db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    finally:
        db.conn.close()


def _local_tools_enabled(db: ThreadsDB, thread_id: str) -> list[bool]:
    values = []
    for event in db.events_since(thread_id, -1):
        if event["type"] != "tools.config":
            continue
        payload = json.loads(event["payload_json"])
        if "llm_tools_enabled" in payload:
            values.append(payload["llm_tools_enabled"])
    return values


def _messages(db: ThreadsDB, thread_id: str) -> list[tuple[str, str]]:
    messages = []
    for event in db.events_since(thread_id, -1):
        if event["type"] != "msg.create":
            continue
        payload = json.loads(event["payload_json"])
        messages.append((payload["role"], payload["content"]))
    return messages


def test_creation_tasks_do_not_use_thread_listing_scans(
    tmp_path, monkeypatch
) -> None:
    def fail_scan(*args, **kwargs):
        raise AssertionError("thread listing scan must not be used")

    for name in (
        "list_threads",
        "list_root_threads",
        "list_children_ids",
        "list_children_with_meta",
    ):
        monkeypatch.setattr(
            eggthreads_adapter, name, fail_scan, raising=False
        )

    threads_path = tmp_path / "threads.sqlite"
    roots = CreateRunRoots(str(threads_path), "study", "strategy").run()
    thread_id = CreateProducerThread(
        str(threads_path), roots.strategy_thread_id, ThreadProducerSpec("leaf")
    ).run()

    assert roots.study_thread_id
    assert thread_id
    assert _thread_count(threads_path) == 3


def test_run_roots_are_cached_authoritative_ancestry(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    flow_path = tmp_path / "flow.db"
    task = CreateRunRoots(str(threads_path), "StudyRoot", "StrategyRunRoot")

    first_store = TaskStore(str(flow_path))
    try:
        first_roots = asyncio.run(FlowExecutor(first_store).run(task))
    finally:
        first_store.conn.close()
    count_after_first = _thread_count(threads_path)

    second_store = TaskStore(str(flow_path))
    try:
        second_roots = asyncio.run(
            FlowExecutor(second_store).run(
                CreateRunRoots(
                    str(threads_path), "StudyRoot", "StrategyRunRoot"
                )
            )
        )
    finally:
        second_store.conn.close()

    assert isinstance(first_roots, RunRoots)
    assert isinstance(task, CreateRunRoots)
    assert second_roots == first_roots
    assert loads(dumps(first_roots)) == first_roots
    assert count_after_first == 2
    assert _thread_count(threads_path) == 2

    db = ThreadsDB(threads_path)
    try:
        db.init_schema()
        study = db.get_thread(first_roots.study_thread_id)
        strategy = db.get_thread(first_roots.strategy_thread_id)
        assert study.name == "StudyRoot"
        assert strategy.name == "StrategyRunRoot"
        assert get_parent(db, first_roots.study_thread_id) is None
        assert get_parent(db, first_roots.strategy_thread_id) == study.thread_id
        assert not get_thread_tools_config(db, study.thread_id).llm_tools_enabled
        assert not get_thread_tools_config(
            db, strategy.thread_id
        ).llm_tools_enabled
        assert _local_tools_enabled(db, study.thread_id)[-1] is False
        assert _local_tools_enabled(db, strategy.thread_id)[-1] is False
    finally:
        db.conn.close()


def test_thread_producer_records_configured_child_and_transcript(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    flow_path = tmp_path / "flow.db"
    store = TaskStore(str(flow_path))
    try:
        roots = asyncio.run(
            FlowExecutor(store).run(
                CreateRunRoots(str(threads_path), "study", "strategy")
            )
        )
    finally:
        store.conn.close()

    spec = ThreadProducerSpec(
        name="restricted leaf",
        system_prompt="Use only supplied context.",
        tools_enabled=True,
    )
    drive = CountingDrive()
    producer = ThreadProducer(
        str(threads_path), roots.strategy_thread_id, spec, drive, "fake:v1"
    )
    store = TaskStore(str(flow_path))
    try:
        output = asyncio.run(
            FlowExecutor(store).run(producer.produce(ThreadInput("solve case")))
        )
    finally:
        store.conn.close()

    assert isinstance(producer, Producer)
    assert isinstance(producer.produce(ThreadInput("other")), RunThreadProducer)
    assert isinstance(producer.produce(ThreadInput("other")), Task)
    assert output.content == "answer: solve case"
    assert drive.calls == 1

    db = ThreadsDB(threads_path)
    try:
        db.init_schema()
        child = db.get_thread(output.thread_id)
        assert child.name == "restricted leaf"
        assert get_parent(db, output.thread_id) == roots.strategy_thread_id
        assert _local_tools_enabled(db, output.thread_id)[-1] is True
        assert not get_thread_tools_config(db, output.thread_id).llm_tools_enabled
        assert _messages(db, output.thread_id) == [
            ("system", "Use only supplied context."),
            ("user", "solve case"),
            ("assistant", "answer: solve case"),
        ]
    finally:
        db.conn.close()


def test_second_executor_reuses_output_without_child_or_drive_call(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    flow_path = tmp_path / "flow.db"
    roots_store = TaskStore(str(flow_path))
    try:
        roots = asyncio.run(
            FlowExecutor(roots_store).run(
                CreateRunRoots(str(threads_path), "study", "strategy")
            )
        )
    finally:
        roots_store.conn.close()

    spec = ThreadProducerSpec("leaf")
    value = ThreadInput("question")
    first_drive = CountingDrive()
    first_store = TaskStore(str(flow_path))
    try:
        first_output = asyncio.run(
            FlowExecutor(first_store).run(
                ThreadProducer(
                    str(threads_path),
                    roots.strategy_thread_id,
                    spec,
                    first_drive,
                    "fake:v1",
                ).produce(value)
            )
        )
    finally:
        first_store.conn.close()
    count_after_first = _thread_count(threads_path)

    second_drive = CountingDrive()
    second_store = TaskStore(str(flow_path))
    try:
        second_output = asyncio.run(
            FlowExecutor(second_store).run(
                ThreadProducer(
                    str(threads_path),
                    roots.strategy_thread_id,
                    spec,
                    second_drive,
                    "fake:v1",
                ).produce(value)
            )
        )
    finally:
        second_store.conn.close()

    assert isinstance(first_output, ThreadOutput)
    assert second_output == first_output
    assert loads(dumps(second_output)) == second_output
    assert first_drive.calls == 1
    assert second_drive.calls == 0
    assert _thread_count(threads_path) == count_after_first

    db = ThreadsDB(threads_path)
    try:
        db.init_schema()
        assert _messages(db, second_output.thread_id) == [
            ("user", "question"),
            ("assistant", "answer: question"),
        ]
    finally:
        db.conn.close()


def test_thread_task_keys_cover_spec_input_and_drive_identity() -> None:
    drive = CountingDrive()
    base = ThreadProducer(
        "threads.sqlite", "parent", ThreadProducerSpec("leaf"), drive, "fake:v1"
    ).produce(ThreadInput("first"))
    changed_spec = ThreadProducer(
        "threads.sqlite",
        "parent",
        ThreadProducerSpec("other"),
        drive,
        "fake:v1",
    ).produce(ThreadInput("first"))
    changed_input = ThreadProducer(
        "threads.sqlite", "parent", ThreadProducerSpec("leaf"), drive, "fake:v1"
    ).produce(ThreadInput("second"))
    changed_drive = ThreadProducer(
        "threads.sqlite", "parent", ThreadProducerSpec("leaf"), drive, "fake:v2"
    ).produce(ThreadInput("first"))
    changed_parent = ThreadProducer(
        "threads.sqlite", "other-parent", ThreadProducerSpec("leaf"), drive, "fake:v1"
    ).produce(ThreadInput("first"))
    changed_db = ThreadProducer(
        "other.sqlite", "parent", ThreadProducerSpec("leaf"), drive, "fake:v1"
    ).produce(ThreadInput("first"))

    assert base.get_cache_key() != changed_spec.get_cache_key()
    assert base.get_cache_key() != changed_input.get_cache_key()
    assert base.get_cache_key() != changed_drive.get_cache_key()
    assert base.get_cache_key() != changed_parent.get_cache_key()
    assert base.get_cache_key() != changed_db.get_cache_key()
    assert CreateProducerThread(
        "threads.sqlite", "parent", ThreadProducerSpec("leaf")
    ).get_cache_key() != CreateProducerThread(
        "threads.sqlite", "parent", ThreadProducerSpec("other")
    ).get_cache_key()
    assert CreateRunRoots("a.sqlite", "study", "strategy").get_cache_key() != (
        CreateRunRoots("b.sqlite", "study", "strategy").get_cache_key()
    )
    assert CreateRunRoots("a.sqlite", "study", "strategy").get_cache_key() != (
        CreateRunRoots("a.sqlite", "other-study", "strategy").get_cache_key()
    )
    assert CreateRunRoots("a.sqlite", "study", "strategy").get_cache_key() != (
        CreateRunRoots("a.sqlite", "study", "other-strategy").get_cache_key()
    )


def test_specs_refs_and_validation_are_pickle_safe() -> None:
    values = (
        RunRoots("study", "strategy"),
        ThreadProducerSpec("leaf", "system", "model", False),
        ThreadInput(""),
        ThreadOutput("thread", ""),
    )
    assert loads(dumps(values)) == values

    with pytest.raises(ValueError, match="study_thread_id must not be empty"):
        RunRoots("", "strategy")
    with pytest.raises(ValueError, match="strategy_thread_id must not be empty"):
        RunRoots("study", "")
    with pytest.raises(ValueError, match="name must not be empty"):
        ThreadProducerSpec("")
    with pytest.raises(TypeError, match="system_prompt must be a string"):
        ThreadProducerSpec("leaf", system_prompt=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="model_key must be a string or None"):
        ThreadProducerSpec("leaf", model_key=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tools_enabled must be a bool"):
        ThreadProducerSpec("leaf", tools_enabled=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="prompt must be a string"):
        ThreadInput(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="threads_db_path must not be empty"):
        CreateRunRoots("", "study", "strategy")
    with pytest.raises(ValueError, match="parent_thread_id must not be empty"):
        CreateProducerThread("threads.sqlite", "", ThreadProducerSpec("leaf"))
    with pytest.raises(ValueError, match="thread_id must not be empty"):
        ThreadOutput("", "content")
    with pytest.raises(TypeError, match="drive must implement Producer"):
        ThreadProducer(
            "threads.sqlite",
            "parent",
            ThreadProducerSpec("leaf"),
            object(),  # type: ignore[arg-type]
            "fake:v1",
        )
    with pytest.raises(ValueError, match="drive_identity must not be empty"):
        ThreadProducer(
            "threads.sqlite",
            "parent",
            ThreadProducerSpec("leaf"),
            CountingDrive(),
            "",
        )
    producer = ThreadProducer(
        "threads.sqlite",
        "parent",
        ThreadProducerSpec("leaf"),
        CountingDrive(),
        "fake:v1",
    )
    with pytest.raises(TypeError, match="value must be a ThreadInput"):
        producer.produce("prompt")  # type: ignore[arg-type]


def test_run_thread_rejects_non_string_drive_output() -> None:
    drive = FunctionDrive()
    task = RunThreadProducer(
        "threads.sqlite",
        "parent",
        ThreadProducerSpec("leaf"),
        drive,
        "bad:v1",
        ThreadInput("prompt"),
    )
    generator = task.run()
    next(generator)
    with pytest.raises(TypeError, match="drive must produce a string"):
        generator.send("thread-id")


@dataclass
class FunctionDrive:
    def produce(self, value: ThreadInput):
        return object()
