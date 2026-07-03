from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.output_optimizer.observability import (
    format_output_optimizer_summary,
    optimizer_public_metadata_from_output_approval,
)
from eggthreads.tools import ToolRegistry


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _latest_payload(db: ts.ThreadsDB, thread_id: str, event_type: str, tool_call_id: str | None = None) -> dict:
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


def test_optimizer_public_metadata_uses_existing_output_approval_channels() -> None:
    payload = {
        "tool_call_id": "tc",
        "artifact_path": "/tmp/.egg/egg_outputs/thread/rawabc123",
        "channels": {
            "raw": {"stored_in_finished_event": True},
            "artifact": "/tmp/.egg/egg_outputs/thread/rawabc123",
            "optimizer": {
                "optimized": True,
                "fallback": False,
                "filter_name": "generic",
                "raw_chars": 1000,
                "optimized_chars": 100,
                "published_chars": 120,
                "savings_pct": 90.0,
                "published_savings_pct": 88.0,
                "confidence": 0.8,
                "reason": "generic_output_optimized",
            },
        },
    }

    metadata = optimizer_public_metadata_from_output_approval(payload)

    assert metadata is not None
    assert metadata["optimized"] is True
    assert metadata["summary"] == "Egg optimized · 88% saved · raw available"
    assert metadata["summary_with_artifact"] == "Egg optimized · 88% saved · raw artifact rawabc123"
    assert metadata["artifact_id"] == "rawabc123"
    assert metadata["raw_hint"] == "read_long_tool_output('rawabc123', chunk_number=1)"
    assert metadata["filter_name"] == "generic"
    assert format_output_optimizer_summary(metadata) == "Egg optimized · 88% saved · raw available"
    assert format_output_optimizer_summary(metadata, include_artifact_id=True) == "Egg optimized · 88% saved · raw artifact rawabc123"


def test_optimizer_public_metadata_abstains_for_default_or_fallback_output() -> None:
    assert optimizer_public_metadata_from_output_approval({"channels": {"raw": {"stored_in_finished_event": True}}}) is None
    assert optimizer_public_metadata_from_output_approval({
        "channels": {
            "optimizer": {
                "optimized": False,
                "fallback": True,
                "savings_pct": 0,
            }
        }
    }) is None


def test_runner_publishes_optimizer_metadata_on_tool_message_without_changing_raw_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "on")
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="optimizer-observability")
    tcid = ts.enqueue_user_tool_call(db, thread_id, "repeat", {}, auto_approve=True, hidden=False, content="$ repeat")
    repeated_line = "observable optimizer repeated line " * 8
    raw_output = "start\n" + "\n".join([repeated_line] * 6) + "\ndone"

    tools = ToolRegistry()
    tools.register("repeat", "Repeat", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, thread_id, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True
    approval = _latest_payload(db, thread_id, "tool_call.output_approval", tcid)
    assert approval["channels"]["optimizer"]["optimized"] is True

    assert asyncio.run(runner.run_once()) is True
    state = ts.build_tool_call_states(db, thread_id)[tcid]
    assert state.finished_output == raw_output

    tool_msg = _latest_payload(db, thread_id, "msg.create", tcid)
    assert tool_msg["content"] != raw_output
    assert "[... repeated 5 more times ...]" in tool_msg["content"]
    assert tool_msg["output_optimizer"]["optimized"] is True
    assert tool_msg["output_optimizer"]["summary"].startswith("Egg optimized ·")
    assert tool_msg["output_optimizer"]["raw_available"] is True


def test_runner_omits_optimizer_metadata_for_default_output(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="optimizer-no-clutter")
    tcid = ts.enqueue_user_tool_call(db, thread_id, "echo", {}, auto_approve=True, hidden=False, content="$ echo")

    tools = ToolRegistry()
    tools.register("echo", "Echo", {"type": "object", "properties": {}}, lambda args: "plain output")

    runner = ts.ThreadRunner(db, thread_id, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True
    approval = _latest_payload(db, thread_id, "tool_call.output_approval", tcid)
    assert "optimizer" not in approval.get("channels", {})

    assert asyncio.run(runner.run_once()) is True
    tool_msg = _latest_payload(db, thread_id, "msg.create", tcid)
    assert "output_optimizer" not in tool_msg


def test_output_optimizer_metadata_is_not_sent_to_provider_api(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="optimizer-provider-sanitize")
    runner = ts.ThreadRunner(db, thread_id, llm=object())

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-real-tool",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "optimized preview",
            "tool_call_id": "call-real-tool",
            "output_optimizer": {"optimized": True, "summary": "Egg optimized · 95% saved · raw available"},
        },
        {
            "role": "tool",
            "content": "user command optimized preview",
            "tool_call_id": "call-user-tool",
            "user_tool_call": True,
            "output_optimizer": {"optimized": True, "summary": "Egg optimized · 95% saved · raw available"},
        },
    ]

    sanitized = runner._sanitize_messages_for_api(messages)

    assert all("output_optimizer" not in message for message in sanitized)
    real_tool = next(message for message in sanitized if message.get("role") == "tool")
    assert real_tool["content"] == "optimized preview"
    user_tool = next(message for message in sanitized if message.get("role") == "user")
    assert user_tool["content"] == "user command optimized preview"
