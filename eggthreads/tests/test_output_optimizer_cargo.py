from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.output_optimizer import CargoTestFailureSummaryFilter, OptimizeRequest, OutputOptimizer
from eggthreads.tools import ToolRegistry


def _cargo_failure_section(name: str, *, file: str = "src/lib.rs", line: int = 10, detail_lines: int = 18) -> str:
    lines = [
        f"---- {name} stdout ----",
        f"thread '{name}' panicked at {file}:{line}:5:",
        "assertion `left == right` failed",
        "  left: 1",
        " right: 2",
    ]
    for idx in range(1, detail_lines + 1):
        lines.append(f"stack/detail line {idx} for {name} with enough text to make cargo summarization useful")
    lines.append("note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace")
    return "\n".join(lines)


def _cargo_failure_output(*, prefix: str = "") -> str:
    failing = [
        "tests::alpha_failure",
        "tests::beta_failure",
        "integration::gamma_failure",
        "integration::delta_failure",
    ]
    parts: list[str] = []
    if prefix:
        parts.extend(prefix.splitlines())
    parts.extend(
        [
            "running 5 tests",
            "test tests::alpha_failure ... FAILED",
            "test tests::beta_failure ... FAILED",
            "test integration::gamma_failure ... FAILED",
            "test integration::delta_failure ... FAILED",
            "test tests::passes ... ok",
            "",
            "failures:",
            _cargo_failure_section(failing[0], file="src/lib.rs", line=10),
            _cargo_failure_section(failing[1], file="src/lib.rs", line=42),
            _cargo_failure_section(failing[2], file="tests/integration.rs", line=8),
            _cargo_failure_section(failing[3], file="tests/integration.rs", line=99),
            "",
            "failures:",
            *(f"    {name}" for name in failing),
            "",
            "test result: FAILED. 1 passed; 4 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s",
            "error: test failed, to rerun pass `--lib`",
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


def test_cargo_filter_summarizes_failures_and_reports_caps() -> None:
    raw = _cargo_failure_output()

    decision = OutputOptimizer(
        [CargoTestFailureSummaryFilter(max_failure_names=2, max_sections=2, max_lines_per_section=8, section_head_lines=5, section_tail_lines=2)]
    ).optimize(OptimizeRequest(tool_name="bash", tool_args={"script": "cargo test"}, output=raw))

    assert decision.optimized is True
    assert decision.filter_name == "cargo_test_failure_summary"
    assert decision.output.startswith("Cargo test failure summary:\n  FAILED tests::alpha_failure")
    assert "[... omitted 2 more failing test names ...]" in decision.output
    assert "Final: test result: FAILED. 1 passed; 4 failed" in decision.output
    assert "error: test failed, to rerun pass `--lib`" in decision.output
    assert "[FAILURE] tests::alpha_failure" in decision.output
    assert "panicked at src/lib.rs:10:5:" in decision.output
    assert "[... omitted " in decision.output
    assert "[... omitted 2 more cargo failure sections" in decision.output
    assert "integration::delta_failure" in decision.output
    assert decision.metadata["failure_name_count"] == 4
    assert decision.metadata["section_count"] == 4
    assert decision.metadata["emitted_failure_names"] == 2
    assert decision.metadata["emitted_sections"] == 2
    assert decision.metadata["omitted_sections"] == 2
    assert decision.savings_pct > 0


def test_cargo_filter_accepts_direct_cargo_tool_metadata_and_stdout_header() -> None:
    raw = _cargo_failure_output(prefix="--- STDOUT ---")

    direct = OutputOptimizer([CargoTestFailureSummaryFilter(max_sections=1)]).optimize(
        OptimizeRequest(tool_name="cargo", tool_args={"args": ["test", "--workspace"]}, output=raw)
    )

    assert direct.optimized is True
    assert direct.filter_name == "cargo_test_failure_summary"
    assert direct.metadata["original_output_header"] == "--- STDOUT ---"
    assert direct.output.startswith("Cargo test failure summary:")

    toolchain_wrapped = OutputOptimizer([CargoTestFailureSummaryFilter(max_sections=1)]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cargo +nightly test --workspace"}, output=raw)
    )
    assert toolchain_wrapped.optimized is True


def test_cargo_filter_abstains_on_passing_malformed_or_non_cargo_output() -> None:
    passing = "running 1 test\ntest tests::ok ... ok\n\ntest result: ok. 1 passed; 0 failed; finished in 0.01s"
    passing_decision = OutputOptimizer([CargoTestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cargo test"}, output=passing)
    )
    assert passing_decision.optimized is False
    assert passing_decision.output == passing

    compiler = "error[E0425]: cannot find value `x` in this scope\n --> src/lib.rs:1:5\nerror: could not compile `crate`"
    compiler_decision = OutputOptimizer([CargoTestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cargo build"}, output=compiler)
    )
    assert compiler_decision.optimized is False
    assert compiler_decision.output == compiler

    malformed = "failures:\n    tests::alpha_failure\n\ntest result: FAILED. 0 passed; 1 failed; finished in 0.01s"
    malformed_decision = OutputOptimizer([CargoTestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cargo test"}, output=malformed)
    )
    assert malformed_decision.optimized is False
    assert malformed_decision.output == malformed

    non_cargo_context = OutputOptimizer([CargoTestFailureSummaryFilter()]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cat cargo-output.txt"}, output=_cargo_failure_output())
    )
    assert non_cargo_context.optimized is False
    assert non_cargo_context.output == _cargo_failure_output()


def test_enabled_policy_uses_cargo_filter_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "cargo test"},
        content="$ cargo test",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _cargo_failure_output(prefix="--- STDOUT ---")

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] != raw_output
    assert approval["preview"].startswith("Cargo test failure summary:")
    assert "tests::alpha_failure" in approval["preview"]
    assert "running 5 tests" not in approval["preview"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "cargo_test_failure_summary"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "Cargo test failure summary:" in tool_msg["content"]


def test_disabled_policy_keeps_default_cargo_output(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "cargo test"},
        content="$ cargo test",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _cargo_failure_output(prefix="--- STDOUT ---")

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
