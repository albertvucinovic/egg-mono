from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.output_optimizer import GitDiffCompactFilter, OptimizeRequest, OutputOptimizer
from eggthreads.tools import ToolRegistry


def _file_diff(path: str, *, hunk_line: int = 1, pairs: int = 3, context: int = 8) -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -{hunk_line},{pairs + context} +{hunk_line},{pairs + context} @@",
    ]
    for idx in range(1, context + 1):
        lines.append(f" context line {idx} with lots of unchanged surrounding detail for {path}")
    for idx in range(1, pairs + 1):
        lines.append(f"-old value {idx} from {path}")
        lines.append(f"+new value {idx} from {path}")
    return "\n".join(lines)


def _request(output: str, *, script: str = "git diff") -> OptimizeRequest:
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


def test_git_diff_filter_compacts_unified_diff_and_reports_caps() -> None:
    paths = [
        "src/packages/app/feature_a/module_alpha.py",
        "src/packages/app/feature_b/module_beta.py",
        "src/packages/app/feature_c/module_gamma.py",
    ]
    raw = "\n".join(_file_diff(path, pairs=3, context=10) for path in paths)

    decision = OutputOptimizer(
        [GitDiffCompactFilter(max_files=2, max_changed_lines_per_hunk=2, max_changed_lines_total=4)]
    ).optimize(_request(raw, script="git diff -- src/packages/app"))

    assert decision.optimized is True
    assert decision.filter_name == "git_diff_compact"
    assert decision.output == "\n".join(
        [
            "diff --git src/packages/app/feature_a/module_alpha.py",
            "  @@ -1,13 +1,13 @@",
            "    -old value 1 from src/packages/app/feature_a/module_alpha.py",
            "    +new value 1 from src/packages/app/feature_a/module_alpha.py",
            "    [... omitted 4 more changed lines in this hunk ...]",
            "",
            "diff --git src/packages/app/feature_b/module_beta.py",
            "  @@ -1,13 +1,13 @@",
            "    -old value 1 from src/packages/app/feature_b/module_beta.py",
            "    +new value 1 from src/packages/app/feature_b/module_beta.py",
            "    [... omitted 4 more changed lines in this hunk ...]",
            "",
            "[... omitted 1 more files / 1 hunks / 6 changed lines due to cap ...]",
        ]
    )
    assert decision.metadata["file_count"] == 3
    assert decision.metadata["hunk_count"] == 3
    assert decision.metadata["changed_line_count"] == 18
    assert decision.metadata["emitted_files"] == 2
    assert decision.metadata["emitted_hunks"] == 2
    assert decision.metadata["emitted_changed_lines"] == 4
    assert decision.metadata["omitted_files"] == 1
    assert decision.metadata["omitted_changed_lines"] == 14
    assert decision.savings_pct > 0


def test_git_diff_filter_accepts_direct_git_metadata_and_stdout_header() -> None:
    raw = "--- STDOUT ---\n" + _file_diff("src/packages/app/feature/module.py", pairs=8, context=12)

    decision = OutputOptimizer([GitDiffCompactFilter(max_changed_lines_per_hunk=4)]).optimize(
        OptimizeRequest(tool_name="git", tool_args={"args": ["diff", "--", "src/packages/app"]}, output=raw)
    )

    assert decision.optimized is True
    assert decision.output.startswith("diff --git src/packages/app/feature/module.py\n  @@ -1,20 +1,20 @@")
    assert "--- STDOUT ---" not in decision.output
    assert "[... omitted 12 more changed lines in this hunk ...]" in decision.output
    assert decision.metadata["original_had_stdout_header"] is True

    direct_diff_tool = OutputOptimizer([GitDiffCompactFilter(max_changed_lines_per_hunk=4)]).optimize(
        OptimizeRequest(tool_name="git_diff", output=_file_diff("src/direct/tool.py", pairs=8, context=12))
    )
    assert direct_diff_tool.optimized is True


def test_git_diff_filter_abstains_on_non_git_or_malformed_patch_like_text() -> None:
    patch_like = _file_diff("src/packages/app/feature/module.py", pairs=8, context=12)

    non_git = OutputOptimizer([GitDiffCompactFilter()]).optimize(_request(patch_like, script="cat patch.diff"))
    assert non_git.optimized is False
    assert non_git.output == patch_like

    malformed = "\n".join(
        [
            "diff --git a/src/a.py b/src/a.py",
            "index 1111111..2222222 100644",
            "--- a/src/a.py",
            "+++ b/src/a.py",
            "-old without hunk",
            "+new without hunk",
        ]
    )
    malformed_decision = OutputOptimizer([GitDiffCompactFilter()]).optimize(_request(malformed))
    assert malformed_decision.optimized is False
    assert malformed_decision.output == malformed

    ambiguous = "diff --git is mentioned in prose\nnot a unified diff"
    ambiguous_decision = OutputOptimizer([GitDiffCompactFilter()]).optimize(_request(ambiguous))
    assert ambiguous_decision.optimized is False
    assert ambiguous_decision.output == ambiguous


def test_enabled_policy_uses_git_diff_filter_and_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "git diff -- src/packages/app"},
        content="$ git diff -- src/packages/app",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\n" + "\n".join(
        [
            _file_diff("src/packages/app/feature_a/module_alpha.py", pairs=8, context=12),
            _file_diff("src/packages/app/feature_b/module_beta.py", pairs=8, context=12),
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
    assert approval["preview"] != raw_output
    assert approval["preview"].startswith("diff --git src/packages/app/feature_a/module_alpha.py")
    assert "--- STDOUT ---" not in approval["preview"]
    assert "context line 1 with lots of unchanged" not in approval["preview"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "git_diff_compact"
    assert optimizer["optimized"] is True
    assert optimizer["raw_chars"] == len(raw_output)

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "diff --git src/packages/app/feature_a/module_alpha.py" in tool_msg["content"]


def test_disabled_policy_keeps_default_git_diff_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "off")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "git diff"},
        content="$ git diff",
        auto_approve=True,
        hidden=False,
    )
    raw_output = "--- STDOUT ---\n" + _file_diff("src/packages/app/feature/module.py", pairs=8, context=12)

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]
