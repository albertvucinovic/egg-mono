from __future__ import annotations

import asyncio
import json

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


def test_runner_output_policy_stashes_long_output_and_records_channels(tmp_path, monkeypatch):
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
    assert payload["channels"]["artifact"] == payload["artifact_path"]
    assert "Preview only" in payload["preview"]


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
