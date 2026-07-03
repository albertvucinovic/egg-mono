from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.output_optimizer import GrepRgGroupByFileFilter, OptimizeRequest, OutputOptimizer
from eggthreads.output_optimizer.classify import simple_bash_command_name
from eggthreads.tools import ToolRegistry


def _request(output: str, *, script: str = "rg needle") -> OptimizeRequest:
    return OptimizeRequest(tool_name="bash", tool_args={"script": script}, output=output)


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


def test_simple_bash_command_classifier_is_conservative() -> None:
    assert simple_bash_command_name("rg needle src") == "rg"
    assert simple_bash_command_name("VAR=1 command grep -n needle file.txt") == "grep"
    assert simple_bash_command_name("rg needle | head") is None
    assert simple_bash_command_name("grep needle; echo done") is None
    assert simple_bash_command_name("grep $(printf needle)") is None


def test_grep_filter_groups_matches_and_reports_caps() -> None:
    path_a = "src/packages/example_feature/long_module_a.py"
    path_b = "src/packages/example_feature/long_module_b.py"
    path_c = "src/packages/example_feature/long_module_c.py"
    output = "\n".join(
        [
            f"{path_a}:1:alpha needle with enough context to make grouping beneficial",
            f"{path_a}:2:beta needle with enough context to make grouping beneficial",
            f"{path_a}:3:gamma needle with enough context to make grouping beneficial",
            f"{path_b}:10:delta needle with enough context to make grouping beneficial",
            f"{path_c}:20:epsilon needle with enough context to make grouping beneficial",
        ]
    )

    decision = OutputOptimizer(
        [GrepRgGroupByFileFilter(max_files=10, max_matches_per_file=2, max_matches_total=3)]
    ).optimize(_request(output, script="rg -n needle src"))

    assert decision.optimized is True
    assert decision.filter_name == "grep_rg_group_by_file"
    assert decision.output == "\n".join(
        [
            f"{path_a}:",
            "  1: alpha needle with enough context to make grouping beneficial",
            "  2: beta needle with enough context to make grouping beneficial",
            "  [... omitted 1 more matches in this file ...]",
            "",
            f"{path_b}:",
            "  10: delta needle with enough context to make grouping beneficial",
            "",
            "[... omitted 1 more files / 1 matches due to cap ...]",
        ]
    )
    assert decision.metadata["file_count"] == 3
    assert decision.metadata["match_count"] == 5
    assert decision.metadata["emitted_files"] == 2
    assert decision.metadata["omitted_files"] == 1
    assert decision.metadata["omitted_matches"] == 2
    assert decision.metadata["max_matches_total"] == 3
    assert decision.savings_pct > 0

    direct_tool_decision = OutputOptimizer([GrepRgGroupByFileFilter()]).optimize(
        OptimizeRequest(tool_name="rg", output=output)
    )
    assert direct_tool_decision.optimized is True
    assert direct_tool_decision.filter_name == "grep_rg_group_by_file"


def test_grep_filter_abstains_for_non_grep_or_ambiguous_colon_text() -> None:
    output = "notes:1:this looks colon shaped\nnotes:2:but command is not grep"

    non_grep = OutputOptimizer([GrepRgGroupByFileFilter()]).optimize(_request(output, script="cat notes.txt"))
    assert non_grep.optimized is False
    assert non_grep.output == output

    ambiguous = OutputOptimizer([GrepRgGroupByFileFilter()]).optimize(_request("INFO: startup\npath:2:value"))
    assert ambiguous.optimized is False
    assert ambiguous.output == "INFO: startup\npath:2:value"


def test_grep_filter_accepts_bash_stdout_header() -> None:
    output = "--- STDOUT ---\nvery/long/path/src/a.py:7:needle with context\nvery/long/path/src/a.py:8:needle again"

    decision = OutputOptimizer([GrepRgGroupByFileFilter()]).optimize(_request(output, script="grep -n needle file"))

    assert decision.optimized is True
    assert decision.output == "very/long/path/src/a.py:\n  7: needle with context\n  8: needle again"
    assert decision.metadata["original_had_stdout_header"] is True


def test_enabled_policy_uses_grep_grouping_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "rg -n needle src"},
        content="$ rg -n needle src",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\n" + "\n".join(
        [
            "src/a.py:1:alpha needle",
            "src/a.py:2:beta needle",
            "src/a.py:3:gamma needle",
            "src/b.py:10:delta needle",
        ]
    )

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == "\n".join(
        [
            "src/a.py:",
            "  1: alpha needle",
            "  2: beta needle",
            "  3: gamma needle",
            "",
            "src/b.py:",
            "  10: delta needle",
        ]
    )
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "grep_rg_group_by_file"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)
    assert optimizer["optimized_chars"] == len(approval["preview"])

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "src/a.py:" in tool_msg["content"]
    assert "src/a.py:1:alpha needle" not in tool_msg["content"]


def test_disabled_policy_keeps_default_grep_output(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "rg -n needle src"},
        content="$ rg -n needle src",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\nsrc/a.py:1:alpha needle\nsrc/a.py:2:beta needle\nsrc/b.py:10:delta needle"

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
