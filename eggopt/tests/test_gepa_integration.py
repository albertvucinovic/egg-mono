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
from gepa.strategies.proposal_sampling import SameParentSampling
from gepa.utils.stop_condition import (
    MaxCandidateProposalsStopper,
    MaxTrackedCandidatesStopper,
)

from eggopt.gepa import (
    CandidateMutation,
    CandidateMutations,
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
        self.start_calls = 0
        self.resume_calls = 0
        self.thread_ids: list[str] = []
        self.context_roles: list[list[str]] = []

    @property
    def calls(self) -> int:
        return self.start_calls + self.resume_calls

    def start(self, conversation, request) -> CandidateMutation:
        self.start_calls += 1
        return self._respond(conversation, request)

    def resume(self, conversation, request) -> CandidateMutation:
        self.resume_calls += 1
        return self._respond(conversation, request)

    def _respond(self, conversation, request) -> CandidateMutation:
        self.thread_ids.append(conversation.thread_id)
        projection = load_thread_projection(
            conversation.db,
            conversation.thread_id,
            conversation.db.max_event_seq(conversation.thread_id),
        )
        self.context_roles.append(
            [message.payload.get("role") for message in projection.messages]
        )
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
    assert [name for _, name, _, _ in iterations] == ["Iteration 001"]
    mutations = list_children_with_meta(threads, iterations[0][0])
    assert [name for _, name, _, _ in mutations] == ["Mutation"]
    mutation_id = mutations[0][0]
    messages = load_thread_projection(
        threads, mutation_id, threads.max_event_seq(mutation_id)
    ).messages
    assistant = [
        message for message in messages if message.payload.get("role") == "assistant"
    ]
    assert len(assistant) == 2
    assert all(message.payload.get("eggopt_kind") for message in assistant)
    assert all(
        message.payload.get("content") == "I would choose instruction=999"
        for message in assistant
    )
    assert drive.thread_ids == [mutation_id, mutation_id]
    assert drive.start_calls == 1
    assert drive.resume_calls == 1


def test_singular_reflector_uses_upstream_reflection_strategy(monkeypatch, tmp_path):
    from eggopt.gepa import runner

    captured = {}

    def fake_optimize(**kwargs):
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(runner, "optimize", fake_optimize)
    _, _, executor, threads = make_runtime(tmp_path)
    reflector = make_proposer(executor, threads, DeterministicDrive())
    adapter = make_adapter(executor, CountingEvaluator())

    assert optimize_with_egg(
        seed_candidate={"instruction": "0"},
        trainset=[Example("one", 1)],
        adapter=adapter,
        proposer=reflector,
        max_metric_calls=1,
    ) == "result"
    assert captured["reflection_strategy"] is reflector
    assert "custom_candidate_proposer" not in captured


def test_candidate_mutations_are_non_empty_ordered_and_pickle_safe():
    import pickle

    mutations = CandidateMutations(
        (
            CandidateMutation({"instruction": "first"}),
            CandidateMutation({"instruction": "second"}),
        )
    )
    assert [item.updates["instruction"] for item in mutations] == [
        "first",
        "second",
    ]
    assert pickle.loads(pickle.dumps(mutations)) == mutations
    assert CandidateMutations.one(
        CandidateMutation({"instruction": "only"})
    ).items[0].updates == {"instruction": "only"}
    with pytest.raises(ValueError, match="must not be empty"):
        CandidateMutations(())


def test_same_parent_sampling_uses_one_plural_turn_and_upstream_selection(
    tmp_path,
):
    @dataclass(frozen=True)
    class SpecialistExample:
        example_id: str

    class SpecialistEvaluator:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        def __call__(self, candidate, example):
            level = int(candidate["instruction"])
            self.calls.append((level, example.example_id))
            scores = {
                "left": {0: 0.0, 1: 0.5, 2: 1.0},
                "right": {0: 0.0, 1: 1.0, 2: 0.5},
            }
            score = scores[example.example_id][level]
            return ExampleEvaluation(
                output=str(level),
                score=score,
                evidence=ReflectionEvidence(
                    inputs={"example_id": example.example_id},
                    generated_outputs=str(level),
                    feedback="become the matching specialist",
                ),
            )

    class PluralDrive:
        def __init__(self) -> None:
            self.start_calls = 0
            self.resume_calls = 0
            self.thread_ids: list[str] = []

        def start(self, conversation, request):
            self.start_calls += 1
            self.thread_ids.append(conversation.thread_id)
            assert request["mutation_count"] == 2
            mutations = CandidateMutations(
                (
                    CandidateMutation({"instruction": "1"}),
                    CandidateMutation({"instruction": "2"}),
                )
            )
            conversation.append_assistant("Two specialists", mutations)
            return mutations

        def resume(self, conversation, request):
            self.resume_calls += 1
            return self.start(conversation, request)

    _, _, executor, threads = make_runtime(tmp_path)
    evaluator = SpecialistEvaluator()
    drive = PluralDrive()
    adapter = make_adapter(executor, evaluator, config={"metric": "specialist"})
    reflector = make_proposer(executor, threads, drive)
    examples = [SpecialistExample("left"), SpecialistExample("right")]

    result = optimize_with_egg(
        seed_candidate={"instruction": "0"},
        trainset=examples,
        valset=examples,
        adapter=adapter,
        proposer=reflector,
        sampling_strategy=SameParentSampling(2),
        stop_callbacks=MaxCandidateProposalsStopper(1),
        reflection_minibatch_size=1,
        skip_perfect_score=False,
        seed=1,
        logger=QuietLogger(),
    )

    assert result.candidates == [
        {"instruction": "0"},
        {"instruction": "1"},
        {"instruction": "2"},
    ]
    assert result.parents == [[None], [0], [0]]
    assert result.val_aggregate_scores == [0.0, 0.75, 0.75]
    assert result.per_val_instance_best_candidates == {0: {2}, 1: {1}}
    assert drive.start_calls == 1
    assert drive.resume_calls == 0
    assert {level for level, _example_id in evaluator.calls} == {0, 1, 2}

    iterations = list_children_with_meta(threads, reflector.study_thread_id)
    assert len(iterations) == 1
    mutations = list_children_with_meta(threads, iterations[0][0])
    assert len(mutations) == 1
    assert drive.thread_ids == [mutations[0][0]]
    messages = load_thread_projection(
        threads, mutations[0][0], threads.max_event_seq(mutations[0][0])
    ).messages
    response = next(
        message
        for message in messages
        if message.payload.get("eggopt_kind")
        == "eggopt.gepa.reflection-response.v1"
    )
    assert response.payload["mutations"] == [
        {"instruction": "1"},
        {"instruction": "2"},
    ]
    assert "mutation" not in response.payload


def test_plural_turn_cache_replay_and_each_child_keeps_affinity(tmp_path):
    class PluralDrive(DeterministicDrive):
        def start(self, conversation, request):
            self.start_calls += 1
            self.thread_ids.append(conversation.thread_id)
            self.context_roles.append(_roles(conversation))
            count = request["mutation_count"]
            mutations = CandidateMutations(
                tuple(
                    CandidateMutation({"instruction": str(index + 1)})
                    for index in range(count)
                )
            )
            conversation.append_assistant("Plural mutation", mutations)
            return mutations

        def resume(self, conversation, request):
            self.resume_calls += 1
            self.thread_ids.append(conversation.thread_id)
            self.context_roles.append(_roles(conversation))
            level = int(request["candidate"]["instruction"]) + 10
            mutation = CandidateMutation({"instruction": str(level)})
            conversation.append_assistant("Follow-up", mutation)
            return mutation

    def _roles(conversation):
        projection = load_thread_projection(
            conversation.db,
            conversation.thread_id,
            conversation.db.max_event_seq(conversation.thread_id),
        )
        return [message.payload.get("role") for message in projection.messages]

    flow_path, store, executor, threads = make_runtime(tmp_path)
    first_drive = PluralDrive()
    first = make_proposer(executor, threads, first_drive)
    candidate = {"instruction": "0"}
    jobs = [
        (candidate, {"instruction": [{"Feedback": "left"}]}, ["instruction"]),
        (candidate, {"instruction": [{"Feedback": "right"}]}, ["instruction"]),
    ]

    proposals = first.reflect_many(jobs)
    assert [proposal.new_texts for proposal, _next in proposals] == [
        {"instruction": "1"},
        {"instruction": "2"},
    ]
    occurrence = first.occurrence_many(candidate, jobs)
    assert occurrence is not None
    assert first_drive.start_calls == 1
    store.conn.close()

    fresh_store = TaskStore(str(flow_path))
    replay_drive = PluralDrive()
    replay = make_proposer(FlowExecutor(fresh_store), threads, replay_drive)
    replayed = replay.reflect_many(jobs)
    assert [proposal.new_texts for proposal, _next in replayed] == [
        {"instruction": "1"},
        {"instruction": "2"},
    ]
    assert replay_drive.calls == 0

    # Each child persisted in the plural response resolves to its producing
    # conversation when later evidence arrives.
    lineage_drive = PluralDrive()
    lineage = make_proposer(
        FlowExecutor(fresh_store),
        threads,
        lineage_drive,
        study_thread_id=first.study_thread_id,
    )
    for child in ({"instruction": "1"}, {"instruction": "2"}):
        proposal, _next = lineage.reflect(
            child,
            {"instruction": [{"Feedback": f"critique {child['instruction']}"}]},
            ["instruction"],
        )
        assert proposal.new_texts == {
            "instruction": str(int(child["instruction"]) + 10)
        }

    assert lineage_drive.resume_calls == 2
    assert lineage_drive.thread_ids == [
        occurrence.mutation_thread_id,
        occurrence.mutation_thread_id,
    ]
    assert lineage_drive.context_roles[0] == ["user", "assistant", "user"]
    assert lineage_drive.context_roles[1] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    iterations = list_children_with_meta(threads, first.study_thread_id)
    assert len(iterations) == 1
    assert len(list_children_with_meta(threads, iterations[0][0])) == 1


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


def test_identical_reflection_reuses_committed_result_with_fresh_proposer(tmp_path):
    flow_path, store, executor, threads = make_runtime(tmp_path)
    first_drive = DeterministicDrive()
    first = make_proposer(executor, threads, first_drive)
    candidate = {"instruction": "0"}
    dataset = {"instruction": [{"Feedback": "reach level 1"}]}

    assert first(candidate, dataset, ["instruction"]) == {"instruction": "1"}
    first_occurrence = first.occurrence(candidate, dataset, ["instruction"])
    assert first_occurrence is not None
    store.conn.close()

    fresh_store = TaskStore(str(flow_path))
    fresh_drive = DeterministicDrive()
    # This different physical study is deliberately empty. The semantic
    # Eggflow result remains reusable without consulting or driving a thread.
    fresh = make_proposer(FlowExecutor(fresh_store), threads, fresh_drive)
    assert fresh.study_thread_id != first.study_thread_id

    assert fresh(candidate, dataset, ["instruction"]) == {"instruction": "1"}
    assert fresh_drive.calls == 0
    assert list_children_with_meta(threads, fresh.study_thread_id) == []


def test_new_turn_for_mutated_candidate_reuses_lineage_conversation(tmp_path):
    _, _, executor, threads = make_runtime(tmp_path)
    drive = DeterministicDrive()
    first = make_proposer(executor, threads, drive)
    first_dataset = {"instruction": [{"Feedback": "reach level 1"}]}

    assert first(
        {"instruction": "0"}, first_dataset, ["instruction"]
    ) == {"instruction": "1"}
    first_occurrence = first.occurrence(
        {"instruction": "0"}, first_dataset, ["instruction"]
    )
    assert first_occurrence is not None

    # A fresh runtime object binds by the authoritative study id. New evidence
    # for candidate B is a new semantic turn in B's producing conversation.
    restarted_drive = DeterministicDrive()
    restarted = make_proposer(
        executor,
        threads,
        restarted_drive,
        study_thread_id=first.study_thread_id,
    )
    second_dataset = {"instruction": [{"Feedback": "now reach level 2"}]}
    assert restarted(
        {"instruction": "1"}, second_dataset, ["instruction"]
    ) == {"instruction": "2"}
    second_occurrence = restarted.occurrence(
        {"instruction": "1"}, second_dataset, ["instruction"]
    )

    assert second_occurrence is not None
    assert second_occurrence.mutation_thread_id == first_occurrence.mutation_thread_id
    assert restarted_drive.thread_ids == [first_occurrence.mutation_thread_id]
    assert restarted_drive.start_calls == 0
    assert restarted_drive.resume_calls == 1
    assert restarted_drive.context_roles == [["user", "assistant", "user"]]
    iterations = list_children_with_meta(threads, first.study_thread_id)
    assert len(iterations) == 1
    assert len(list_children_with_meta(threads, iterations[0][0])) == 1
    messages = load_thread_projection(
        threads,
        first_occurrence.mutation_thread_id,
        threads.max_event_seq(first_occurrence.mutation_thread_id),
    ).messages
    assert [message.payload.get("role") for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[0].payload["request"]["candidate"] == {"instruction": "0"}
    assert messages[2].payload["request"]["candidate"] == {"instruction": "1"}


def test_interrupted_request_reuses_exact_thread_without_duplicate_trigger(tmp_path):
    _, store, executor, threads = make_runtime(tmp_path)

    class InterruptedDrive(DeterministicDrive):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        def start(self, conversation, request):
            self.start_calls += 1
            self.thread_ids.append(conversation.thread_id)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("interrupted before typed response")
            return self._respond(conversation, request)

    first_drive = InterruptedDrive()
    first = make_proposer(executor, threads, first_drive)
    candidate = {"instruction": "0"}
    dataset = {"instruction": [{"Feedback": "reach level 1"}]}

    with pytest.raises(TaskError, match="interrupted before typed response"):
        first(candidate, dataset, ["instruction"])
    occurrence = first.occurrence(candidate, dataset, ["instruction"])
    assert occurrence is not None and occurrence.response_message_id is None
    key = first.semantic_key(candidate, dataset, ["instruction"])
    assert store.get(key)["status"] == "FAILED"

    restarted_drive = DeterministicDrive()
    restarted = make_proposer(
        executor,
        threads,
        restarted_drive,
        study_thread_id=first.study_thread_id,
    )
    assert restarted(candidate, dataset, ["instruction"]) == {"instruction": "1"}
    recovered = restarted.occurrence(candidate, dataset, ["instruction"])
    assert recovered is not None
    assert recovered.mutation_thread_id == occurrence.mutation_thread_id
    assert restarted_drive.thread_ids == [occurrence.mutation_thread_id]
    assert restarted_drive.start_calls == 1
    assert restarted_drive.resume_calls == 0

    iterations = list_children_with_meta(threads, first.study_thread_id)
    assert len(iterations) == 1
    assert len(list_children_with_meta(threads, iterations[0][0])) == 1
    messages = load_thread_projection(
        threads,
        occurrence.mutation_thread_id,
        threads.max_event_seq(occurrence.mutation_thread_id),
    ).messages
    requests = [
        message
        for message in messages
        if message.payload.get("eggopt_kind")
        == "eggopt.gepa.reflection-request.v1"
    ]
    assert [message.msg_id for message in requests] == [occurrence.request_message_id]


def test_drive_transcript_is_not_result_authority(tmp_path):
    _, _, executor, threads = make_runtime(tmp_path)

    class TextOnlyDrive:
        def __init__(self) -> None:
            self.calls = 0

        def start(self, conversation, request):
            del request
            self.calls += 1
            # Neither this transcript nor its thread label can authorize a result.
            from eggthreads import append_message

            append_message(
                conversation.db,
                conversation.thread_id,
                "assistant",
                "instruction=42",
            )
            return CandidateMutation({"instruction": "1"})

        def resume(self, conversation, request):
            return self.start(conversation, request)

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

    # A fresh proposer crosses Eggflow's real FAILED -> recover -> run boundary.
    # run() finds the structured assistant result and does not drive again.
    restarted_drive = DeterministicDrive()
    restarted = make_proposer(
        executor,
        threads,
        restarted_drive,
        study_thread_id=proposer.study_thread_id,
    )
    assert restarted(candidate, dataset, ["instruction"]) == {"instruction": "1"}
    assert drive.calls == 1
    assert restarted_drive.calls == 0
    assert restarted.occurrence(candidate, dataset, ["instruction"]) == occurrence
    completed = store.get(restarted.semantic_key(candidate, dataset, ["instruction"]))
    assert completed is not None and completed["status"] == "COMPLETED"
