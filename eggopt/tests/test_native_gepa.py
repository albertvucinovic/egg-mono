from __future__ import annotations

from dataclasses import dataclass

from eggopt import Evaluation, NativeGEPA, Reflection
from eggopt.gepa import CandidateMutation


class Increment:
    def __init__(self) -> None:
        self.calls = 0
        self.threads: list[str] = []

    def start(self, conversation, request):
        return self._mutate(conversation, request)

    def resume(self, conversation, request):
        return self._mutate(conversation, request)

    def _mutate(self, conversation, request):
        self.calls += 1
        self.threads.append(conversation.thread_id)
        value = int(request["candidate"]["instruction"]) + 1
        mutation = CandidateMutation({"instruction": str(value)})
        conversation.append_assistant(f"Try level {value}", mutation)
        return mutation


@dataclass(frozen=True)
class Example:
    name: str
    target: int


class Metric:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, candidate, example):
        self.calls += 1
        value = int(candidate["instruction"])
        return Evaluation(
            score=float(value >= example.target),
            output=value,
            feedback=f"reach {example.target}",
            evidence={"name": example.name, "target": example.target},
        )


def optimizer(tmp_path, metric, drive):
    return NativeGEPA(
        metric=metric,
        reflection=Reflection(drive, {"name": "increment-v1"}),
        run_dir=tmp_path / "poem",
        generations=2,
        metric_identity={"name": "threshold-v1"},
        example_id=lambda example: example.name,
    )


def test_native_gepa_reads_like_the_algorithm(tmp_path):
    metric = Metric()
    drive = Increment()

    result = optimizer(tmp_path, metric, drive).compile(
        {"instruction": "0"},
        trainset=[Example("easy", 1), Example("hard", 2)],
    )

    assert result.best_candidate == {"instruction": "2"}
    assert result.best_score == 1.0
    assert result.parents == (None, 0, 1)
    assert result.metric_calls == metric.calls == 6
    assert drive.calls == 2
    assert len(set(drive.threads)) == 1


def test_native_gepa_replays_without_repeating_work(tmp_path):
    first_metric = Metric()
    first_drive = Increment()
    first = optimizer(tmp_path, first_metric, first_drive).compile(
        {"instruction": "0"},
        trainset=[Example("easy", 1), Example("hard", 2)],
    )

    replay_metric = Metric()
    replay_drive = Increment()
    replay = optimizer(tmp_path, replay_metric, replay_drive).compile(
        {"instruction": "0"},
        trainset=[Example("easy", 1), Example("hard", 2)],
    )

    assert replay.best_candidate == first.best_candidate
    assert replay.scores == first.scores
    assert replay.parents == first.parents
    assert replay.outputs == first.outputs
    assert replay.metric_calls == 0
    assert replay_metric.calls == 0
    assert replay_drive.calls == 0
