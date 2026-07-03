from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.output_optimizer import FindPathGroupFilter, OptimizeRequest, OutputOptimizer
from eggthreads.tools import ToolRegistry


def _request(output: str, *, script: str = "find src -type f") -> OptimizeRequest:
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


def test_find_filter_groups_paths_and_reports_caps() -> None:
    dir_a = "src/packages/example_feature/dir_a"
    dir_b = "src/packages/example_feature/dir_b"
    dir_c = "src/packages/example_feature/dir_c"
    output = "\n".join(
        [
            f"{dir_a}/module_001.py",
            f"{dir_a}/module_002.py",
            f"{dir_a}/module_003.py",
            f"{dir_b}/module_010.py",
            f"{dir_c}/module_020.py",
        ]
    )

    decision = OutputOptimizer(
        [FindPathGroupFilter(min_paths=5, max_dirs=10, max_files_per_dir=2, max_paths_total=3)]
    ).optimize(_request(output, script="find src -type f"))

    assert decision.optimized is True
    assert decision.filter_name == "find_path_group_by_directory"
    assert decision.output == "\n".join(
        [
            f"{dir_a}:",
            "  module_001.py",
            "  module_002.py",
            "  [... omitted 1 more paths in this directory ...]",
            "",
            f"{dir_b}:",
            "  module_010.py",
            "",
            "[... omitted 1 more directories / 1 paths due to cap ...]",
        ]
    )
    assert decision.metadata["directory_count"] == 3
    assert decision.metadata["path_count"] == 5
    assert decision.metadata["emitted_dirs"] == 2
    assert decision.metadata["omitted_dirs"] == 1
    assert decision.metadata["omitted_paths"] == 2
    assert decision.metadata["max_paths_total"] == 3
    assert decision.savings_pct > 0


def test_find_filter_accepts_direct_fd_tool_name() -> None:
    directory = "src/packages/example_feature/dir"
    output = "\n".join(f"{directory}/module_{idx:03d}.py" for idx in range(1, 7))

    decision = OutputOptimizer([FindPathGroupFilter(min_paths=5)]).optimize(
        OptimizeRequest(tool_name="fd", output=output)
    )

    assert decision.optimized is True
    assert decision.filter_name == "find_path_group_by_directory"
    assert decision.output.startswith(f"{directory}:\n  module_001.py")


def test_find_filter_accepts_bash_stdout_header() -> None:
    directory = "very/long/path/src"
    output = "--- STDOUT ---\n" + "\n".join(f"{directory}/file_{idx:03d}.txt" for idx in range(1, 7))

    decision = OutputOptimizer([FindPathGroupFilter(min_paths=5)]).optimize(
        _request(output, script="find very/long/path/src -type f")
    )

    assert decision.optimized is True
    assert decision.output.startswith(f"{directory}:\n  file_001.txt")
    assert "--- STDOUT ---" not in decision.output
    assert decision.metadata["original_had_stdout_header"] is True


def test_find_filter_abstains_for_non_find_path_text_and_grep_colon_output() -> None:
    path_list = "\n".join(f"src/app/file_{idx:03d}.py" for idx in range(1, 10))

    non_find = OutputOptimizer([FindPathGroupFilter()]).optimize(_request(path_list, script="cat files.txt"))
    assert non_find.optimized is False
    assert non_find.output == path_list

    colon_output = "\n".join(f"src/app/file_{idx:03d}.py:{idx}:needle" for idx in range(1, 10))
    grep_like = OutputOptimizer([FindPathGroupFilter()]).optimize(_request(colon_output, script="find src -type f"))
    assert grep_like.optimized is False
    assert grep_like.output == colon_output

    mixed = OutputOptimizer([FindPathGroupFilter()]).optimize(_request("INFO starting\nsrc/app/file_001.py\nsrc/app/file_002.py"))
    assert mixed.optimized is False
    assert mixed.output == "INFO starting\nsrc/app/file_001.py\nsrc/app/file_002.py"


def test_enabled_policy_uses_find_grouping_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "find src/packages/app -type f"},
        content="$ find src/packages/app -type f",
        auto_approve=True,
        hidden=False,
    )
    dir_a = "src/packages/app/feature_a"
    dir_b = "src/packages/app/feature_b"
    raw_paths = [
        *(f"{dir_a}/module_{idx:03d}.py" for idx in range(1, 6)),
        *(f"{dir_b}/view_{idx:03d}.tsx" for idx in range(1, 6)),
    ]
    raw_output = "--- STDOUT ---\n" + "\n".join(raw_paths)

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
            f"{dir_a}:",
            "  module_001.py",
            "  module_002.py",
            "  module_003.py",
            "  module_004.py",
            "  module_005.py",
            "",
            f"{dir_b}:",
            "  view_001.tsx",
            "  view_002.tsx",
            "  view_003.tsx",
            "  view_004.tsx",
            "  view_005.tsx",
        ]
    )
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "find_path_group_by_directory"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert f"{dir_a}:" in tool_msg["content"]
    assert f"{dir_a}/module_001.py" not in tool_msg["content"]


def test_disabled_policy_keeps_default_find_output(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "find src -type f"},
        content="$ find src -type f",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\n" + "\n".join(f"src/app/file_{idx:03d}.py" for idx in range(1, 10))

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
