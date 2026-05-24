from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.output_policy import OutputPolicyRequest, create_output_policy_registry, decide_output_publication
from eggthreads.tools import ToolRegistry


def test_output_policy_registry_default_policy(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    registry = create_output_policy_registry()
    decision = decide_output_publication(
        registry,
        OutputPolicyRequest(db=db, thread_id=tid, tool_call_id="tc", output="a\x1b[2Jb"),
    )

    assert registry.names() == ["default_output"]
    assert decision.decision == "whole"
    assert decision.preview == "ab"
    assert decision.channels["raw"]["stored_in_finished_event"] is True
    assert decision.channels["llm_message"] == "ab"


def test_runner_output_policy_artifacts_long_output_and_read_tool_reads_chunk(tmp_path, monkeypatch):
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


def test_runner_caps_persisted_tool_output_and_artifact_metadata(tmp_path, monkeypatch):
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
