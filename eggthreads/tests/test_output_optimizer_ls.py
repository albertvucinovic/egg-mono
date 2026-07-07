from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.output_optimizer import LsLongListingFilter, OptimizeRequest, OutputOptimizer
from eggthreads.tools import ToolRegistry


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


def _ls_line(kind: str, name: str, size: int = 1234) -> str:
    mode = "drwxr-xr-x" if kind == "d" else "lrwxrwxrwx" if kind == "l" else "-rw-r--r--"
    return f"{mode} 1 albert albert {size} Jan 01 12:00 {name}"


def _ls_output(count: int = 30) -> str:
    lines = ["--- STDOUT ---", "total 120"]
    lines.append(_ls_line("d", ".", 4096))
    lines.append(_ls_line("d", "..", 4096))
    lines.extend(_ls_line("d", f"package_{idx:03d}", 4096) for idx in range(1, 6))
    lines.extend(_ls_line("-", f"very_long_file_name_{idx:03d}.py", idx * 10) for idx in range(1, count + 1))
    lines.append(_ls_line("l", "current -> package_001"))
    return "\n".join(lines)


def test_ls_long_listing_filter_summarizes_entries_and_caps() -> None:
    raw = _ls_output(12)

    decision = OutputOptimizer([LsLongListingFilter(min_entries=8, max_entries_per_kind=4)]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "ls -la"}, output=raw)
    )

    assert decision.optimized is True
    assert decision.filter_name == "ls_long_listing_summary"
    assert decision.output.startswith("ls -l summary: 18 entries")
    assert "directories (5):" in decision.output
    assert "files (12):" in decision.output
    assert "very_long_file_name_001.py (10 bytes)" in decision.output
    assert "[... omitted 8 more files ...]" in decision.output
    assert "symlinks (1):" in decision.output
    assert decision.metadata["entry_count"] == 18
    assert decision.metadata["kind_counts"]["-"] == 12


def test_ls_long_listing_filter_abstains_for_plain_ls_and_non_ls_output() -> None:
    raw = _ls_output(12)

    plain_ls = OutputOptimizer([LsLongListingFilter(min_entries=8)]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "ls"}, output=raw)
    )
    assert plain_ls.optimized is False

    non_ls = OutputOptimizer([LsLongListingFilter(min_entries=8)]).optimize(
        OptimizeRequest(tool_name="bash", tool_args={"script": "cat listing.txt"}, output=raw)
    )
    assert non_ls.optimized is False


def test_default_policy_uses_ls_filter_and_raw_artifact_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="ls-root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "ls -la"},
        content="$ ls -la",
        auto_approve=True,
        hidden=False,
    )
    raw_output = _ls_output(100)

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"].startswith("ls -l summary:")
    assert "read_long_tool_output(" in approval["preview"]
    assert approval["artifact_path"]
    assert approval["channels"]["optimizer"]["filter_name"] == "ls_long_listing_summary"

    artifact_id = Path(approval["artifact_path"]).name
    read = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 1},
        thread_id=tid,
        db=db,
    )
    assert "very_long_file_name_100.py" in read

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg.get("user_tool_call") is True
    assert "ls -l summary:" in tool_msg["content"]
    assert "read_long_tool_output(" in tool_msg["content"]
