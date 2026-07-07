from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.output_optimizer import GitStatusCompactFilter, OptimizeRequest, OutputOptimizer
from eggthreads.tools import ToolRegistry


def _request(output: str, *, script: str = "git status --short") -> OptimizeRequest:
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


def test_git_status_filter_groups_short_status_and_reports_caps() -> None:
    modified = [f" M src/packages/app/feature/modified_{idx:03d}.py" for idx in range(1, 5)]
    added = [f"A  src/packages/app/feature/added_{idx:03d}.py" for idx in range(1, 3)]
    deleted = [" D src/packages/app/feature/deleted_001.py"]
    untracked = [f"?? src/packages/app/feature/untracked_{idx:03d}.py" for idx in range(1, 4)]
    output = "\n".join([*modified, *added, *deleted, *untracked])

    decision = OutputOptimizer(
        [GitStatusCompactFilter(max_statuses=3, max_entries_per_status=2, max_entries_total=5)]
    ).optimize(_request(output))

    assert decision.optimized is True
    assert decision.filter_name == "git_status_compact"
    assert decision.output == "\n".join(
        [
            "[ M] Modified (4):",
            "  src/packages/app/feature/modified_001.py",
            "  src/packages/app/feature/modified_002.py",
            "  [... omitted 2 more entries with status ' M' ...]",
            "",
            "[A ] Added (2):",
            "  src/packages/app/feature/added_001.py",
            "  src/packages/app/feature/added_002.py",
            "",
            "[ D] Deleted (1):",
            "  src/packages/app/feature/deleted_001.py",
            "",
            "[... omitted 1 more status groups / 3 entries due to cap ...]",
        ]
    )
    assert decision.metadata["status_count"] == 4
    assert decision.metadata["entry_count"] == 10
    assert decision.metadata["emitted_statuses"] == 3
    assert decision.metadata["omitted_statuses"] == 1
    assert decision.metadata["omitted_entries"] == 5
    assert decision.metadata["max_entries_total"] == 5
    assert decision.savings_pct > 0


def test_git_status_filter_preserves_renamed_and_copied_paths() -> None:
    renamed = [
        f"R  src/old/path/component_{idx:03d}.py -> src/new/path/component_{idx:03d}.py" for idx in range(1, 7)
    ]
    copied = [
        f"C  src/source/template_{idx:03d}.py -> src/copied/template_{idx:03d}.py" for idx in range(1, 7)
    ]
    output = "\n".join([*renamed, *copied])

    decision = OutputOptimizer(
        [GitStatusCompactFilter(max_entries_per_status=2, max_entries_total=4)]
    ).optimize(_request(output, script="git status --porcelain=v1"))

    assert decision.optimized is True
    assert "[R ] Renamed (6):" in decision.output
    assert "src/old/path/component_001.py -> src/new/path/component_001.py" in decision.output
    assert "[C ] Copied (6):" in decision.output
    assert "src/source/template_001.py -> src/copied/template_001.py" in decision.output
    assert "[... omitted 4 more entries with status 'R ' ...]" in decision.output
    assert "[... omitted 4 more entries with status 'C ' ...]" in decision.output
    assert decision.metadata["omitted_entries"] == 8


def test_git_status_filter_accepts_direct_git_tool_metadata_and_stdout_header() -> None:
    output = "--- STDOUT ---\n" + "\n".join(
        f" M src/packages/app/feature/modified_{idx:03d}.py" for idx in range(1, 30)
    )

    decision = OutputOptimizer([GitStatusCompactFilter()]).optimize(
        OptimizeRequest(tool_name="git", tool_args={"args": ["status", "--short"]}, output=output)
    )

    assert decision.optimized is True
    assert decision.output.startswith("[ M] Modified (29):\n  src/packages/app/feature/modified_001.py")
    assert "--- STDOUT ---" not in decision.output
    assert decision.metadata["original_had_stdout_header"] is True

    direct_status_tool = OutputOptimizer([GitStatusCompactFilter()]).optimize(
        OptimizeRequest(tool_name="git_status", output="\n".join(f" M src/x_{idx:03d}.py" for idx in range(1, 30)))
    )
    assert direct_status_tool.optimized is True

    env_wrapped_bash = OutputOptimizer([GitStatusCompactFilter()]).optimize(
        _request("\n".join(f" M src/y_{idx:03d}.py" for idx in range(1, 30)), script="GIT_OPTIONAL_LOCKS=0 command git status --short")
    )
    assert env_wrapped_bash.optimized is True


def test_git_status_filter_abstains_on_human_prose_and_non_git_context() -> None:
    human = """On branch output-optimizer
Your branch is up to date with 'origin/output-optimizer'.

Changes not staged for commit:
  modified:   eggthreads/runner.py
""".strip()
    human_decision = OutputOptimizer([GitStatusCompactFilter()]).optimize(_request(human))
    assert human_decision.optimized is False
    assert human_decision.output == human

    status_like = "\n".join(f" M src/packages/app/feature/file_{idx:03d}.py" for idx in range(1, 30))
    non_git = OutputOptimizer([GitStatusCompactFilter()]).optimize(_request(status_like, script="cat status.txt"))
    assert non_git.optimized is False
    assert non_git.output == status_like

    malformed_rename = OutputOptimizer([GitStatusCompactFilter()]).optimize(_request("R  src/new/path.py"))
    assert malformed_rename.optimized is False
    assert malformed_rename.output == "R  src/new/path.py"


def test_enabled_policy_uses_git_status_filter_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "git status --short"},
        content="$ git status --short",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\n" + "\n".join(
        f" M src/packages/app/feature/modified_{idx:03d}.py" for idx in range(1, 130)
    )

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"].startswith("[ M] Modified (129):\n  src/packages/app/feature/modified_001.py")
    assert "[... omitted 49 more entries with status ' M' ...]" in approval["preview"]
    assert "read_long_tool_output(" in approval["preview"]
    assert "--- STDOUT ---" not in approval["preview"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "git_status_compact"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "[ M] Modified (129):" in tool_msg["content"]
    assert " M src/packages/app/feature/modified_001.py" not in tool_msg["content"]


def test_disabled_policy_keeps_default_git_status_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "off")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "git status --short"},
        content="$ git status --short",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\n" + "\n".join(
        f" M src/packages/app/feature/modified_{idx:03d}.py" for idx in range(1, 10)
    )

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
