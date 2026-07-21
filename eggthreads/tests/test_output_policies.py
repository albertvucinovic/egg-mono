from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.output_policy import OutputPolicyRequest, create_output_policy_registry, decide_output_publication
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


def test_output_policy_registry_default_gate_matches_default_policy(tmp_path, monkeypatch):
    from eggthreads.builtin_plugins.output_policies import DefaultOutputPolicy

    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    request = OutputPolicyRequest(db=db, thread_id=tid, tool_call_id="tc", output="a\x1b[2Jb")

    registry = create_output_policy_registry()
    decision = decide_output_publication(registry, request)
    default_decision = DefaultOutputPolicy().decide(request)

    assert registry.names() == ["default_output", "native_output_optimizer"]
    assert decision == default_decision
    assert decision.decision == "whole"
    assert decision.preview == "ab"
    assert decision.channels["raw"]["stored_in_finished_event"] is True
    assert decision.channels["llm_message"] == "ab"
    assert "optimizer" not in decision.channels


def test_runner_output_policy_artifacts_long_output_and_read_tool_reads_chunk(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "long", {}, auto_approve=True, hidden=True)

    tools = ToolRegistry()
    tools.register("long", "Long", {"type": "object", "properties": {}}, lambda args: "x" * 120_000)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.output_approval' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["tool_call_id"] == tcid
    assert payload["decision"] == "partial"
    assert payload["artifact_path"]
    artifact_dir = Path(payload["artifact_path"])
    assert artifact_dir.parent == tmp_path / ".egg" / "egg_outputs" / tid
    assert (artifact_dir / "metadata.json").is_file()
    assert (artifact_dir / "chunk-0001.txt").is_file()
    assert payload["channels"]["artifact"] == payload["artifact_path"]
    assert "Preview only" in payload["preview"]
    assert payload["reason"].endswith("stored as artifact")
    assert payload["artifact_path"] not in payload["preview"]
    assert f"Artifact id: {Path(payload['artifact_path']).name}" in payload["preview"]
    assert "read_long_tool_output(" in payload["preview"]
    assert ".egg_outputs" not in payload["preview"]

    read = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_dir.name, "chunk_number": 1},
        thread_id=tid,
        db=db,
    )
    assert f"artifact_id: {artifact_dir.name}" in read
    assert f"owner_thread_id: {tid}" in read
    assert "chunk_number: 1" in read
    assert "total_chunks: 3" in read
    assert read.endswith("x" * 40_000)


def test_assistant_tool_long_output_uses_artifact_before_publication(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = "assistant-long-output"
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tcid,
                    "type": "function",
                    "function": {"name": "long", "arguments": "{}"},
                }
            ]
        },
    )
    ts.approve_tool_calls_for_thread(
        db,
        tid,
        decision="granted",
        tool_call_id=tcid,
    )

    tools = ToolRegistry()
    full_output = "a" * 120_000
    tools.register("long", "Long", {"type": "object", "properties": {}}, lambda args: full_output)
    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)

    assert asyncio.run(runner.run_once()) is True
    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.state == "TC5"
    payload = dict(state.last_output_approval_payload or {})
    assert payload["decision"] == "partial"
    assert Path(payload["artifact_path"]).is_dir()
    assert "read_long_tool_output(" in payload["preview"]

    assert asyncio.run(runner.run_once()) is True
    tool_message = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_message["role"] == "tool"
    assert len(tool_message["content"]) < len(full_output)
    assert "read_long_tool_output(" in tool_message["content"]


def test_runner_artifacts_large_python_stdout(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "python_exec",
        {"script": "import sys; sys.stdout.write('x' * 120_000)", "timeout_sec": 3},
        auto_approve=True,
        hidden=True,
    )

    runner = ts.ThreadRunner(db, tid, llm=object())
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_reason == "success"
    assert state.finished_output is not None
    assert len(state.finished_output) > 120_000

    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.output_approval' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["tool_call_id"] == tcid
    assert payload["decision"] == "partial"
    assert payload["artifact_path"]
    assert "read_long_tool_output(" in payload["preview"]


def test_runner_executes_historical_python_tool_name_with_python_exec(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="legacy-python-tool")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "python",
        {"script": "print('legacy-python-call')"},
        auto_approve=True,
        hidden=True,
    )

    runner = ts.ThreadRunner(db, tid, llm=object())
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_reason == "success"
    assert "legacy-python-call" in (state.finished_output or "")

    assert asyncio.run(runner.run_once()) is True
    tool_message = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_message["name"] == "python_exec"


def test_runner_caps_persisted_tool_output_and_artifact_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("eggthreads.runner.MAX_STORED_TOOL_OUTPUT_CHARS", 50)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHUNK_CHARS", 20)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHAR_THRESHOLD", 10)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "capped", {}, auto_approve=True, hidden=True)

    tools = ToolRegistry()
    tools.register("capped", "Capped", {"type": "object", "properties": {}}, lambda args: "z" * 80)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    states = ts.build_tool_call_states(db, tid)
    assert states[tcid].finished_output == "z" * 50

    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.output_approval' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    payload = json.loads(row[0])
    metadata = json.loads((Path(payload["artifact_path"]) / "metadata.json").read_text())
    assert metadata["capped"] is True
    assert metadata["stored_char_count"] == 50
    assert metadata["original_char_count"] == 80
    assert "Stored output capped at 50 of 80 chars" in payload["preview"]


def test_enabled_optimizer_preserves_long_output_artifact_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "on")
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHAR_THRESHOLD", 1_000)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_LINE_THRESHOLD", 20)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHUNK_CHARS", 500)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHUNK_LINES", 20)
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "long_repeat",
        {},
        auto_approve=True,
        hidden=False,
        content="$ long_repeat",
    )
    line = "long optimizer repeated raw line " * 8
    raw_output = "\n".join([line] * 60)

    tools = ToolRegistry()
    tools.register("long_repeat", "Long repeat", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "partial"
    assert approval["artifact_path"]
    artifact_dir = Path(approval["artifact_path"])
    assert (artifact_dir / "metadata.json").is_file()
    assert (artifact_dir / "chunk-0001.txt").is_file()
    assert approval["channels"]["artifact"] == approval["artifact_path"]
    assert approval["channels"]["optimizer"]["optimized"] is True
    assert approval["channels"]["optimizer"]["fallback"] is False
    assert "[... repeated 59 more times ...]" in approval["preview"]
    assert "Preview only" in approval["preview"]
    assert f"Artifact id: {artifact_dir.name}" in approval["preview"]
    assert "read_long_tool_output(" in approval["preview"]
    assert approval["artifact_path"] not in approval["preview"]

    read = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_dir.name, "chunk_number": 1},
        thread_id=tid,
        db=db,
    )
    assert line in read


def test_output_policy_composition_ignores_abstain() -> None:
    from dataclasses import dataclass

    from eggthreads.output_policy import OutputPolicyRegistry, OutputPublicationDecision

    @dataclass(frozen=True)
    class Policy:
        name: str
        decision: str

        def decide(self, request: OutputPolicyRequest) -> OutputPublicationDecision:
            return OutputPublicationDecision(self.decision, f"preview:{self.name}")

    registry = OutputPolicyRegistry()
    registry.register(Policy("first", "whole"))
    registry.register(Policy("second", "abstain"))

    decision = decide_output_publication(registry, OutputPolicyRequest(db=None, thread_id="t", tool_call_id="tc", output="raw"))

    assert decision.decision == "whole"
    assert decision.preview == "preview:first"


def test_enabled_optimizer_changes_preview_but_preserves_raw_finished_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "repeat", {}, auto_approve=True, hidden=False, content="$ repeat")
    repeated_line = "generic optimizer repeated noise " * 8
    raw_output = "start\n" + "\n".join([repeated_line] * 6) + "\ndone"

    tools = ToolRegistry()
    tools.register("repeat", "Repeat", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] != raw_output
    assert "[... repeated 5 more times ...]" in approval["preview"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["optimized"] is True
    assert optimizer["fallback"] is False
    assert optimizer["filter_name"] == "generic"
    assert optimizer["raw_chars"] == len(raw_output)
    assert optimizer["optimized_chars"] < len(approval["preview"])
    assert optimizer["published_chars"] == len(approval["preview"])
    assert optimizer["savings_pct"] > 0
    assert approval["artifact_path"]
    assert "read_long_tool_output(" in approval["preview"]
    assert approval["channels"]["raw"]["stored_in_finished_event"] is True

    artifact_id = Path(approval["artifact_path"]).name
    read = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 1},
        thread_id=tid,
        db=db,
    )
    assert raw_output in read

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert "[... repeated 5 more times ...]" in tool_msg["content"]
    assert repeated_line + "\n" + repeated_line not in tool_msg["content"]


def test_default_optimizer_bounds_medium_visible_user_command_output(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "python dump-medium-output.py"},
        auto_approve=True,
        hidden=False,
        content="$ python dump-medium-output.py",
    )
    raw_output = "--- STDOUT ---\n" + "\n".join(
        f"medium bash output line {idx:04d} with enough text to exceed the preview bound"
        for idx in range(1, 420)
    )
    assert 8_000 < len(raw_output) < 100_000

    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.finished_output == raw_output

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] != raw_output
    assert "[... omitted " in approval["preview"]
    assert "read_long_tool_output(" in approval["preview"]
    assert approval["artifact_path"]
    optimizer = approval["channels"]["optimizer"]
    assert optimizer["filter_name"] == "generic"
    assert optimizer["metadata"]["bounded"] is True
    assert "bounded_head_tail_fallback" in optimizer["metadata"]["operations"]
    assert optimizer["published_chars"] == len(approval["preview"])

    artifact_id = Path(approval["artifact_path"]).name
    read = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 1},
        thread_id=tid,
        db=db,
    )
    assert "medium bash output line 0001" in read
    read_tail = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 2},
        thread_id=tid,
        db=db,
    )
    assert "medium bash output line 0419" in read_tail

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg.get("user_tool_call") is True
    assert "no_api" not in tool_msg
    assert "$ python dump-medium-output.py" in tool_msg["content"]
    assert "read_long_tool_output(" in tool_msg["content"]


def test_enabled_optimizer_skips_hidden_user_command_no_api(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "yes")
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    line = ("hidden optimizer repeated noise " * 8).strip()
    script = f"for i in 1 2 3 4 5 6; do printf '%s\\n' '{line}'; done"
    tcid = ts.execute_bash_command_hidden(db, tid, script)

    runner = ts.ThreadRunner(db, tid, llm=object())
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert "optimizer" not in approval["channels"]
    assert "[... repeated" not in approval["preview"]
    assert f"{line}\n{line}" in approval["preview"]

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, tid, "msg.create", tcid)
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == tcid
    assert tool_msg.get("no_api") is True
    assert tool_msg.get("keep_user_turn") is True
    assert "output_optimizer" not in tool_msg
    assert "[... repeated" not in tool_msg["content"]
    assert f"{line}\n{line}" in tool_msg["content"]
