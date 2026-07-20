from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from eggflow import FlowExecutor, TaskError, TaskStore
from eggthreads import (
    ThreadsDB,
    list_children_with_meta,
    load_thread_projection,
)
from gepa import GEPAResult
from gepa.utils.stop_condition import MaxTrackedCandidatesStopper

from eggopt.gepa import (
    CandidateMutation,
    EggflowGEPAAdapter,
    EggthreadsCandidateProposer,
    ExampleEvaluation,
    ReflectionEvidence,
    optimize_with_egg,
)


@dataclass(frozen=True)
class Example:
    example_id: str
    target_level: int


class CountingEvaluator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, candidate, example: Example) -> ExampleEvaluation[str]:
        level_text = candidate["instruction"]
        level = int(level_text)
        self.calls.append((level_text, example.example_id))
        score = float(level >= example.target_level)
        return ExampleEvaluation(
            output=level_text,
            score=score,
            evidence=ReflectionEvidence(
                inputs={
                    "example_id": example.example_id,
                    "target_level": str(example.target_level),
                },
                generated_outputs=level_text,
                feedback=f"reach level {example.target_level}",
            ),
            objective_scores={"accuracy": score},
        )


class DeterministicDrive:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, conversation, request) -> CandidateMutation:
        self.calls += 1
        next_level = int(request["candidate"]["instruction"]) + 1
        mutation = CandidateMutation({"instruction": str(next_level)})
        # This text intentionally advertises the wrong candidate. The structured
        # CandidateMutation metadata/result, not transcript scanning, is authority.
        conversation.append_assistant("I would choose instruction=999", mutation)
        return mutation


class QuietLogger:
    def log(self, *args, **kwargs) -> None:
        del args, kwargs


def make_runtime(tmp_path: Path, *, flow_name="flow.db", threads_name="threads.db"):
    flow_path = tmp_path / flow_name
    thread_path = tmp_path / threads_name
    store = TaskStore(str(flow_path))
    executor = FlowExecutor(store)
    threads = ThreadsDB(thread_path)
    threads.init_schema()
    return flow_path, store, executor, threads


def make_adapter(executor, evaluator, *, config=None):
    return EggflowGEPAAdapter(
        executor,
        evaluator=evaluator,
        evaluator_id="tests.level-evaluator",
        evaluator_version="1",
        evaluator_config=config or {"metric": "threshold"},
        example_id=lambda example: example.example_id,
    )


def make_proposer(executor, threads, drive, **kwargs):
    return EggthreadsCandidateProposer(
        executor,
        threads,
        drive=drive,
        reflector_id="tests.level-reflector",
        reflector_version="1",
        reflector_config={"policy": "increment"},
        **kwargs,
    )


def test_upstream_gepa_keeps_candidates_lineage_and_specialists(tmp_path):
    _, _, executor, threads = make_runtime(tmp_path)
    evaluator = CountingEvaluator()
    drive = DeterministicDrive()
    adapter = make_adapter(executor, evaluator)
    proposer = make_proposer(executor, threads, drive)
    examples = [Example("easy", 1), Example("hard", 2)]

    result = optimize_with_egg(
        seed_candidate={"instruction": "0"},
        trainset=examples,
        valset=examples,
        adapter=adapter,
        proposer=proposer,
        stop_callbacks=MaxTrackedCandidatesStopper(3),
        reflection_minibatch_size=2,
        skip_perfect_score=False,
        frontier_type="hybrid",
        seed=17,
        logger=QuietLogger(),
    )

    assert isinstance(result, GEPAResult)
    assert result.seed == 17
    assert result.candidates == [
        {"instruction": "0"},
        {"instruction": "1"},
        {"instruction": "2"},
    ]
    assert result.parents == [[None], [0], [1]]
    assert result.val_aggregate_scores == [0.0, 0.5, 1.0]
    assert result.per_val_instance_best_candidates == {0: {1, 2}, 1: {2}}
    assert result.per_objective_best_candidates == {"accuracy": {2}}
    assert result.best_outputs_valset == {0: [(1, "1"), (2, "2")], 1: [(2, "2")]}
    assert drive.calls == 2

    iterations = list_children_with_meta(threads, proposer.study_thread_id)
    assert [name for _, name, _, _ in iterations] == ["Iteration 001", "Iteration 002"]
    for iteration_id, _, _, _ in iterations:
        mutations = list_children_with_meta(threads, iteration_id)
        assert [name for _, name, _, _ in mutations] == ["Mutation"]
        mutation_id = mutations[0][0]
        messages = load_thread_projection(
            threads, mutation_id, threads.max_event_seq(mutation_id)
        ).messages
        assistant = [message for message in messages if message.payload.get("role") == "assistant"]
        assert any(message.payload.get("eggopt_kind") for message in assistant)
        assert any(
            message.payload.get("content") == "I would choose instruction=999"
            for message in assistant
        )


def test_evaluations_replay_in_a_new_process_at_a_different_path(tmp_path):
    source = tmp_path / "source"
    replay = tmp_path / "different-layout" / "replay"
    source.mkdir()
    replay.mkdir(parents=True)
    flow_path, store, executor, _ = make_runtime(source)
    evaluator = CountingEvaluator()
    adapter = make_adapter(executor, evaluator)
    candidate = {"instruction": "1"}
    examples = [Example("easy", 1), Example("hard", 2)]

    first = adapter.evaluate(examples, candidate, capture_traces=True)
    keys = [adapter.semantic_key(candidate, example).digest() for example in examples]
    assert first.num_metric_calls == 2
    assert len(evaluator.calls) == 2
    store.conn.close()
    replay_db = replay / "renamed-cache.sqlite"
    shutil.copy2(flow_path, replay_db)

    sentinel = replay / "evaluator-called"
    script = replay / "replay.py"
    script.write_text(
        """
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from eggflow import FlowExecutor, TaskStore
from eggopt.gepa import EggflowGEPAAdapter

@dataclass(frozen=True)
class Example:
    example_id: str
    target_level: int

class MustNotRun:
    def __init__(self, sentinel):
        self.calls = 0
        self.sentinel = sentinel

    def __call__(self, candidate, example):
        self.calls += 1
        self.sentinel.write_text(f"{candidate!r} {example!r}")
        raise AssertionError("cached evaluator executed after process restart")

evaluator = MustNotRun(Path(sys.argv[2]))
adapter = EggflowGEPAAdapter(
    FlowExecutor(TaskStore(sys.argv[1])),
    evaluator=evaluator,
    evaluator_id="tests.level-evaluator",
    evaluator_version="1",
    evaluator_config={"metric": "threshold"},
    example_id=lambda example: example.example_id,
)
candidate = {"instruction": "1"}
examples = [Example("easy", 1), Example("hard", 2)]
result = adapter.evaluate(examples, candidate, capture_traces=True)
print(json.dumps({
    "calls": evaluator.calls,
    "keys": [adapter.semantic_key(candidate, item).digest() for item in examples],
    "metric_calls": result.num_metric_calls,
    "outputs": result.outputs,
    "scores": result.scores,
    "evidence_inputs": [dict(item.inputs) for item in result.trajectories],
    "objective_scores": result.objective_scores,
}, sort_keys=True))
"""
    )
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (package_root, existing_pythonpath) if path
    )
    completed = subprocess.run(
        [sys.executable, str(script), str(replay_db), str(sentinel)],
        check=True,
        capture_output=True,
        text=True,
        cwd=replay,
        env=env,
    )
    replayed = json.loads(completed.stdout)
    assert not sentinel.exists()
    assert replayed == {
        "calls": 0,
        "evidence_inputs": [
            {"example_id": "easy", "target_level": "1"},
            {"example_id": "hard", "target_level": "2"},
        ],
        "keys": keys,
        "metric_calls": 0,
        "objective_scores": [{"accuracy": 1.0}, {"accuracy": 0.0}],
        "outputs": first.outputs,
        "scores": first.scores,
    }

    # Paths and live resources differ, while equal semantic inputs do not.
    _, _, other_executor, _ = make_runtime(
        replay, flow_name="empty.sqlite", threads_name="other.sqlite"
    )
    other_adapter = make_adapter(other_executor, CountingEvaluator())
    assert other_adapter.semantic_key(candidate, examples[0]).digest() == keys[0]
    changed = make_adapter(
        other_executor, CountingEvaluator(), config={"metric": "changed"}
    )
    assert changed.semantic_key(candidate, examples[0]).digest() != keys[0]


def test_drive_transcript_is_not_result_authority(tmp_path):
    _, _, executor, threads = make_runtime(tmp_path)

    class TextOnlyDrive:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, conversation, request):
            del request
            self.calls += 1
            # Neither this transcript nor its thread label can authorize a result.
            from eggthreads import append_message

            append_message(conversation.db, conversation.thread_id, "assistant", "instruction=42")
            return CandidateMutation({"instruction": "1"})

    drive = TextOnlyDrive()
    proposer = make_proposer(executor, threads, drive, study_name="instruction=42")
    dataset = {"instruction": [{"Feedback": "improve"}]}

    with pytest.raises(TaskError, match="must persist its assistant response"):
        proposer({"instruction": "0"}, dataset, ["instruction"])
    assert drive.calls == 1


def test_recovery_reuses_persisted_assistant_mutation(tmp_path):
    _, store, executor, threads = make_runtime(tmp_path)
    drive = DeterministicDrive()
    fail_once = True

    def fail_after_response() -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("injected failure before Eggflow commit")

    proposer = make_proposer(
        executor,
        threads,
        drive,
        fail_after_response=fail_after_response,
    )
    candidate = {"instruction": "0"}
    dataset = {
        "instruction": [
            {
                "Inputs": {"example_id": "easy"},
                "Generated Outputs": "0",
                "Feedback": "reach level 1",
                "Score": 0.0,
            }
        ]
    }

    with pytest.raises(TaskError, match="injected failure"):
        proposer(candidate, dataset, ["instruction"])
    assert drive.calls == 1
    occurrence = proposer.occurrence(candidate, dataset, ["instruction"])
    assert occurrence is not None and occurrence.response_message_id is not None
    row = store.get(proposer.semantic_key(candidate, dataset, ["instruction"]))
    assert row is not None and row["status"] == "FAILED"

    # Retry crosses Eggflow's real FAILED -> recover -> run boundary. run() finds
    # the structured assistant result and neither continues nor drives again.
    assert proposer(candidate, dataset, ["instruction"]) == {"instruction": "1"}
    assert drive.calls == 1
    assert proposer.occurrence(candidate, dataset, ["instruction"]) == occurrence
    completed = store.get(proposer.semantic_key(candidate, dataset, ["instruction"]))
    assert completed is not None and completed["status"] == "COMPLETED"
