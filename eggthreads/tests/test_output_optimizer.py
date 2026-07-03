from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from eggthreads.output_optimizer import (
    BoundedHeadTailFilter,
    OptimizeDecision,
    OptimizeRequest,
    OutputOptimizer,
    RepeatedLineDedupeFilter,
    bounded_head_tail,
    clean_ansi_controls,
    make_decision,
    suppress_progress_noise,
)


def _request(output: str, **kwargs) -> OptimizeRequest:
    return OptimizeRequest(tool_name="bash", output=output, thread_id="thread", tool_call_id="tool", **kwargs)


@dataclass(frozen=True)
class FakeFilter:
    name: str
    output: str | None = None
    confidence: float = 1.0
    reason: str = "fake"
    throws: bool = False

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if self.throws:
            raise RuntimeError("boom")
        if self.output is None:
            return None
        return make_decision(
            request,
            self.output,
            filter_name=self.name,
            reason=self.reason,
            confidence=self.confidence,
            metadata={"candidate": self.name},
        )


def test_request_and_decision_are_frozen_and_freeze_mappings() -> None:
    request = OptimizeRequest(
        tool_name="bash",
        tool_args={"script": "echo hi", "nested": {"k": ["v"]}},
        output="hello",
        metadata={"tags": ["one"]},
    )
    decision = make_decision(request, "hell", filter_name="short", reason="test", metadata={"items": [1, 2]})

    with pytest.raises(FrozenInstanceError):
        request.output = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.tool_args["script"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        request.metadata["tags"] = ("two",)  # type: ignore[index]
    assert request.tool_args["nested"]["k"] == ("v",)

    with pytest.raises(FrozenInstanceError):
        decision.output = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        decision.metadata["items"] = (3,)  # type: ignore[index]
    assert decision.metadata["items"] == (1, 2)


def test_optimizer_unavailable_no_filters_returns_unchanged_decision() -> None:
    request = _request("hello world")

    decision = OutputOptimizer().optimize(request)

    assert decision.optimized is False
    assert decision.output == request.output
    assert decision.filter_name is None
    assert decision.reason == "no_filters"
    assert decision.raw_chars == len(request.output)
    assert decision.optimized_chars == len(request.output)
    assert decision.savings_pct == 0.0
    assert decision.metadata["fallback"] is True
    assert decision.metadata["filter_count"] == 0


def test_optimizer_min_size_threshold_returns_unchanged_decision() -> None:
    request = _request("tiny")

    decision = OutputOptimizer([FakeFilter("short", "x")], min_size_chars=10).optimize(request)

    assert decision.optimized is False
    assert decision.output == "tiny"
    assert decision.reason == "below_min_size"
    assert decision.metadata["min_size_chars"] == 10


def test_optimizer_accepts_first_beneficial_filter_and_records_savings() -> None:
    request = _request("0123456789")

    decision = OutputOptimizer([FakeFilter("short", "0123")], min_confidence=0.5).optimize(request)

    assert decision.optimized is True
    assert decision.output == "0123"
    assert decision.filter_name == "short"
    assert decision.reason == "fake"
    assert decision.confidence == 1.0
    assert decision.raw_chars == 10
    assert decision.optimized_chars == 4
    assert decision.savings_pct == pytest.approx(60.0)
    assert decision.metadata["savings_chars"] == 6
    assert decision.metadata["fallback"] is False
    assert decision.metadata["accepted_filter"] == "short"


def test_optimizer_preserves_order_and_accepts_later_beneficial_filter() -> None:
    request = _request("abcdef")

    decision = OutputOptimizer([FakeFilter("noop"), FakeFilter("short", "abc")]).optimize(request)

    assert decision.optimized is True
    assert decision.filter_name == "short"
    assert decision.output == "abc"
    assert decision.metadata["rejected_filters"][0]["filter_name"] == "noop"
    assert decision.metadata["rejected_filters"][0]["reason"] == "abstained"


def test_optimizer_rejects_expanding_filter_with_never_worse_guard() -> None:
    request = _request("short")

    decision = OutputOptimizer([FakeFilter("expand", "short but longer")]).optimize(request)

    assert decision.optimized is False
    assert decision.output == "short"
    assert decision.reason == "no_filter_accepted"
    rejection = decision.metadata["rejected_filters"][0]
    assert rejection["filter_name"] == "expand"
    assert rejection["reason"] == "not_smaller"
    assert rejection["raw_chars"] == len("short")
    assert rejection["optimized_chars"] == len("short but longer")


def test_optimizer_rejects_low_confidence_filter() -> None:
    request = _request("abcdef")

    decision = OutputOptimizer([FakeFilter("low", "abc", confidence=0.49)], min_confidence=0.5).optimize(request)

    assert decision.optimized is False
    assert decision.output == "abcdef"
    rejection = decision.metadata["rejected_filters"][0]
    assert rejection["filter_name"] == "low"
    assert rejection["reason"] == "low_confidence"
    assert rejection["confidence"] == 0.49
    assert rejection["min_confidence"] == 0.5


def test_optimizer_converts_throwing_filter_to_fallback() -> None:
    request = _request("abcdef")

    decision = OutputOptimizer([FakeFilter("bad", throws=True)]).optimize(request)

    assert decision.optimized is False
    assert decision.output == "abcdef"
    assert decision.reason == "no_filter_accepted"
    rejection = decision.metadata["rejected_filters"][0]
    assert rejection["filter_name"] == "bad"
    assert rejection["reason"] == "exception"
    assert rejection["exception_type"] == "RuntimeError"
    assert rejection["exception_message"] == "boom"


def test_generic_ansi_control_cleanup_helper() -> None:
    assert clean_ansi_controls("a\x1b[31mred\x1b[0m\rb") == "ared\nb"


def test_generic_progress_noise_suppression_is_conservative() -> None:
    output, metadata = suppress_progress_noise("start\n50% [#####     ]\n|\ndone\n")

    assert output == "start\ndone\n"
    assert metadata["suppressed_progress_lines"] == 2


def test_generic_dedupe_filter_collapses_repeated_lines_with_counts() -> None:
    long_line = "noise " * 20
    request = _request("start\n" + "\n".join([long_line] * 6) + "\nend\n")

    decision = OutputOptimizer([RepeatedLineDedupeFilter()]).optimize(request)

    assert decision.optimized is True
    assert decision.filter_name == "repeated_line_dedupe"
    assert decision.output == f"start\n{long_line}\n[... repeated 5 more times ...]\nend\n"
    assert decision.metadata["dedupe_runs"] == 1
    assert decision.metadata["dedupe_suppressed_lines"] == 5


def test_bounded_head_tail_helper_preserves_head_tail_and_reports_omission() -> None:
    text = "HEADER\n" + "middle\n" * 20 + "TAIL\n"

    output, metadata = bounded_head_tail(text, max_chars=90, head_chars=12, tail_chars=10)

    assert len(output) <= 90
    assert output.startswith("HEADER\nmidd")
    assert output.endswith("le\nTAIL\n")
    assert "[... omitted " in output
    assert " from middle ...]" in output
    assert metadata["bounded"] is True
    assert metadata["max_chars"] == 90
    assert metadata["omitted_chars"] > 0
    assert metadata["omitted_lines"] > 0
    assert metadata["head_chars"] == 12
    assert metadata["tail_chars"] == 10


def test_bounded_head_tail_filter_acts_as_accepted_fallback_preview() -> None:
    request = _request("0123456789" * 20)

    decision = OutputOptimizer([BoundedHeadTailFilter(max_chars=80)]).optimize(request)

    assert decision.optimized is True
    assert decision.filter_name == "bounded_head_tail"
    assert len(decision.output) <= 80
    assert "omitted" in decision.output
    assert decision.metadata["bounded"] is True
