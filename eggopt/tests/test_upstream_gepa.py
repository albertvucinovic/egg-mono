from __future__ import annotations

from eggopt import Reflection, UpstreamGEPA
from eggopt.gepa import CandidateMutation, ExampleEvaluation, ReflectionEvidence


class Drive:
    def start(self, conversation, request):
        return self._mutate(conversation, request)

    def resume(self, conversation, request):
        return self._mutate(conversation, request)

    def _mutate(self, conversation, request):
        value = int(request["candidate"]["instruction"]) + 1
        mutation = CandidateMutation({"instruction": str(value)})
        conversation.append_assistant(str(value), mutation)
        return mutation


def metric(candidate, example):
    value = int(candidate["instruction"])
    score = float(value >= example["target"])
    return ExampleEvaluation(
        output=value,
        score=score,
        evidence=ReflectionEvidence(
            inputs=example,
            generated_outputs=value,
            feedback=f"reach {example['target']}",
        ),
    )


def test_upstream_gepa_hides_the_runtime(tmp_path):
    optimizer = UpstreamGEPA(
        metric=metric,
        reflection=Reflection(Drive(), {"name": "increment-v1"}),
        run_dir=tmp_path / "upstream",
        metric_identity={"name": "threshold-v1"},
        example_id=lambda example: example["id"],
        perfect_score=1.0,
        skip_perfect_score=True,
        reflection_minibatch_size=1,
        display_progress_bar=False,
        stop_callbacks=lambda state: len(state.program_candidates) >= 2,
    )

    result = optimizer.compile(
        {"instruction": "0"},
        trainset=[{"id": "one", "target": 1}],
        valset=[{"id": "one", "target": 1}],
    )

    assert result.best_candidate == {"instruction": "1"}
    assert (tmp_path / "upstream" / "flow.db").is_file()
    assert (tmp_path / "upstream" / ".egg" / "threads.sqlite").is_file()
