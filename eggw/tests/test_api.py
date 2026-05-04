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

    def test_get_messages_uses_incremental_snapshot_cache(self, client, monkeypatch):
        """Repeated /messages calls should not force a full snapshot rebuild."""
        create_resp = client.post("/api/threads", json={"name": "Test Thread"})
        thread_id = create_resp.json()["id"]

        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="msg.create",
            msg_id=os.urandom(10).hex(),
            payload={"role": "user", "content": "first"},
        )
        assert client.get(f"/api/threads/{thread_id}/messages").status_code == 200

        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="msg.create",
            msg_id=os.urandom(10).hex(),
            payload={"role": "user", "content": "second"},
        )

        def fail_full_rebuild(self, events):
            raise AssertionError("full snapshot rebuild should not run for append-only /messages tail")

        monkeypatch.setattr("eggthreads.api.SnapshotBuilder.build", fail_full_rebuild)

        response = client.get(f"/api/threads/{thread_id}/messages")

        assert response.status_code == 200
        assert [m["content"] for m in response.json()] == ["first", "second"]


class TestEventStreaming:
    """Test SSE event shaping for streaming UI clients."""

    def test_sse_replays_active_tool_stream_with_preview_limit_indicator(self, client):
        """An eggw client joining mid-tool-stream sees preview + suppressed event."""
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
        # After approval, state should advance (TC2 = approved, waiting execution)
        assert len(tools) == 1
        assert tools[0]["state"] in ["TC2", "TC3", "TC4", "TC5"]  # Advanced from TC1


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

    def test_get_stats_includes_live_llm_tps(self, client):
        """Stats endpoint reports live TPS for an active LLM stream."""
        create_resp = client.post("/api/threads", json={"name": "Live TPS Test"})
        thread_id = create_resp.json()["id"]
        invoke_id = "invoke-live-tps"

        assert core_state.db.try_open_stream(
            thread_id,
            invoke_id,
            "2999-01-01 00:00:00",
            owner="test",
            purpose="llm",
        )
        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="stream.open",
            msg_id=os.urandom(10).hex(),
            invoke_id=invoke_id,
            payload={"stream_kind": "llm", "model_key": "test-model"},
        )
        core_state.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="stream.delta",
            invoke_id=invoke_id,
            chunk_seq=1,
            payload={"text": "hello world " * 80, "model_key": "test-model"},
        )
        # Give live TPS a non-zero elapsed duration.
        time.sleep(0.3)

        response = client.get(f"/api/threads/{thread_id}/stats")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["streaming_tps"], (int, float))
        assert data["streaming_tps"] > 0


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
        assert "Available commands" in data["message"]

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
