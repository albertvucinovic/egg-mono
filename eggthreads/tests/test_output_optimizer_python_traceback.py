from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.output_optimizer import OptimizeRequest, OutputOptimizer, PythonTracebackFocusFilter
from eggthreads.tools import ToolRegistry


def _traceback(frame_count: int = 10, *, prefix: str = "") -> str:
    lines = []
    if prefix:
        lines.extend(prefix.splitlines())
    lines.append("Traceback (most recent call last):")
    for idx in range(1, frame_count + 1):
        lines.append(f"  File \"/workspace/project/src/package/module_{idx:03d}.py\", line {100 + idx}, in function_{idx}")
        lines.append(f"    return call_next_{idx}()")
    lines.append("ValueError: important failure detail")
    return "\n".join(lines)


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


def test_python_traceback_filter_focuses_long_traceback_and_reports_metadata() -> None:
    raw = _traceback(10)

    decision = OutputOptimizer([PythonTracebackFocusFilter(max_frames=5, head_frames=2, tail_frames=2)]).optimize(
        OptimizeRequest(tool_name="python", output=raw)
    )

    assert decision.optimized is True
    assert decision.filter_name == "python_traceback_focus"
    assert decision.output == "\n".join(
        [
            "Traceback (most recent call last):",
            "  File \"/workspace/project/src/package/module_001.py\", line 101, in function_1",
            "    return call_next_1()",
            "  File \"/workspace/project/src/package/module_002.py\", line 102, in function_2",
            "    return call_next_2()",
            "  [... omitted 6 middle traceback frames ...]",
            "  File \"/workspace/project/src/package/module_009.py\", line 109, in function_9",
            "    return call_next_9()",
            "  File \"/workspace/project/src/package/module_010.py\", line 110, in function_10",
            "    return call_next_10()",
            "ValueError: important failure detail",
        ]
    )
    assert decision.metadata["frame_count"] == 10
    assert decision.metadata["emitted_frames"] == 4
    assert decision.metadata["omitted_frames"] == 6
    assert decision.metadata["exception"] == "ValueError: important failure detail"
    assert decision.savings_pct > 0


def test_python_traceback_filter_preserves_stderr_header_prefix() -> None:
    raw = _traceback(8, prefix="--- STDERR ---")

    decision = OutputOptimizer([PythonTracebackFocusFilter(max_frames=4, head_frames=1, tail_frames=2)]).optimize(
        OptimizeRequest(tool_name="bash", output=raw)
    )

    assert decision.optimized is True
    assert decision.output.startswith("--- STDERR ---\nTraceback (most recent call last):")
    assert "[... omitted 5 middle traceback frames ...]" in decision.output
    assert decision.output.endswith("ValueError: important failure detail")
    assert decision.metadata["original_had_stderr_header"] is True


def test_python_traceback_filter_abstains_on_short_or_non_traceback_text() -> None:
    short_traceback = _traceback(3)
    short_decision = OutputOptimizer([PythonTracebackFocusFilter(max_frames=6)]).optimize(
        OptimizeRequest(output=short_traceback)
    )
    assert short_decision.optimized is False
    assert short_decision.output == short_traceback

    stack_like = "Error: boom\n    at function (/workspace/app.js:10:2)\n    at main (/workspace/app.js:20:2)"
    stack_decision = OutputOptimizer([PythonTracebackFocusFilter()]).optimize(OptimizeRequest(output=stack_like))
    assert stack_decision.optimized is False
    assert stack_decision.output == stack_like

    malformed = "Traceback (most recent call last):\n  File \"x.py\", line 1, in main\n    main()\nnot an exception summary with spaces"
    malformed_decision = OutputOptimizer([PythonTracebackFocusFilter()]).optimize(OptimizeRequest(output=malformed))
    assert malformed_decision.optimized is False
    assert malformed_decision.output == malformed

    chained = raw = _traceback(4) + "\n\nDuring handling of the above exception, another exception occurred:\n\n" + _traceback(4)
    chained_decision = OutputOptimizer([PythonTracebackFocusFilter()]).optimize(OptimizeRequest(output=chained))
    assert chained_decision.optimized is False
    assert chained_decision.output == raw


def test_enabled_policy_uses_traceback_focus_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "tracebacker",
        {},
        content="$ tracebacker",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _traceback(12, prefix="--- STDERR ---")

    tools = ToolRegistry()
    tools.register("tracebacker", "Tracebacker", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] != raw_output
    assert approval["preview"].startswith("--- STDERR ---\nTraceback (most recent call last):")
    assert "[... omitted 7 middle traceback frames ...]" in approval["preview"]
    assert "ValueError: important failure detail" in approval["preview"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "python_traceback_focus"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "[... omitted 7 middle traceback frames ...]" in tool_msg["content"]


def test_disabled_policy_keeps_default_traceback_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "off")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "tracebacker",
        {},
        content="$ tracebacker",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _traceback(12, prefix="--- STDERR ---")

    tools = ToolRegistry()
    tools.register("tracebacker", "Tracebacker", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
