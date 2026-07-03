from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.output_optimizer import OptimizeRequest, OutputOptimizer, PytestFailureSummaryFilter
from eggthreads.tools import ToolRegistry


def _failure_section(name: str, *, line_start: int = 1, lines: int = 18) -> str:
    body = [f"____________________________ {name} ____________________________"]
    body.append(f"tests/test_sample.py:{line_start}: in {name}")
    body.append("    assert compute_value() == expected_value")
    body.append("E   AssertionError: important assertion failure detail")
    for idx in range(1, lines + 1):
        body.append(f"E   detail line {idx} for {name} with enough text to make pytest summarization useful")
    return "\n".join(body)


def _error_section(name: str, *, lines: int = 16) -> str:
    body = [f"____________________________ ERROR at setup of {name} ____________________________"]
    body.append("tests/conftest.py:12: in fixture")
    body.append("    raise RuntimeError('fixture failed')")
    body.append("E   RuntimeError: fixture failed")
    for idx in range(1, lines + 1):
        body.append(f"E   fixture detail line {idx} for {name} with setup context")
    return "\n".join(body)


def _pytest_failure_output(*, prefix: str = "") -> str:
    parts: list[str] = []
    if prefix:
        parts.extend(prefix.splitlines())
    parts.extend(
        [
            "============================= test session starts =============================",
            "platform linux -- Python 3.12.0, pytest-9.0.0",
            "collected 5 items",
            "",
            "tests/test_sample.py FFEF                                              [100%]",
            "",
            "=================================== FAILURES ===================================",
            _failure_section("test_alpha_failure", line_start=10),
            _failure_section("test_beta_failure", line_start=40),
            _failure_section("test_gamma_failure", line_start=70),
            "==================================== ERRORS ====================================",
            _error_section("test_delta_error"),
            "=========================== short test summary info ============================",
            "FAILED tests/test_sample.py::test_alpha_failure - AssertionError: important assertion failure detail",
            "FAILED tests/test_sample.py::test_beta_failure - AssertionError: important assertion failure detail",
            "FAILED tests/test_sample.py::test_gamma_failure - AssertionError: important assertion failure detail",
            "ERROR tests/test_sample.py::test_delta_error - RuntimeError: fixture failed",
            "========================= 3 failed, 1 error in 0.42s =========================",
        ]
    )
    return "\n".join(parts)


def _latest_payload(db, thread_id: str, event_type: str, tool_call_id: str | None = None) -> dict:
    if tool_call_id is None:
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq DESC LIMIT 1",
            (thread_id, event_type),
        ).fetchone()
    else:
        row = db.conn.execute(
            """
            SELECT payload_json FROM events
             WHERE thread_id=? AND type=? AND json_extract(payload_json, '$.tool_call_id')=?
             ORDER BY event_seq DESC LIMIT 1
            """,
            (thread_id, event_type, tool_call_id),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_pytest_filter_summarizes_failures_and_reports_caps() -> None:
    raw = _pytest_failure_output()

    decision = OutputOptimizer(
        [PytestFailureSummaryFilter(max_summary_entries=2, max_sections=2, max_lines_per_section=8, section_head_lines=4, section_tail_lines=3)]
    ).optimize(OptimizeRequest(tool_name="bash", tool_args={"script": "pytest -q"}, output=raw))

    assert decision.optimized is True
    assert decision.filter_name == "pytest_failure_summary"
    assert decision.output.startswith("Pytest failure summary:\n  FAILED tests/test_sample.py::test_alpha_failure")
    assert "[... omitted 2 more summary entries ...]" in decision.output
    assert "Final: 3 failed, 1 error in 0.42s" in decision.output
    assert "[FAILURE] test_alpha_failure" in decision.output
    assert "E   AssertionError: important assertion failure detail" in decision.output
    assert "[... omitted " in decision.output
    assert "[... omitted 2 more pytest failure/error sections" in decision.output
    assert "test_delta_error" in decision.output
    assert decision.metadata["summary_count"] == 4
    assert decision.metadata["section_count"] == 4
    assert decision.metadata["emitted_summary_entries"] == 2
    assert decision.metadata["emitted_sections"] == 2
    assert decision.metadata["omitted_sections"] == 2
    assert decision.savings_pct > 0


def test_pytest_filter_accepts_direct_tool_and_python_module_invocation_with_stdout_header() -> None:
    raw = _pytest_failure_output(prefix="--- STDOUT ---")

    direct = OutputOptimizer([PytestFailureSummaryFilter(max_sections=1)]).optimize(
        OptimizeRequest(tool_name="pytest", output=raw)
    )
    assert direct.optimized is True
    assert direct.metadata["original_had_stdout_header"] is True

    python_module = OutputOptimizer([PytestFailureSummaryFilter(max_sections=1)]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "python -m pytest tests -q"}, output=raw)
    )
    assert python_module.optimized is True


def test_pytest_filter_abstains_on_passing_malformed_or_non_pytest_output() -> None:
    passing = "============================= test session starts =============================\ncollected 1 item\n\ntest_ok.py . [100%]\n============================== 1 passed in 0.01s =============================="
    passing_decision = OutputOptimizer([PytestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "pytest -q"}, output=passing)
    )
    assert passing_decision.optimized is False
    assert passing_decision.output == passing

    unittest_like = "FAIL: test_alpha (tests.TestThing.test_alpha)\nTraceback (most recent call last):\n  File \"test.py\", line 1, in test_alpha\nAssertionError: boom"
    unittest_decision = OutputOptimizer([PytestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "python -m unittest"}, output=unittest_like)
    )
    assert unittest_decision.optimized is False
    assert unittest_decision.output == unittest_like

    malformed = "=================================== FAILURES ===================================\nnot enough structure\n=========================== short test summary info ============================\nFAILED tests/test_x.py::test_y - AssertionError"
    malformed_decision = OutputOptimizer([PytestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "pytest"}, output=malformed)
    )
    assert malformed_decision.optimized is False
    assert malformed_decision.output == malformed

    non_pytest_context = OutputOptimizer([PytestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cat pytest-output.txt"}, output=_pytest_failure_output())
    )
    assert non_pytest_context.optimized is False
    assert non_pytest_context.output == _pytest_failure_output()


def test_enabled_policy_uses_pytest_filter_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "pytest -q"},
        content="$ pytest -q",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _pytest_failure_output(prefix="--- STDOUT ---")

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] != raw_output
    assert approval["preview"].startswith("Pytest failure summary:")
    assert "tests/test_sample.py::test_alpha_failure" in approval["preview"]
    assert "test session starts" not in approval["preview"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "pytest_failure_summary"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "Pytest failure summary:" in tool_msg["content"]


def test_disabled_policy_keeps_default_pytest_output(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "pytest -q"},
        content="$ pytest -q",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _pytest_failure_output(prefix="--- STDOUT ---")

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
