"""
Integration tests for eggw backend API.

Tests streaming, tool approval, and tool execution flows.
Run with: pytest test_api.py -v
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi.testclient import TestClient
from eggw.core import state as core_state


# Fixture to create a test database and app instance
@pytest.fixture
def test_db_path(tmp_path):
    """Create a temporary database path."""
    db_path = tmp_path / ".egg" / "threads.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


@pytest.fixture
def app(test_db_path, monkeypatch):
    """Create a test app instance with isolated database."""
    # Set environment to use test database
    monkeypatch.setenv("EGG_DB_PATH", test_db_path)

    # Import main after setting env - force reimport
    if "eggw.main" in sys.modules:
        del sys.modules["eggw.main"]
    from eggw import main

    # Reset global state in core.state (routes use core.db which delegates to core.state.db)
    core_state.db = None
    core_state.active_schedulers = {}

    # Initialize database with check_same_thread=False for testing
    from eggthreads import ThreadsDB
    # Create DB connection that allows multi-threaded access
    conn = sqlite3.connect(test_db_path, check_same_thread=False, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")

    # Create ThreadsDB and replace its connection
    core_state.db = ThreadsDB.__new__(ThreadsDB)
    core_state.db.path = Path(test_db_path)
    core_state.db.conn = conn
    core_state.db.init_schema()

    return main.app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestHealthAndBasics:
    """Test basic API endpoints."""

    def test_health_endpoint(self, client):
        """Test health check returns OK."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["db_connected"] is True

    def test_list_threads_empty(self, client):
        """Test listing threads when none exist."""
        response = client.get("/api/threads")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestThreadOperations:
    """Test thread CRUD operations."""

    def test_create_thread(self, client):
        """Test creating a new thread."""
        response = client.post("/api/threads", json={"name": "Test Thread"})
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert len(data["id"]) > 0
        return data["id"]

    def test_get_thread(self, client):
        """Test getting thread details."""
        # Create thread first
        create_resp = client.post("/api/threads", json={"name": "Test Thread"})
        thread_id = create_resp.json()["id"]

        # Get thread
        response = client.get(f"/api/threads/{thread_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == thread_id

    def test_get_thread_state(self, client):
        """Test getting thread state."""
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Test Thread"})
        thread_id = create_resp.json()["id"]

        # Get state
        response = client.get(f"/api/threads/{thread_id}/state")
        assert response.status_code == 200
        data = response.json()
        assert "state" in data
        assert data["state"] == "waiting_user"  # New thread waits for user

    def test_get_thread_state_includes_stream_kind(self, client):
        """Thread state exposes active stream purpose for web UI polling."""
        create_resp = client.post("/api/threads", json={"name": "Streaming Tool Test"})
        thread_id = create_resp.json()["id"]

        invoke_id = "invoke-tool-state"
        assert core_state.db.try_open_stream(
            thread_id,
            invoke_id,
            "2999-01-01 00:00:00",
            owner="test",
            purpose="tool",
        )

        response = client.get(f"/api/threads/{thread_id}/state")
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "running"
        assert data["streaming_kind"] == "tool"
        assert data["streaming_invoke_id"] == invoke_id

    def test_get_user_wait_state_settings_and_normal_answer_submission(self, client):
        """EggW exposes active get-user waits and answers via normal messages."""
        from eggthreads import append_message, create_snapshot

        get_user_tool_name = "get_user_message_while_preserving_llm_turn"
        create_resp = client.post("/api/threads", json={"name": "Get User Wait"})
        thread_id = create_resp.json()["id"]
        invoke_id = "invoke-get-user-web"
        tool_call_id = "call-get-user-web"
        note = "What should I do next?"

        assert core_state.db.try_open_stream(
            thread_id,
            invoke_id,
            "2999-01-01 00:00:00",
            owner="test",
            purpose="tool",
        )
        append_message(
            core_state.db,
            thread_id,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": get_user_tool_name,
                            "arguments": json.dumps({"assistant_note": note}),
                        },
                    }
                ]
            },
        )
        core_state.db.append_event(
            event_id="started-get-user-web",
            thread_id=thread_id,
            type_="tool_call.execution_started",
            invoke_id=invoke_id,
            payload={"tool_call_id": tool_call_id},
        )
        note_msg_id = append_message(
            core_state.db,
            thread_id,
            "assistant",
            note,
            extra={
                "answer_user_preserve_turn": True,
                "source_tool_name": get_user_tool_name,
                "tool_call_id": tool_call_id,
                "awaiting_user_message_tool_call_id": tool_call_id,
            },
        )
        create_snapshot(core_state.db, thread_id)

        state_response = client.get(f"/api/threads/{thread_id}/state")
        settings_response = client.get(f"/api/threads/{thread_id}/settings")

        assert state_response.status_code == 200
        state = state_response.json()
        assert state["state"] == "waiting_user"
        assert state["streaming_kind"] == "tool"
        assert state["streaming_invoke_id"] == invoke_id
        assert state["active_get_user_wait"] is True
        assert state["get_user_waiting_note"]["msg_id"] == note_msg_id
        assert state["get_user_waiting_note"]["tool_call_id"] == tool_call_id
        assert state["get_user_waiting_note"]["content"] == note

        assert settings_response.status_code == 200
        settings = settings_response.json()
        assert settings["active_get_user_wait"] is True
        assert settings["get_user_waiting_note"]["content"] == note

        answer_response = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"content": "Continue with the next slice."},
        )

        assert answer_response.status_code == 200
        answer_msg_id = answer_response.json()["message_id"]
        row = core_state.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND msg_id=? AND type='msg.create'",
            (thread_id, answer_msg_id),
        ).fetchone()
        payload = json.loads(row[0])
        assert payload["role"] == "user"
        assert payload["content"] == "Continue with the next slice."
        assert payload.get("keep_user_turn") is not True
        assert payload.get("no_api") is not True

        state_after_answer = client.get(f"/api/threads/{thread_id}/state").json()
        assert state_after_answer["active_get_user_wait"] is False

    def test_interrupt_cancels_active_get_user_wait_with_tool_result(self, client):
        """Web interrupt closes an active get-user wait with preserved turn semantics."""
        from eggthreads import append_message, build_tool_call_states, create_snapshot

        get_user_tool_name = "get_user_message_while_preserving_llm_turn"
        create_resp = client.post("/api/threads", json={"name": "Cancel Get User Wait"})
        thread_id = create_resp.json()["id"]
        invoke_id = "invoke-get-user-cancel-web"
        tool_call_id = "call-get-user-cancel-web"
        interrupted = "User interrupted get_user_message_while_preserving_llm_turn."

        assert core_state.db.try_open_stream(
            thread_id,
            invoke_id,
            "2999-01-01 00:00:00",
            owner="test",
            purpose="tool",
        )
        append_message(
            core_state.db,
            thread_id,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": get_user_tool_name,
                            "arguments": json.dumps({"assistant_note": "Need user input"}),
                        },
                    }
                ]
            },
        )
        core_state.db.append_event(
            event_id="started-get-user-cancel-web",
            thread_id=thread_id,
            type_="tool_call.execution_started",
            invoke_id=invoke_id,
            payload={"tool_call_id": tool_call_id},
        )
        append_message(
            core_state.db,
            thread_id,
            "assistant",
            "Need user input",
            extra={
                "answer_user_preserve_turn": True,
                "source_tool_name": get_user_tool_name,
                "tool_call_id": tool_call_id,
                "awaiting_user_message_tool_call_id": tool_call_id,
            },
        )
        create_snapshot(core_state.db, thread_id)

        assert client.get(f"/api/threads/{thread_id}/state").json()["active_get_user_wait"] is True

        response = client.post(f"/api/threads/{thread_id}/interrupt")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "interrupted"
        assert body["invoke_id"] == invoke_id
        assert body["get_user_cancelled"] is True
        assert core_state.db.current_open(thread_id) is None

        state = client.get(f"/api/threads/{thread_id}/state").json()
        assert state["active_get_user_wait"] is False

        rows = core_state.db.conn.execute(
            "SELECT type, payload_json FROM events WHERE thread_id=? ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
        payloads_by_type = [(row["type"], json.loads(row["payload_json"])) for row in rows]
        approvals = [payload for typ, payload in payloads_by_type if typ == "tool_call.output_approval"]
        assert approvals[-1]["tool_call_id"] == tool_call_id
        assert approvals[-1]["decision"] == "whole"
        assert approvals[-1]["preview"] == interrupted

        tool_messages = [payload for typ, payload in payloads_by_type if typ == "msg.create" and payload.get("role") == "tool"]
        assert len(tool_messages) == 1
        tool_msg = tool_messages[0]
        assert tool_msg["content"] == interrupted
        assert tool_msg["tool_call_id"] == tool_call_id
        assert tool_msg["name"] == get_user_tool_name
        assert tool_msg["keep_user_turn"] is True

        tc = build_tool_call_states(core_state.db, thread_id)[tool_call_id]
        assert tc.published is True
        assert tc.state == "TC6"


class TestMessageOperations:
    """Test message sending and retrieval."""

    def test_send_message(self, client):
        """Test sending a user message."""
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Test Thread"})
        thread_id = create_resp.json()["id"]

        # Send message
        response = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"content": "Hello, world!"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "sent"
        assert "message_id" in data

    def test_get_messages(self, client):
        """Test retrieving messages."""
        # Create thread and send message
        create_resp = client.post("/api/threads", json={"name": "Test Thread"})
        thread_id = create_resp.json()["id"]

        client.post(
            f"/api/threads/{thread_id}/messages",
            json={"content": "Test message"}
        )

        # Get messages
        response = client.get(f"/api/threads/{thread_id}/messages")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(m["content"] == "Test message" for m in data)

    def test_get_messages_returns_full_message_ids_for_copyable_ui(self, client):
        """Web transcript API exposes full ids used by /compact and /continue."""
        create_resp = client.post("/api/threads", json={"name": "Message IDs"})
        thread_id = create_resp.json()["id"]

        send_resp = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"content": "Copy my id"},
        )
        msg_id = send_resp.json()["message_id"]

        response = client.get(f"/api/threads/{thread_id}/messages")

        assert response.status_code == 200
        data = response.json()
        message = next(m for m in data if m.get("content") == "Copy my id")
        assert message["id"] == msg_id
        assert len(message["id"]) > 8

    def test_get_messages_includes_compaction_marker_and_full_history(self, client):
        """Web transcript API returns a divider marker without hiding old messages."""
        from eggthreads import append_message, commit_thread_compaction, create_snapshot

        create_resp = client.post("/api/threads", json={"name": "Compaction UI"})
        thread_id = create_resp.json()["id"]

        old = append_message(core_state.db, thread_id, "user", "old visible history")
        start = append_message(core_state.db, thread_id, "assistant", "compact summary")
        after = append_message(core_state.db, thread_id, "user", "new question")
        commit_thread_compaction(core_state.db, thread_id, start, created_by="test")
        create_snapshot(core_state.db, thread_id)

        response = client.get(f"/api/threads/{thread_id}/messages")

        assert response.status_code == 200
        data = response.json()
        contents = [m.get("content") for m in data]
        assert "old visible history" in contents
        assert "compact summary" in contents
        assert "new question" in contents
        markers = [m for m in data if m.get("kind") == "compaction_marker"]
        assert len(markers) == 1
        marker = markers[0]
        assert marker["role"] == "compaction_marker"
        assert marker["start_msg_id"] == start
        assert marker["start_event_seq"] is not None
        assert "Compaction boundary: API context now starts at msg_" in marker["content"]
        assert start[-8:] in marker["content"]
        assert [m.get("id") for m in data].index(old) < data.index(marker) < [m.get("id") for m in data].index(start)
        assert after

    def test_get_messages_marks_answer_user_preserve_turn_notes(self, client):
        """Web transcript API exposes assistant-note metadata for frontend styling."""
        from eggthreads import append_message, create_snapshot

        create_resp = client.post("/api/threads", json={"name": "Assistant Note UI"})
        thread_id = create_resp.json()["id"]

        note = append_message(
            core_state.db,
            thread_id,
            "assistant",
            "**Interim** note",
            extra={"answer_user_preserve_turn": True},
        )
        create_snapshot(core_state.db, thread_id)

        response = client.get(f"/api/threads/{thread_id}/messages")

        assert response.status_code == 200
        data = response.json()
        message = next(m for m in data if m["id"] == note)
        assert message["role"] == "assistant"
        assert message["answer_user_preserve_turn"] is True

    def test_web_continue_appends_recovery_notice(self, client):
        """Eggw /continue persists a local recovery notice after success."""
        from eggthreads import append_message

        create_resp = client.post("/api/threads", json={"name": "Continue Notice"})
        thread_id = create_resp.json()["id"]

        user_msg_id = append_message(core_state.db, thread_id, "user", "Hello")
        append_message(core_state.db, thread_id, "assistant", "Partial answer")
        append_message(core_state.db, thread_id, "system", "LLM/runner error: provider exploded")

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/continue {user_msg_id}"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["skipped_count"] == 2

        rows = core_state.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
        payloads = [json.loads(row[0]) for row in rows]
        notices = [payload for payload in payloads if payload.get("recovery_notice")]
        assert len(notices) == 1
        notice = notices[0]
        assert notice["role"] == "system"
        assert notice["no_api"] is True
        assert notice["preserve_on_continue"] is True
        assert "manual /continue" in notice["content"]
        assert "Previous error: LLM/runner error: provider exploded" in notice["content"]

        messages_resp = client.get(f"/api/threads/{thread_id}/messages")
        assert messages_resp.status_code == 200
        notice_messages = [msg for msg in messages_resp.json() if "manual /continue" in str(msg.get("content"))]
        assert notice_messages
        assert notice_messages[0]["recovery_notice"] is True


class TestEventStreaming:
    """Test SSE event shaping for streaming UI clients."""

    def test_sse_replays_active_tool_stream_with_preview_limit_indicator(self, client):
        """An eggw client joining mid-tool-stream sees preview + suppressed event."""
        pytest.skip("SSE TestClient stream hangs in CI; needs real-server SSE test")
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Tool Stream"})
        thread_id = create_resp.json()["id"]
        invoke_id = "invoke-tool-sse"

        # Simulate an active tool stream. The SSE endpoint should start from
        # stream.open and replay the current stream so a joining web client can
        # reconstruct the same limited preview the TUI shows.
        assert core_state.db.try_open_stream(
            thread_id,
            invoke_id,
            "2999-01-01 00:00:00",
            owner="test",
            purpose="tool",
        )
        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="stream.open",
            msg_id=os.urandom(10).hex(),
            invoke_id=invoke_id,
            payload={"stream_kind": "tool", "model_key": "test-model"},
        )
        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="stream.delta",
            invoke_id=invoke_id,
            chunk_seq=1,
            payload={"tool": {"id": "tc1", "name": "bash", "text": "preview"}},
        )
        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="stream.delta",
            invoke_id=invoke_id,
            chunk_seq=2,
            payload={"tool": {"id": "tc1", "name": "bash", "suppressed": True}},
        )

        with client.stream("GET", f"/api/threads/{thread_id}/events") as response:
            assert response.status_code == 200
            lines = []
            for line in response.iter_lines():
                if line:
                    lines.append(line)
                if sum(1 for line in lines if line.startswith("data: ")) >= 3:
                    break

        data_events = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")]
        assert [event["event_type"] for event in data_events] == [
            "stream.open",
            "stream.delta",
            "stream.delta",
        ]
        assert data_events[0]["payload"]["stream_kind"] == "tool"
        assert data_events[1]["payload"]["tool"]["text"] == "preview"
        assert data_events[2]["payload"]["tool"]["suppressed"] is True


class TestToolCalls:
    """Test tool call states and approval."""

    def test_get_tool_calls_empty(self, client):
        """Test getting tool calls when none exist."""
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Test Thread"})
        thread_id = create_resp.json()["id"]

        response = client.get(f"/api/threads/{thread_id}/tools")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_tool_approval_flow(self, client, test_db_path):
        """Test the full tool approval flow."""
        from eggthreads import ThreadsDB, append_message

        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Tool Test"})
        thread_id = create_resp.json()["id"]

        # Simulate an assistant message with a tool call
        # This mimics what the LLM would produce
        tool_call_id = "test_tc_001"
        assistant_msg = {
            "role": "assistant",
            "content": "Let me check that for you.",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "test_tool",
                        "arguments": '{"query": "test"}'
                    }
                }
            ]
        }

        # Append the assistant message with tool call directly to DB
        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="msg.create",
            msg_id=os.urandom(10).hex(),
            payload=assistant_msg,
        )

        # Now check tool calls - should be in TC1 state (needs approval)
        response = client.get(f"/api/threads/{thread_id}/tools")
        assert response.status_code == 200
        tools = response.json()
        assert len(tools) == 1
        assert tools[0]["id"] == tool_call_id
        assert tools[0]["state"] == "TC1"  # Needs execution approval
        assert tools[0]["name"] == "test_tool"

        # Approve the tool
        response = client.post(
            f"/api/threads/{thread_id}/tools/approve",
            json={"tool_call_id": tool_call_id, "approved": True}
        )
        assert response.status_code == 200

        # Check tool state changed
        response = client.get(f"/api/threads/{thread_id}/tools")
        tools = response.json()
        # After approval, state should advance (TC2.1 = approved, waiting execution)
        assert len(tools) == 1
        assert tools[0]["state"] in ["TC2.1", "TC3", "TC4", "TC5"]  # Advanced from TC1


class TestAutoApproval:
    """Test auto-approval toggle via command."""

    def test_toggle_auto_approval(self, client):
        """Test enabling and disabling auto-approval via /toggleAutoApproval command."""
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Auto Test"})
        thread_id = create_resp.json()["id"]

        # Get initial settings
        response = client.get(f"/api/threads/{thread_id}/settings")
        assert response.status_code == 200
        initial = response.json()
        initial_state = initial.get("auto_approval", False)

        # Toggle auto-approval via command
        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/toggleAutoApproval"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["auto_approval"] != initial_state  # Should toggle

        # Toggle again
        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/toggleAutoApproval"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["auto_approval"] == initial_state  # Back to initial


class TestAutoContinueOnError:
    """Test auto-continue-on-error settings via command."""

    def test_toggle_auto_continue_on_error(self, client):
        from eggthreads import create_child_thread, get_thread_recovery

        create_resp = client.post("/api/threads", json={"name": "Recovery Toggle"})
        thread_id = create_resp.json()["id"]
        child_id = create_child_thread(core_state.db, thread_id, "Recovery Toggle Child")

        response = client.get(f"/api/threads/{thread_id}/settings")
        assert response.status_code == 200
        assert response.json()["autoContinueOnError"] is True
        assert get_thread_recovery(core_state.db, child_id).auto_continue_on_error is True

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/toggleAutoContinueOnError"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["autoContinueOnError"] is False
        assert get_thread_recovery(core_state.db, child_id).auto_continue_on_error is False

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/toggleAutoContinueOnError on"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["autoContinueOnError"] is True
        assert get_thread_recovery(core_state.db, child_id).auto_continue_on_error is True

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/toggleAutoContinueOnError nope"},
        )
        assert response.status_code == 200
        assert response.json()["success"] is False


class TestTokenStats:
    """Test token statistics endpoint."""

    def test_get_stats(self, client):
        """Test getting token stats for a thread."""
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Stats Test"})
        thread_id = create_resp.json()["id"]

        # Get stats
        response = client.get(f"/api/threads/{thread_id}/stats")
        assert response.status_code == 200
        data = response.json()
        assert "input_tokens" in data
        assert "output_tokens" in data
        assert "total_tokens" in data
        assert "context_tokens" in data
        assert "full_thread_tokens" in data

    def test_stats_exposes_precise_usage_fields(self, client, monkeypatch):
        """Stats keeps legacy fields and adds detailed usage/cost fields."""
        create_resp = client.post("/api/threads", json={"name": "Detailed Stats"})
        thread_id = create_resp.json()["id"]

        def fake_thread_token_stats(db, tid, llm=None):
            assert tid == thread_id
            return {
                "context_tokens": 10,
                "full_thread_tokens": 30,
                "api_usage": {
                    "total_input_tokens": 20,
                    "cached_input_tokens": 5,
                    "cache_creation_input_tokens": 3,
                    "cache_creation_5m_input_tokens": 1,
                    "cache_creation_1h_input_tokens": 2,
                    "cached_tokens": 12,
                    "total_output_tokens": 7,
                    "total_reasoning_tokens": 2,
                    "approx_call_count": 4,
                    "actual_call_count": 1,
                    "estimated_call_count": 3,
                    "api_confirmed_usage": {
                        "actual_call_count": 1,
                        "total_input_tokens": 20,
                        "cached_input_tokens": 5,
                        "total_output_tokens": 7,
                        "field_call_counts": {
                            "total_input_tokens": 1,
                            "cached_input_tokens": 1,
                            "total_output_tokens": 1,
                        },
                    },
                    "by_model": {"test-model": {"total_input_tokens": 20}},
                    "cost_usd": {
                        "total": 0.1234,
                        "by_model": {"test-model": {"total": 0.1234}},
                        "warnings": ["note"],
                    },
                },
                "api_usage_since_compaction": {
                    "total_input_tokens": 9,
                    "cached_input_tokens": 1,
                    "total_output_tokens": 2,
                    "approx_call_count": 1,
                    "actual_call_count": 0,
                    "estimated_call_count": 1,
                },
            }

        monkeypatch.setattr("eggw.routes.stats.thread_token_stats", fake_thread_token_stats)

        response = client.get(f"/api/threads/{thread_id}/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["input_tokens"] == 20
        assert data["output_tokens"] == 7
        assert data["cached_tokens"] == 5
        assert data["context_tokens"] == 10
        assert data["full_thread_tokens"] == 30
        assert data["current_provider_context_tokens"] == 10
        assert data["full_thread_context_tokens"] == 30
        assert data["compacted_away_tokens"] == 20
        assert data["cached_input_tokens"] == 5
        assert data["cached_tokens_last"] == 12
        assert data["cached_input_hit_rate"] == 25.0
        assert data["cache_creation_input_tokens"] == 3
        assert data["cache_creation_5m_input_tokens"] == 1
        assert data["cache_creation_1h_input_tokens"] == 2
        assert data["approx_call_count"] == 4
        assert data["actual_call_count"] == 1
        assert data["estimated_call_count"] == 3
        assert data["cost_usd"] == 0.1234
        assert data["cost_total_usd"] == 0.1234
        assert data["cost_warnings"] == ["note"]
        assert data["api_confirmed_usage"]["actual_call_count"] == 1
        assert data["api_usage"]["cache_creation_input_tokens"] == 3
        assert data["api_usage_since_compaction"]["total_input_tokens"] == 9
        assert data["by_model"] == {"test-model": {"total_input_tokens": 20}}

    def test_stats_reports_live_llm_tps_for_active_stream(self, client, monkeypatch):
        """Stats route can compute live TPS for an active LLM stream."""
        create_resp = client.post("/api/threads", json={"name": "Live TPS"})
        thread_id = create_resp.json()["id"]
        invoke_id = "invoke-live-tps"

        assert core_state.db.try_open_stream(
            thread_id,
            invoke_id,
            "2999-01-01 00:00:00",
            owner="test",
            purpose="llm",
        )
        monkeypatch.setattr("eggw.routes.stats.live_llm_tps_for_invoke", lambda db, inv: 12.5)

        response = client.get(f"/api/threads/{thread_id}/stats")

        assert response.status_code == 200
        assert response.json()["streaming_tps"] == 12.5


class TestSSEEvents:
    """Test Server-Sent Events streaming."""

    @pytest.mark.skip(reason="SSE test hangs with TestClient - needs real server")
    def test_sse_endpoint_exists(self, client):
        """Test SSE endpoint is accessible."""
        # SSE tests require a real running server, not TestClient
        # This is better tested in E2E tests with Playwright
        pass


class TestCommands:
    """Test slash commands."""

    def test_cost_command_matches_shared_diagnostics_format(self, client, monkeypatch):
        """EggW /cost uses the shared rich diagnostics formatter."""
        create_resp = client.post("/api/threads", json={"name": "Cost Command"})
        thread_id = create_resp.json()["id"]

        def fake_thread_token_stats(db, tid, llm=None):
            assert tid == thread_id
            return {
                "context_tokens": 10,
                "full_thread_tokens": 30,
                "api_usage": {
                    "total_input_tokens": 5,
                    "cached_input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cached_tokens": 5,
                    "total_output_tokens": 2,
                    "approx_call_count": 1,
                    "actual_call_count": 1,
                    "estimated_call_count": 0,
                    "api_confirmed_usage": {
                        "actual_call_count": 1,
                        "total_input_tokens": 5,
                        "cached_input_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "total_output_tokens": 2,
                        "field_call_counts": {
                            "total_input_tokens": 1,
                            "cached_input_tokens": 1,
                            "cache_creation_input_tokens": 1,
                            "total_output_tokens": 1,
                        },
                    },
                    "by_model": {
                        "test-model": {
                            "total_input_tokens": 5,
                            "cached_input_tokens": 1,
                            "cache_creation_input_tokens": 2,
                            "total_output_tokens": 2,
                            "approx_call_count": 1,
                            "actual_call_count": 1,
                            "estimated_call_count": 0,
                        }
                    },
                    "cost_usd": {
                        "total": 0.10,
                        "by_model": {
                            "test-model": {
                                "input": 0.01,
                                "cached": 0.02,
                                "cache_creation": 0.03,
                                "output": 0.04,
                                "total": 0.10,
                            }
                        },
                    },
                },
                "api_usage_since_compaction": {
                    "total_input_tokens": 3,
                    "cached_input_tokens": 1,
                    "total_output_tokens": 2,
                    "approx_call_count": 1,
                    "actual_call_count": 0,
                    "estimated_call_count": 1,
                    "api_confirmed_usage": {"actual_call_count": 0, "field_call_counts": {}},
                },
            }

        monkeypatch.setattr("eggw.commands.utility.thread_token_stats", fake_thread_token_stats)

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/cost"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        cost_text = data["message"]
        assert "full_thread_context_tokens:       30" in cost_text
        assert "current_provider_context_tokens:  10" in cost_text
        assert "compacted_away_tokens:            20" in cost_text
        assert "Full context usage (full effective history):" in cost_text
        assert "Current provider context usage (after last compaction):" in cost_text
        assert "cached_input_hit_rate: 20.0%" in cost_text
        assert "actual_call_count:     1 API-confirmed" in cost_text
        assert "estimated_call_count:  0" in cost_text
        assert "API-confirmed usage:" in cost_text
        assert "input_tokens: 5" in cost_text
        assert "cached_input_tokens: 1" in cost_text
        assert "cache_creation_input_tokens: 2" in cost_text
        assert "actual_call_count:     0 API-confirmed" in cost_text
        assert "input_tokens: Not available" in cost_text
        assert "cache_creation: $0.0300" in cost_text
        assert "calls=1 (actual=1, estimated=0)" in cost_text
        assert "cache_creation_in=2" in cost_text
        assert data["data"]["actual_call_count"] == 1
        assert data["data"]["estimated_call_count"] == 0
        assert data["data"]["compacted_away_tokens"] == 20
        assert data["data"]["cache_creation_input_tokens"] == 2

    def test_display_verbosity_command_returns_frontend_action(self, client):
        """eggw supports the UI-only /displayVerbosity command."""
        create_resp = client.post("/api/threads", json={"name": "Display Verbosity"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/displayVerbosity medium"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Display verbosity set to medium."
        assert data["data"] == {
            "action": "set_display_verbosity",
            "display_verbosity": "medium",
        }

    def test_display_verbosity_command_reports_usage(self, client):
        """No /displayVerbosity argument returns usage for the browser UI."""
        create_resp = client.post("/api/threads", json={"name": "Display Verbosity Usage"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/displayVerbosity"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Usage: /displayVerbosity <max|medium|min>"
        assert data["data"]["action"] == "display_verbosity_usage"

    def test_execute_help_command(self, client):
        """Test executing /help command."""
        # Create thread
        create_resp = client.post("/api/threads", json={"name": "Command Test"})
        thread_id = create_resp.json()["id"]

        # Execute /help
        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/help"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Commands:" in data["message"]
        assert "/reload" in data["message"]
        assert "/context" in data["message"]
        assert "/compact" in data["message"]
        assert "/compactWithSummary" in data["message"]
        assert "/setAutoCompactThreshold" in data["message"]
        assert "/btw <message>" in data["message"]
        assert "/cost" in data["message"]
        assert "EggW-only commands:" in data["message"]
        assert "/theme [name]" in data["message"]
        assert "/rename <name>" in data["message"]
        assert "/spawn <context>" in data["message"]
        assert "/redraw — No-op in EggW" in data["message"]
        assert "/displayMode — Terminal-only" in data["message"]

    def test_btw_command_uses_shared_preserve_turn_handler(self, client, monkeypatch):
        """EggW /btw queues the shared preserve-turn request for the assistant."""
        started: list[str] = []
        monkeypatch.setattr("eggw.commands.ensure_scheduler_for", lambda tid: started.append(tid))
        create_resp = client.post("/api/threads", json={"name": "BTW Command"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/btw please update me"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Queued /btw request for the assistant."
        assert started == [thread_id]

        messages = client.get(f"/api/threads/{thread_id}/messages").json()
        request = messages[-1]
        assert request["role"] == "user"
        assert "answer_user_while_preserving_llm_turn" in request["content"]
        assert "please update me" in request["content"]

    def test_compact_command_sets_provider_context_start(self, client):
        """eggw supports the shared /compact command."""
        create_resp = client.post("/api/threads", json={"name": "Compact Command"})
        thread_id = create_resp.json()["id"]

        client.post(f"/api/threads/{thread_id}/messages", json={"content": "old"})
        from eggthreads import append_message, filter_messages_for_compaction_provider_context, create_snapshot

        start = append_message(core_state.db, thread_id, "assistant", "summary")
        after = append_message(core_state.db, thread_id, "user", "after")

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/compact {start}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["start_msg_id"] == start
        snapshot = create_snapshot(core_state.db, thread_id)
        provider = filter_messages_for_compaction_provider_context(core_state.db, thread_id, snapshot["messages"])
        assert [m["msg_id"] for m in provider if m.get("role") != "system"] == [start, after]

    def test_compact_with_summary_command_queues_request(self, client, monkeypatch):
        """eggw /compactWithSummary commits a boundary, then queues summary."""
        started: list[str] = []
        monkeypatch.setattr("eggw.commands.compaction.ensure_scheduler_for", lambda tid: started.append(tid))
        create_resp = client.post("/api/threads", json={"name": "Compact Summary Command"})
        thread_id = create_resp.json()["id"]
        user_resp = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"content": "Please summarize the context."},
        )
        user_msg_id = user_resp.json()["message_id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/compactWithSummary"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["request_msg_id"]
        assert started == [thread_id]

        rows = core_state.db.conn.execute(
            "SELECT event_seq, type, msg_id, payload_json FROM events WHERE thread_id=? ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
        compaction = next(row for row in rows if row["type"] == "thread.compaction")
        request_event = next(row for row in rows if row["msg_id"] == data["data"]["request_msg_id"])
        compaction_payload = json.loads(compaction["payload_json"])
        assert compaction["event_seq"] < request_event["event_seq"]
        assert compaction_payload["start_msg_id"] == user_msg_id

        messages = client.get(f"/api/threads/{thread_id}/messages").json()
        request = next(m for m in messages if m["id"] == data["data"]["request_msg_id"])
        assert request["role"] == "user"
        assert "compaction-checkpoint" in request["content"]
        assert "summary_only" in request["content"]
        assert "compact_thread()" not in request["content"]

    def test_set_auto_compact_threshold_command_appends_context_length_event(self, client):
        """eggw supports the shared auto-compaction threshold command."""
        from eggthreads import list_thread_compaction_context_lengths, resolve_auto_compact_threshold

        create_resp = client.post("/api/threads", json={"name": "Auto Compact Threshold"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/setAutoCompactThreshold 12345"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["threshold_tokens"] == 12345
        events = list_thread_compaction_context_lengths(core_state.db, thread_id)
        assert events[-1]["threshold_tokens"] == 12345
        assert events[-1]["created_by"] == "user_command"
        resolved = resolve_auto_compact_threshold(core_state.db, thread_id, explicit_threshold_tokens=999, environ={})
        assert resolved.enabled is True
        assert resolved.threshold_tokens == 12345
        assert resolved.source == "thread_event"

    def test_set_auto_compact_threshold_command_zero_disables_auto_compaction(self, client):
        """Zero uses existing core semantics to disable auto-compaction."""
        from eggthreads import list_thread_compaction_context_lengths, resolve_auto_compact_threshold

        create_resp = client.post("/api/threads", json={"name": "Disable Auto Compact"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/setAutoCompactThreshold 0"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "disabled" in data["message"]
        assert data["data"]["threshold_tokens"] == 0
        events = list_thread_compaction_context_lengths(core_state.db, thread_id)
        assert events[-1]["threshold_tokens"] == 0
        resolved = resolve_auto_compact_threshold(core_state.db, thread_id, explicit_threshold_tokens=999, environ={})
        assert resolved.enabled is False
        assert resolved.threshold_tokens is None
        assert resolved.source == "thread_event"

    def test_context_command_reports_compaction_and_limits(self, client, monkeypatch):
        """eggw /context reports current provider context and compaction status."""
        from eggthreads import append_message, commit_thread_compaction, set_context_limit, set_thread_compaction_context_length

        create_resp = client.post("/api/threads", json={"name": "Context Status"})
        thread_id = create_resp.json()["id"]
        append_message(core_state.db, thread_id, "user", "old")
        start = append_message(core_state.db, thread_id, "assistant", "summary")
        append_message(core_state.db, thread_id, "user", "after")
        commit_thread_compaction(core_state.db, thread_id, start, created_by="test")
        set_context_limit(core_state.db, thread_id, 1000)
        set_thread_compaction_context_length(core_state.db, thread_id, 800, created_by="test")

        monkeypatch.setattr(
            "eggthreads.builtin_plugins.compaction.thread_token_stats",
            lambda db_arg, tid_arg, llm=None: {"context_tokens": 400, "full_thread_tokens": 900},
        )

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/context"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["context_tokens"] == 400
        assert data["data"]["full_thread_tokens"] == 900
        assert data["data"]["context_limit"] == 1000
        assert data["data"]["auto_compact_threshold"] == 800
        assert data["data"]["auto_compact_source"] == "thread_event"
        assert data["data"]["compaction"]["compacted"] is True
        assert start in data["message"]
        assert "context_limit:" in data["message"]
        assert "1,000" in data["message"]
        assert "40.0%" in data["message"]

    def test_reload_requires_eggw_wrapper(self, client, monkeypatch):
        """/reload reports a clear error when not launched by eggw.sh."""
        monkeypatch.delenv("EGGW_RELOAD_STATE_FILE", raising=False)
        create_resp = client.post("/api/threads", json={"name": "Reload Test"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/reload"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "eggw.sh" in data["message"]

    def test_reload_writes_thread_id_and_schedules_exit(self, client, tmp_path, monkeypatch):
        """/reload writes the current thread for the eggw.sh restart handoff."""
        state_file = tmp_path / "reload-state"
        monkeypatch.setenv("EGGW_RELOAD_STATE_FILE", str(state_file))
        monkeypatch.setenv("EGGW_RELOAD_EXIT_CODE", "75")
        created = []

        class DummyTask:
            pass

        def fake_create_task(coro):
            created.append(coro)
            coro.close()
            return DummyTask()

        monkeypatch.setattr(asyncio, "create_task", fake_create_task)
        create_resp = client.post("/api/threads", json={"name": "Reload Test"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/reload"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["action"] == "reload"
        assert state_file.read_text(encoding="utf-8").strip() == thread_id
        assert len(created) == 1

    def test_tools_status_reflects_thread_tool_allowlist(self, client):
        """/toolsStatus should report effective tools after set_thread_tool_allowlist."""
        create_resp = client.post("/api/threads", json={"name": "Tools Status Test"})
        thread_id = create_resp.json()["id"]

        import eggthreads as ts

        ts.set_thread_tool_allowlist(core_state.db, thread_id, ["bash"])

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/toolsStatus"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["allowed_tools"] == ["bash"]

        statuses = {item["name"]: item for item in data["data"]["tools"]}
        assert statuses["bash"]["enabled"] is True
        assert statuses["bash"]["status"] == "enabled"
        assert statuses["python"]["enabled"] is False
        assert statuses["python"]["status"] == "not_allowed"
        assert "python: not allowed" in data["message"]


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestSessionCommands:
    """Test persistent session commands."""

    def test_session_status_command(self, client):
        create_resp = client.post("/api/threads", json={"name": "Session Test"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/sessionStatus"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Current thread session" in data["message"]

    def test_session_on_off_commands(self, client):
        create_resp = client.post("/api/threads", json={"name": "Session Test"})
        thread_id = create_resp.json()["id"]

        on_resp = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/sessionOn provider=memory share_repl=true"},
        )
        assert on_resp.status_code == 200
        on_data = on_resp.json()
        assert on_data["success"] is True
        assert on_data["data"]["provider"] == "memory"
        assert on_data["data"]["share_repl"] is True

        status_resp = client.get(f"/api/threads/{thread_id}/session")
        assert status_resp.status_code == 200
        assert status_resp.json()["enabled"] is True

        off_resp = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/sessionOff"},
        )
        assert off_resp.status_code == 200
        assert off_resp.json()["success"] is True

    def test_python_repl_command_enqueues_tool_call(self, client):
        create_resp = client.post("/api/threads", json={"name": "Session Test"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/pythonRepl print('hi')"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["tool_call_id"]

        from eggthreads import build_tool_call_states
        states = build_tool_call_states(core_state.db, thread_id)
        assert any(tc.name == "python_repl" for tc in states.values())


class TestAutocomplete:
    """Test backend autocomplete for session/RLM commands."""

    def test_session_command_autocomplete(self, client):
        response = client.get("/api/autocomplete", params={"line": "/session", "cursor": 8})
        assert response.status_code == 200
        displays = [s["display"] for s in response.json()["suggestions"]]
        assert "/sessionStatus" in displays
        assert "/sessionOn" in displays
        assert "/sessionStop" in displays
        assert "/sessionReset" in displays

    def test_session_argument_autocomplete(self, client):
        response = client.get("/api/autocomplete", params={"line": "/sessionOn provider=", "cursor": 20})
        assert response.status_code == 200
        displays = [s["display"] for s in response.json()["suggestions"]]
        assert "provider=docker" in displays
        assert "provider=memory" in displays

        response = client.get("/api/autocomplete", params={"line": "/sessionReset ", "cursor": 14})
        assert response.status_code == 200
        displays = [s["display"] for s in response.json()["suggestions"]]
        assert "python" in displays
        assert "bash" in displays
        assert "all" in displays
