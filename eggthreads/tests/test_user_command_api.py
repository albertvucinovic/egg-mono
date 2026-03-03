"""Tests for the user command API functions (execute_bash_command etc.)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

import eggthreads as ts
from eggthreads.api import (
    execute_bash_command,
    execute_bash_command_hidden,
    get_user_command_result,
    wait_for_user_command_result,
    wait_for_user_command_result_async,
    execute_bash_command_async,
)


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    """Create an in‑memory ThreadsDB for testing."""
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_execute_bash_command_creates_tool_call(tmp_path):
    """execute_bash_command should add a user message with a tool call and auto‑approve it."""
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    script = "echo hello"
    tool_call_id = execute_bash_command(db, thread_id, script, hidden=False)

    # Verify the tool call exists and is approved
    states = ts.build_tool_call_states(db, thread_id)
    assert tool_call_id in states
    tc = states[tool_call_id]
    assert tc.parent_role == "user"
    assert tc.approval_decision == "granted"
    assert tc.state == "TC2.1"  # approved, not yet executed
    # Verify the script is stored in the tool call arguments
    # The arguments JSON should be in the original user message event
    # We can fetch the event and inspect.
    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    )
    (payload_json,) = cur.fetchone()
    payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    assert payload["role"] == "user"
    assert len(payload["tool_calls"]) == 1
    tool_call = payload["tool_calls"][0]
    assert tool_call["id"] == tool_call_id
    assert tool_call["function"]["name"] == "bash"
    args = json.loads(tool_call["function"]["arguments"])
    assert args["script"] == script
    # Verify extra fields (merged into payload)
    assert payload.get("user_command_type") == "$"
    assert payload.get("keep_user_turn") is True
    assert "no_api" not in payload  # not hidden


def test_execute_bash_command_hidden(tmp_path):
    """Hidden bash commands should set no_api flag and user_command_type '$$'."""
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    script = "echo secret"
    tool_call_id = execute_bash_command_hidden(db, thread_id, script)

    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    )
    (payload_json,) = cur.fetchone()
    payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    assert payload.get("user_command_type") == "$$"
    assert payload.get("no_api") is True
    # Tool call should still be approved
    states = ts.build_tool_call_states(db, thread_id)
    tc = states[tool_call_id]
    assert tc.approval_decision == "granted"


def test_get_user_command_result_none_before_publication(tmp_path):
    """get_user_command_result returns None when tool call not yet published."""
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    tool_call_id = execute_bash_command(db, thread_id, "echo test")
    result = get_user_command_result(db, thread_id, tool_call_id)
    assert result is None


def test_get_user_command_result_after_publication(tmp_path):
    """get_user_command_result returns content after tool call is published."""
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    tool_call_id = execute_bash_command(db, thread_id, "echo test")

    # Simulate execution and publication: we need to add events that transition to TC6.
    # 1. tool_call.started (optional)
    # 2. tool_call.finished
    db.append_event(
        event_id=f"finished-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": "test output"},
    )
    # 3. tool_call.output_approval
    db.append_event(
        event_id=f"output-approval-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={"tool_call_id": tool_call_id, "decision": "whole", "preview": "test output"},
    )
    # 4. msg.create with role=tool
    db.append_event(
        event_id=f"tool-msg-{tool_call_id}",
        thread_id=thread_id,
        type_="msg.create",
        payload={
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": "test output",
        },
    )

    result = get_user_command_result(db, thread_id, tool_call_id)
    assert result == "test output"


def test_wait_for_user_command_result_success(tmp_path):
    """wait_for_user_command_result returns content after publication."""
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    tool_call_id = execute_bash_command(db, thread_id, "echo test")

    # Publish the tool call result immediately
    db.append_event(
        event_id=f"finished-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": "done"},
    )
    db.append_event(
        event_id=f"output-approval-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={"tool_call_id": tool_call_id, "decision": "whole", "preview": "done"},
    )
    db.append_event(
        event_id=f"tool-msg-{tool_call_id}",
        thread_id=thread_id,
        type_="msg.create",
        payload={
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": "done",
        },
    )

    # Wait should return the result immediately (already published)
    result = wait_for_user_command_result(db, thread_id, tool_call_id, timeout_sec=0.1)
    assert result == "done"


def test_wait_for_user_command_result_timeout(tmp_path):
    """wait_for_user_command_result returns None if timeout expires."""
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    tool_call_id = execute_bash_command(db, thread_id, "echo test")

    result = wait_for_user_command_result(db, thread_id, tool_call_id, timeout_sec=0.1)
    assert result is None


def test_execute_bash_command_async_success(tmp_path):
    """execute_bash_command_async returns result after publication."""
    import asyncio
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    # First create a real tool call and publish its result
    real_tool_call_id = execute_bash_command(db, thread_id, "echo test")
    db.append_event(
        event_id=f"finished-{real_tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": real_tool_call_id, "reason": "success", "output": "async result"},
    )
    db.append_event(
        event_id=f"output-approval-{real_tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={"tool_call_id": real_tool_call_id, "decision": "whole", "preview": "async result"},
    )
    db.append_event(
        event_id=f"tool-msg-{real_tool_call_id}",
        thread_id=thread_id,
        type_="msg.create",
        payload={
            "role": "tool",
            "tool_call_id": real_tool_call_id,
            "content": "async result",
        },
    )

    # Now patch execute_bash_command to return the same ID, so that
    # execute_bash_command_async will wait for that already-published call.
    with patch('eggthreads.api.execute_bash_command', return_value=real_tool_call_id) as mock_exec:
        result = asyncio.run(execute_bash_command_async(db, thread_id, "echo test", timeout_sec=1.0))
        assert result == "async result"
        mock_exec.assert_called_once_with(db, thread_id, "echo test", hidden=False)
def test_execute_bash_command_async_timeout(tmp_path):
    """execute_bash_command_async returns None on timeout."""
    import asyncio
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    # Mock execute_bash_command to return a new ID, but do NOT publish events.
    mock_tool_call_id = "mock-tc-456"
    with patch('eggthreads.api.execute_bash_command', return_value=mock_tool_call_id):
        result = asyncio.run(execute_bash_command_async(db, thread_id, "echo test", timeout_sec=0.01))
        assert result is None
def test_wait_for_user_command_result_async(tmp_path):
    """Async wait function works."""
    import asyncio
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    tool_call_id = execute_bash_command(db, thread_id, "echo test")
    # No publication yet
    result = asyncio.run(wait_for_user_command_result_async(db, thread_id, tool_call_id, timeout_sec=0.01))
    assert result is None

    # Publish result
    db.append_event(
        event_id=f"finished-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": "async wait"},
    )
    db.append_event(
        event_id=f"output-approval-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={"tool_call_id": tool_call_id, "decision": "whole", "preview": "async wait"},
    )
    db.append_event(
        event_id=f"tool-msg-{tool_call_id}",
        thread_id=thread_id,
        type_="msg.create",
        payload={
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": "async wait",
        },
    )

    result = asyncio.run(wait_for_user_command_result_async(db, thread_id, tool_call_id, timeout_sec=1.0))
    assert result == "async wait"
if __name__ == "__main__":
    pytest.main([__file__])
