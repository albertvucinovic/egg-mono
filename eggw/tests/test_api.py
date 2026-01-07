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

# Add paths for imports
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "eggthreads"))
sys.path.insert(0, str(PROJECT_ROOT / "eggllm"))

from fastapi.testclient import TestClient


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
    if "main" in sys.modules:
        del sys.modules["main"]
    import main

    # Reset global state
    main.db = None
    main.active_schedulers = {}

    # Initialize database with check_same_thread=False for testing
    from eggthreads import ThreadsDB
    # Create DB connection that allows multi-threaded access
    conn = sqlite3.connect(test_db_path, check_same_thread=False, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")

    # Create ThreadsDB and replace its connection
    main.db = ThreadsDB.__new__(ThreadsDB)
    main.db.path = Path(test_db_path)
    main.db.conn = conn
    main.db.init_schema()

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
        import main
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
        main.db.append_event(
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


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
