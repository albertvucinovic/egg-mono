from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import eggthreads as ts
from eggthreads.runner import ThreadRunner
from eggthreads.tools import ToolRegistry


def _event_rows(db: ts.ThreadsDB, thread_id: str) -> list[tuple[str, dict, str | None]]:
    rows = db.conn.execute(
        "SELECT type, payload_json, invoke_id FROM events WHERE thread_id=? ORDER BY event_seq",
        (thread_id,),
    ).fetchall()
    return [(str(row[0]), json.loads(row[1]), row[2]) for row in rows]


def _payloads(db: ts.ThreadsDB, thread_id: str, event_type: str) -> list[dict]:
    return [
        payload
        for type_, payload, _invoke_id in _event_rows(db, thread_id)
        if type_ == event_type
    ]


def _registry(output: str) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "evidence_output",
        "Phase 10 output evidence",
        {"type": "object", "properties": {}},
        lambda _args: output,
    )
    return tools


def _enqueue(db: ts.ThreadsDB, thread_id: str, *, tool_call_id: str) -> None:
    ts.enqueue_user_tool_call(
        db,
        thread_id,
        "evidence_output",
        {},
        content="$ evidence-output",
        hidden=False,
        tool_call_id=tool_call_id,
    )


@pytest.mark.parametrize(
    ("output", "expected_decision", "expects_artifact"),
    [
        pytest.param("ordinary successful output", "whole", False, id="below-threshold"),
        pytest.param("x" * 120_000, "partial", True, id="above-threshold"),
    ],
)
def test_phase10_healthy_runner_automatically_decides_and_publishes_output(
    tmp_path, monkeypatch, output, expected_decision, expects_artifact
) -> None:
    """Characterize the healthy TC4 -> TC5 -> TC6 automatic path."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "healthy.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 healthy")
    tool_call_id = "phase10-healthy-call"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    runner = ThreadRunner(db, thread_id, llm=object(), tools=_registry(output))

    assert asyncio.run(runner.run_once()) is True
    after_execution = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert after_execution.state == "TC5"
    assert after_execution.finished_reason == "success"
    assert after_execution.finished_output == output
    approval = _payloads(db, thread_id, "tool_call.output_approval")
    assert len(approval) == 1
    assert approval[0]["decision_source"] == "automatic_policy"
    assert approval[0]["decision"] == expected_decision
    assert bool(approval[0]["artifact_path"]) is expects_artifact
    assert ("read_long_tool_output(" in approval[0]["preview"]) is expects_artifact

    assert asyncio.run(runner.run_once()) is True
    final = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert final.state == "TC6"
    tool_messages = [
        payload
        for payload in _payloads(db, thread_id, "msg.create")
        if payload.get("role") == "tool" and payload.get("tool_call_id") == tool_call_id
    ]
    assert len(tool_messages) == 1
    assert ("read_long_tool_output(" in tool_messages[0]["content"]) is expects_artifact


def test_phase10_policy_failure_strands_successful_tc4_across_restart_and_second_scheduler(
    tmp_path, monkeypatch
) -> None:
    """Reproduce the durable post-finish/pre-decision failure window literally."""

    db = ts.ThreadsDB(tmp_path / "stranded.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 stranded")
    tool_call_id = "phase10-stranded-call"
    output = "successful output awaiting automatic publication"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)

    def fail_policy(*_args, **_kwargs):
        raise RuntimeError("phase10 injected policy failure")

    monkeypatch.setattr("eggthreads.output_policy.decide_output_publication", fail_policy)
    first_runner = ThreadRunner(
        db,
        thread_id,
        llm=object(),
        tools=_registry(output),
        owner="egg",
    )

    assert asyncio.run(first_runner.run_once()) is True
    stranded = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert stranded.state == "TC4"
    assert stranded.finished_reason == "success"
    assert stranded.finished_output == output
    assert stranded.owner_invoke_id
    assert stranded.finished_event_seq is not None
    assert ts.thread_state(db, thread_id) == "waiting_output_approval"
    assert ts.discover_runner_actionable(db, thread_id) is None
    assert db.current_open(thread_id) is None
    assert _payloads(db, thread_id, "tool_call.finished") == [
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": output,
            "publication_presentation": {},
        }
    ]
    assert _payloads(db, thread_id, "tool_call.output_approval") == []
    assert not any(
        payload.get("role") == "tool" and payload.get("tool_call_id") == tool_call_id
        for payload in _payloads(db, thread_id, "msg.create")
    )

    # Model process restart plus a separate EggW-owned scheduler connection.
    restarted_db = ts.ThreadsDB(db.path)
    eggw_db = ts.ThreadsDB(db.path)
    try:
        restarted_runner = ThreadRunner(
            restarted_db,
            thread_id,
            llm=object(),
            tools=_registry(output),
            owner="egg-restart",
        )
        eggw_runner = ThreadRunner(
            eggw_db,
            thread_id,
            llm=object(),
            tools=_registry(output),
            owner="eggw",
        )
        assert asyncio.run(restarted_runner.run_once()) is False
        assert asyncio.run(eggw_runner.run_once()) is False
    finally:
        restarted_db.conn.close()
        eggw_db.conn.close()

    after_restart = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert after_restart.state == "TC4"
    assert after_restart.finished_event_seq == stranded.finished_event_seq
    assert ts.discover_runner_actionable(db, thread_id) is None
    assert _payloads(db, thread_id, "tool_call.finished") == [
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": output,
            "publication_presentation": {},
        }
    ]
    assert _payloads(db, thread_id, "tool_call.output_approval") == []


def test_phase10_expired_owner_lease_does_not_make_successful_tc4_recoverable(tmp_path) -> None:
    """Characterize lease takeover: successful TC4 is unlike orphaned TC3."""

    db = ts.ThreadsDB(tmp_path / "expired.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 expired")
    tool_call_id = "phase10-expired-call"
    output = "finished before owner disappeared"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    old_invoke_id = "phase10-old-owner"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert db.try_open_stream(
        thread_id,
        old_invoke_id,
        expired,
        owner="egg",
        purpose="tool",
    )
    writer = db.invocation_writer(thread_id, old_invoke_id)
    # The lease was live at the durable finish in the real crash window; direct
    # append builds the post-crash observation after that lease has expired.
    db.append_event(
        "phase10-finished-before-crash",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=old_invoke_id,
    )
    db.append_event(
        "phase10-durable-finish",
        thread_id,
        "tool_call.finished",
        {"tool_call_id": tool_call_id, "reason": "success", "output": output},
        invoke_id=old_invoke_id,
    )

    with pytest.raises(ts.LeaseLost):
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="whole",
            source="automatic_policy",
            expected_event_seq=ts.build_tool_call_states(db, thread_id)[
                tool_call_id
            ].state_event_seq,
            invocation_writer=writer,
        )
    assert ts.discover_runner_actionable(db, thread_id) is None

    contender = ts.ThreadsDB(db.path)
    try:
        contender_runner = ThreadRunner(
            contender,
            thread_id,
            llm=object(),
            owner="eggw",
        )
        assert asyncio.run(contender_runner.run_once()) is False
    finally:
        contender.conn.close()

    final = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert final.state == "TC4"
    assert final.finished_reason == "success"
    assert _payloads(db, thread_id, "tool_call.output_approval") == []
    # Since TC4 is not actionable, the contender never even attempts lease
    # takeover; the expired row and durable output remain untouched.
    assert _payloads(db, thread_id, "control.interrupt") == []
    assert db.current_open(thread_id)["invoke_id"] == old_invoke_id
