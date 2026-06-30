"""
Integration tests for eggw backend API.

Tests streaming, tool approval, and tool execution flows.
Run with: pytest test_api.py -v
"""

import asyncio
import ast
import inspect
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

    def test_image_models_endpoint_lists_only_supported_image_generation_backends(self, client):
        old_models = core_state.image_generation_models_config
        old_default = core_state.default_image_generation_model_key
        try:
            core_state.image_generation_models_config = {
                "Image Backend": {
                    "provider": "openai-images",
                    "model_name": "gpt-image-1",
                    "api_type": "openai_images",
                },
                "OpenAI Pro Image: gpt-image-2": {
                    "provider": "openai-pro",
                    "model_name": "gpt-image-2",
                    "api_type": "codex_images",
                },
            }
            core_state.default_image_generation_model_key = "OpenAI Pro Image: gpt-image-2"

            response = client.get("/api/image-models")

            assert response.status_code == 200
            payload = response.json()
            assert [model["key"] for model in payload["models"]] == ["Image Backend", "OpenAI Pro Image: gpt-image-2"]
            assert payload["default_model"] == "OpenAI Pro Image: gpt-image-2"
        finally:
            core_state.image_generation_models_config = old_models
            core_state.default_image_generation_model_key = old_default


class TestThreadOperations:
    """Test thread CRUD operations."""

    @staticmethod
    def _system_messages(client, thread_id):
        response = client.get(f"/api/threads/{thread_id}/messages")
        assert response.status_code == 200
        return [m for m in response.json() if m["role"] == "system"]

    def test_create_thread(self, client):
        """Test creating a new thread."""
        response = client.post("/api/threads", json={"name": "Test Thread"})
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert len(data["id"]) > 0
        return data["id"]

    def test_thread_lists_use_latest_model_switch(self, client):
        """Thread list APIs should report the event-log model source of truth."""
        from eggthreads import set_thread_model

        response = client.post("/api/threads", json={"name": "Model Switch List Test"})
        assert response.status_code == 200
        thread_id = response.json()["id"]

        set_thread_model(
            core_state.db,
            thread_id,
            "Switched Model",
            concrete_model_info={},
            reason="test",
        )

        response = client.get("/api/threads")
        assert response.status_code == 200
        thread = next(t for t in response.json() if t["id"] == thread_id)
        assert thread["model_key"] == "Switched Model"

        response = client.get("/api/threads/roots")
        assert response.status_code == 200
        thread = next(t for t in response.json() if t["id"] == thread_id)
        assert thread["model_key"] == "Switched Model"

    def test_root_threads_keep_legacy_orphan_runtime_rows_visible_and_sort_by_activity(self, client):
        """Legacy orphan runtime rows remain inspectable in the root list."""
        from eggthreads import append_message

        first_resp = client.post("/api/threads", json={"name": "Older Chat Root"})
        assert first_resp.status_code == 200
        first_id = first_resp.json()["id"]

        second_resp = client.post("/api/threads", json={"name": "Newer Chat Root"})
        assert second_resp.status_code == 200
        second_id = second_resp.json()["id"]

        # Simulate pre-existing/internal runtime rows that have no children row.
        # These have appeared in real databases.  They should remain visible so
        # users can inspect/repair them; EggW startup no longer depends on
        # hiding them because `/` opens a fresh thread.
        core_state.db.create_thread(
            thread_id="01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            name="@runtime:python",
            parent_id=None,
            initial_model_key=None,
            depth=1,
        )
        core_state.db.create_thread(
            thread_id="01ZZZZZZZZZZZZZZZZZZZZZZZY",
            name="@runtime:bash:analysis",
            parent_id=None,
            initial_model_key=None,
            depth=0,
        )

        # Activity, not thread-id/insertion order, determines the landing-page
        # "latest root" because Home redirects to the final root in this list.
        append_message(core_state.db, first_id, "user", "make older root recently active")

        response = client.get("/api/threads/roots")
        assert response.status_code == 200
        roots = response.json()
        root_ids = [thread["id"] for thread in roots]

        assert "01ZZZZZZZZZZZZZZZZZZZZZZZZ" in root_ids
        assert "01ZZZZZZZZZZZZZZZZZZZZZZZY" in root_ids
        assert second_id in root_ids
        assert root_ids[-1] == first_id
        assert next(thread for thread in roots if thread["id"] == first_id)["created_at"] is not None

    def test_api_created_root_thread_has_one_system_prompt(self, client, monkeypatch):
        """EggW API-created root threads include the loaded system prompt once."""
        monkeypatch.setattr("eggw.system_prompt.load_system_prompt", lambda: "EGGW TEST SYSTEM PROMPT")

        response = client.post("/api/threads", json={"name": "System Prompt Root"})
        thread_id = response.json()["id"]

        system_messages = self._system_messages(client, thread_id)
        assert [m["content"] for m in system_messages] == ["EGGW TEST SYSTEM PROMPT"]

        open_resp = client.post(f"/api/threads/{thread_id}/open")
        assert open_resp.status_code == 200
        system_messages = self._system_messages(client, thread_id)
        assert [m["content"] for m in system_messages] == ["EGGW TEST SYSTEM PROMPT"]

    def test_command_created_root_thread_has_one_system_prompt(self, client, monkeypatch):
        """EggW /newThread-created roots include the loaded system prompt once."""
        monkeypatch.setattr("eggw.system_prompt.load_system_prompt", lambda: "EGGW COMMAND SYSTEM PROMPT")
        create_resp = client.post("/api/threads", json={"name": "Current Root"})
        current_thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{current_thread_id}/command",
            json={"command": "/newThread Command Root"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        new_thread_id = data["data"]["thread_id"]
        system_messages = self._system_messages(client, new_thread_id)
        assert [m["content"] for m in system_messages] == ["EGGW COMMAND SYSTEM PROMPT"]

    def test_api_created_child_thread_does_not_get_root_system_prompt(self, client, monkeypatch):
        """Root prompt insertion is limited to new root threads."""
        monkeypatch.setattr("eggw.system_prompt.load_system_prompt", lambda: "EGGW ROOT ONLY PROMPT")
        parent_resp = client.post("/api/threads", json={"name": "Parent Root"})
        parent_id = parent_resp.json()["id"]

        child_resp = client.post("/api/threads", json={"name": "Child", "parent_id": parent_id})
        child_id = child_resp.json()["id"]

        assert [m["content"] for m in self._system_messages(client, parent_id)] == ["EGGW ROOT ONLY PROMPT"]
        assert self._system_messages(client, child_id) == []

    def test_api_rejects_child_thread_with_missing_parent(self, client):
        """Failed child creation must not leave orphan non-root rows."""
        response = client.post("/api/threads", json={"name": "Orphan", "parent_id": "missing-parent"})

        assert response.status_code == 404
        assert response.json()["detail"] == "Parent thread not found"
        assert core_state.db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0

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

    def test_threads_command_returns_full_thread_ids_for_eggw_links(self, client):
        """EggW /threads output includes full ids so the UI can link suffixes."""
        parent_resp = client.post("/api/threads", json={"name": "Clickable Parent"})
        parent_id = parent_resp.json()["id"]
        child_resp = client.post("/api/threads", json={"name": "Clickable Child", "parent_id": parent_id})
        child_id = child_resp.json()["id"]

        response = client.post(
            f"/api/threads/{parent_id}/command",
            json={"command": "/threads"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["command_name"] == "threads"
        assert parent_id in payload["data"]["thread_ids"]
        assert child_id in payload["data"]["thread_ids"]
        assert parent_id[-8:] in payload["message"]
        assert child_id[-8:] in payload["message"]

    def test_threads_command_uses_fast_status_mode_by_default(self, client, monkeypatch):
        """Default /threads must not reduce every long thread's event log."""
        import eggw.commands.thread as thread_commands

        parent_resp = client.post("/api/threads", json={"name": "Fast Threads Parent"})
        parent_id = parent_resp.json()["id"]
        client.post("/api/threads", json={"name": "Fast Threads Child", "parent_id": parent_id})

        calls = []

        def fake_get_thread_statuses_bulk(db, tids, *, skip_runnability=False):
            calls.append((tuple(tids), skip_runnability))
            return {tid: "idle" for tid in tids}

        monkeypatch.setattr(thread_commands, "get_thread_statuses_bulk", fake_get_thread_statuses_bulk)

        response = client.post(
            f"/api/threads/{parent_id}/command",
            json={"command": "/threads"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["data"]["status_mode"] == "fast"
        assert calls and calls[0][1] is True
        assert "status=full" in payload["message"]

    def test_threads_command_full_status_mode_is_explicit(self, client, monkeypatch):
        """Users can still request the expensive runnable-state scan."""
        import eggw.commands.thread as thread_commands

        parent_resp = client.post("/api/threads", json={"name": "Full Threads Parent"})
        parent_id = parent_resp.json()["id"]
        calls = []

        def fake_get_thread_statuses_bulk(db, tids, *, skip_runnability=False):
            calls.append((tuple(tids), skip_runnability))
            return {tid: "idle" for tid in tids}

        monkeypatch.setattr(thread_commands, "get_thread_statuses_bulk", fake_get_thread_statuses_bulk)

        response = client.post(
            f"/api/threads/{parent_id}/command",
            json={"command": "/threads status=full"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["data"]["status_mode"] == "full"
        assert calls and calls[0][1] is False

    def test_threads_command_keeps_orphan_runtime_roots_visible(self, client):
        """Legacy orphan @runtime:* rows remain visible/inspectable."""
        parent_resp = client.post("/api/threads", json={"name": "Visible Parent"})
        parent_id = parent_resp.json()["id"]
        orphan_id = "01ZZZZZZZZZZZZZZZZZZZZZZRT"
        core_state.db.create_thread(
            thread_id=orphan_id,
            name="@runtime:python",
            parent_id=None,
            initial_model_key=None,
            depth=1,
        )

        response = client.post(
            f"/api/threads/{parent_id}/command",
            json={"command": "/threads"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert orphan_id in payload["data"]["thread_ids"]
        assert orphan_id in payload["data"]["threads"]
        assert orphan_id[-8:] in payload["message"]

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

    def test_get_messages_limit_returns_recent_tail(self, client):
        """Transcript API can return a bounded recent tail for large web threads."""
        from eggthreads import append_message

        create_resp = client.post("/api/threads", json={"name": "Message Tail"})
        thread_id = create_resp.json()["id"]

        append_message(core_state.db, thread_id, role="user", content="first")
        append_message(core_state.db, thread_id, role="assistant", content="second")
        append_message(core_state.db, thread_id, role="user", content="third")

        response = client.get(f"/api/threads/{thread_id}/messages?limit=2")

        assert response.status_code == 200
        data = response.json()
        assert [message["content"] for message in data] == ["second", "third"]

    def test_get_messages_before_id_paginates_older_history(self, client):
        """Bounded transcript API can page older messages before the first loaded id."""
        from eggthreads import append_message

        create_resp = client.post("/api/threads", json={"name": "Message Pagination"})
        thread_id = create_resp.json()["id"]

        append_message(core_state.db, thread_id, role="user", content="first")
        second_id = append_message(core_state.db, thread_id, role="assistant", content="second")
        append_message(core_state.db, thread_id, role="user", content="third")

        response = client.get(f"/api/threads/{thread_id}/messages?limit=1&before_id={second_id}")

        assert response.status_code == 200
        data = response.json()
        assert [message["content"] for message in data] == ["first"]

    def test_upload_attachment_returns_metadata_part_and_stores_bytes(self, client, test_db_path):
        from eggthreads.input_artifacts import resolve_input_bytes

        create_resp = client.post("/api/threads", json={"name": "Upload Thread"})
        thread_id = create_resp.json()["id"]
        data = b"\x89PNG\r\n\x1a\nimage-bytes"

        response = client.post(
            f"/api/threads/{thread_id}/attachments",
            files={"file": ("pixel.png", data, "image/png")},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["input_id"] == payload["metadata"]["input_id"]
        assert payload["metadata"]["owner_thread_id"] == thread_id
        assert payload["metadata"]["filename"] == "pixel.png"
        assert payload["metadata"]["mime_type"] == "image/png"
        assert payload["metadata"]["presentation"] == "image"
        assert payload["metadata"]["provenance"] == {
            "kind": "eggw_upload",
            "display_name": "pixel.png",
            "client_content_type": "image/png",
        }
        assert payload["content_part"]["type"] == "attachment"
        assert payload["content_part"]["input_id"] == payload["input_id"]
        assert payload["content_part"]["presentation"] == "image"
        assert payload["content_text"].startswith("[Attachment: image pixel.png image/png")

        workspace = Path(test_db_path).parent.parent
        metadata, resolved = resolve_input_bytes(workspace, core_state.db, thread_id, payload["input_id"])
        assert metadata == payload["metadata"]
        assert resolved == data

    def test_upload_forged_image_extension_stays_generic_file(self, client):
        create_resp = client.post("/api/threads", json={"name": "Forged Upload"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/attachments",
            files={"file": ("not-image.png", b"not really an image", "image/png")},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["metadata"]["filename"] == "not-image.png"
        assert payload["metadata"]["presentation"] == "file"
        assert payload["metadata"]["mime_type"] == "text/plain"
        assert payload["content_part"]["presentation"] == "file"
        assert payload["content_text"].startswith("[Attachment: file not-image.png text/plain")

    def test_send_message_with_uploaded_attachment_part_exposes_content_text(self, client):
        create_resp = client.post("/api/threads", json={"name": "Send Uploaded Part"})
        thread_id = create_resp.json()["id"]
        upload = client.post(
            f"/api/threads/{thread_id}/attachments",
            files={"file": ("note.txt", b"hello upload", "text/plain")},
        ).json()

        content = [{"type": "text", "text": "see attached"}, upload["content_part"]]
        send_resp = client.post(f"/api/threads/{thread_id}/messages", json={"content": content})

        assert send_resp.status_code == 200
        message_id = send_resp.json()["message_id"]
        messages = client.get(f"/api/threads/{thread_id}/messages").json()
        sent = next(m for m in messages if m["id"] == message_id)
        assert sent["content"] == content
        assert sent["content_text"].startswith("see attached\n[Attachment: file note.txt text/plain")

    def test_get_attachment_returns_bytes_and_headers(self, client, test_db_path):
        from eggthreads.input_artifacts import save_input_bytes

        create_resp = client.post("/api/threads", json={"name": "Attachment Download"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).parent.parent
        data = b"\x89PNG\r\n\x1a\ninput-image"
        saved = save_input_bytes(
            workspace,
            thread_id,
            data,
            filename="input image.png",
            mime_type="image/png",
            presentation="image",
        )

        inline = client.get(f"/api/threads/{thread_id}/attachments/{saved.input_id}")
        download = client.get(f"/api/threads/{thread_id}/attachments/{saved.input_id}?download=true")

        assert inline.status_code == 200
        assert inline.content == data
        assert inline.headers["content-type"].startswith("image/png")
        assert inline.headers["x-content-type-options"] == "nosniff"
        assert inline.headers["content-disposition"].startswith("inline;")
        assert "input%20image.png" in inline.headers["content-disposition"]
        assert download.status_code == 200
        assert download.content == data
        assert download.headers["content-disposition"].startswith("attachment;")

    def test_get_attachment_parent_can_read_child_with_explicit_selector(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.input_artifacts import save_input_bytes

        parent_resp = client.post("/api/threads", json={"name": "Attachment Parent"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        workspace = Path(test_db_path).parent.parent
        saved = save_input_bytes(workspace, child_id, b"child input", filename="child.txt", mime_type="text/plain", presentation="file")

        response = client.get(
            f"/api/threads/{parent_id}/attachments/{saved.input_id}",
            params={"descendant_thread_id": child_id},
        )

        assert response.status_code == 200
        assert response.content == b"child input"
        assert response.headers["content-type"].startswith("text/plain")

    def test_get_attachment_parent_without_selector_gets_404(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.input_artifacts import save_input_bytes

        parent_resp = client.post("/api/threads", json={"name": "Attachment Missing Selector"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        workspace = Path(test_db_path).parent.parent
        saved = save_input_bytes(workspace, child_id, b"child input")

        response = client.get(f"/api/threads/{parent_id}/attachments/{saved.input_id}")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_attachment_denies_child_and_sibling_access(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.input_artifacts import save_input_bytes

        parent_resp = client.post("/api/threads", json={"name": "Attachment Denied"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        sibling_id = create_child_thread(core_state.db, parent_id, name="sibling")
        workspace = Path(test_db_path).parent.parent
        parent_saved = save_input_bytes(workspace, parent_id, b"parent input")
        sibling_saved = save_input_bytes(workspace, sibling_id, b"sibling input")

        parent_response = client.get(
            f"/api/threads/{child_id}/attachments/{parent_saved.input_id}",
            params={"descendant_thread_id": parent_id},
        )
        sibling_response = client.get(
            f"/api/threads/{child_id}/attachments/{sibling_saved.input_id}",
            params={"descendant_thread_id": sibling_id},
        )

        assert parent_response.status_code == 403
        assert sibling_response.status_code == 403

    def test_get_attachment_rejects_bad_ids_without_path_leak(self, client, test_db_path):
        from eggthreads.input_artifacts import save_input_bytes

        create_resp = client.post("/api/threads", json={"name": "Attachment Bad IDs"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).parent.parent
        saved = save_input_bytes(workspace, thread_id, b"secret input bytes")

        sha_response = client.get(f"/api/threads/{thread_id}/attachments/{saved.metadata['sha256']}")
        upper_response = client.get(f"/api/threads/{thread_id}/attachments/{saved.input_id.upper()}")
        path_response = client.get(f"/api/threads/{thread_id}/attachments/..%2Fbad1")

        assert sha_response.status_code == 400
        assert upper_response.status_code == 400
        assert path_response.status_code == 404
        joined_details = " ".join(
            str(response.json().get("detail"))
            for response in (sha_response, upper_response, path_response)
        )
        assert str(workspace) not in joined_details
        assert ".egg" not in joined_details
        assert "secret input bytes" not in joined_details

    def test_upload_attachment_unknown_thread_returns_404(self, client):
        response = client.post(
            "/api/threads/missing-thread/attachments",
            files={"file": ("note.txt", b"hello", "text/plain")},
        )

        assert response.status_code == 404

    def test_upload_attachment_rejects_empty_file(self, client):
        create_resp = client.post("/api/threads", json={"name": "Empty Upload"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/attachments",
            files={"file": ("empty.txt", b"", "text/plain")},
        )

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_attachment_slash_commands_are_available_in_eggw(self, client, tmp_path, monkeypatch):
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        monkeypatch.chdir(tmp_path)
        create_resp = client.post("/api/threads", json={"name": "Attachment Commands"})
        thread_id = create_resp.json()["id"]
        source = tmp_path / "note.txt"
        source.write_text("hello from attach", encoding="utf-8")

        attach_response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/attach {source}"},
        )
        assert attach_response.status_code == 200
        attach_payload = attach_response.json()
        assert attach_payload["success"] is True
        assert attach_payload["data"]["action"] == "stage_attachment"
        attached_part = attach_payload["data"]["content_part"]
        assert attached_part["type"] == "attachment"
        assert attached_part["filename"] == "note.txt"
        assert attached_part["owner_thread_id"] == thread_id

        list_response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/attachments", "staged_attachments": [attached_part]},
        )
        assert list_response.status_code == 200
        assert list_response.json()["success"] is True
        assert "Staged attachments:" in list_response.json()["message"]
        assert "note.txt" in list_response.json()["message"]

        send_response = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"content": [{"type": "text", "text": "use this"}, attached_part]},
        )
        assert send_response.status_code == 200

        historical_response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/attachments", "staged_attachments": []},
        )
        assert historical_response.status_code == 200
        historical_payload = historical_response.json()
        assert historical_payload["success"] is True
        assert "No attachments currently staged." in historical_payload["message"]
        assert "Historical attachments used in this conversation:" in historical_payload["message"]
        assert "note.txt" in historical_payload["message"]

        clear_response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/clearAttachments", "staged_attachments": [attached_part]},
        )
        assert clear_response.status_code == 200
        assert clear_response.json()["success"] is True
        assert clear_response.json()["data"] == {"action": "clear_staged_attachments", "count": 1}

        provider_output = save_provider_output_bytes(
            tmp_path,
            thread_id,
            b"generated bytes",
            filename="generated.txt",
            mime_type="text/plain",
            presentation="file",
        )
        attach_output_response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/attachOutput {provider_output.artifact_id}"},
        )
        assert attach_output_response.status_code == 200
        attach_output_payload = attach_output_response.json()
        assert attach_output_payload["success"] is True
        assert attach_output_payload["data"]["action"] == "stage_attachment"
        promoted_part = attach_output_payload["data"]["content_part"]
        assert promoted_part["type"] == "attachment"
        assert promoted_part["filename"] == "generated.txt"

        export_response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/saveProviderArtifact {provider_output.artifact_id} exported.txt"},
        )
        assert export_response.status_code == 200
        export_payload = export_response.json()
        assert export_payload["success"] is True
        assert export_payload["data"]["action"] == "save_provider_artifact"
        assert (tmp_path / "exported.txt").read_bytes() == b"generated bytes"

    def test_command_route_emits_lifecycle_events_and_timing_metadata(self, client):
        create_resp = client.post("/api/threads", json={"name": "Command Lifecycle"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/attachments"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["command_name"] == "attachments"
        assert payload["command_id"]
        assert payload["elapsed_sec"] >= 0

        rows = core_state.db.conn.execute(
            "SELECT type, payload_json FROM events WHERE thread_id=? AND type IN ('user_command.started', 'user_command.finished') ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
        assert [row["type"] for row in rows] == ["user_command.started", "user_command.finished"]
        started = json.loads(rows[0]["payload_json"])
        finished = json.loads(rows[1]["payload_json"])
        assert started["command_id"] == payload["command_id"]
        assert started["command_name"] == "attachments"
        assert finished["command_id"] == payload["command_id"]
        assert finished["success"] is True
        assert finished["elapsed_sec"] >= 0

    def test_image_generate_slash_command_emits_start_status(self, client, monkeypatch, test_db_path):
        import eggw.image_generation_service as image_generation_service

        sha = "0123456789abcdef" * 4

        class FakeArtifact:
            artifact_id = "abc12345"

            def __init__(self, owner_thread_id):
                self.content_part = {
                    "type": "artifact",
                    "artifact_id": self.artifact_id,
                    "owner_thread_id": owner_thread_id,
                    "presentation": "image",
                    "mime_type": "image/png",
                    "filename": "generated-1.png",
                    "size_bytes": 11,
                    "sha256": sha,
                    "provenance": {},
                    "options": {},
                }
                self.metadata = {k: v for k, v in self.content_part.items() if k not in {"type", "provenance", "options"}}

        class FakeResult:
            def __init__(self, owner_thread_id, prompt):
                self.model_key = "Image Backend"
                self.provider_name = "openai-images"
                self.model_name = "gpt-image-1"
                self.prompt = prompt
                self.response_metadata = {}
                self.artifacts = (FakeArtifact(owner_thread_id),)

            @property
            def content_parts(self):
                return [artifact.content_part for artifact in self.artifacts]

            @property
            def metadata(self):
                return [artifact.metadata for artifact in self.artifacts]

        monkeypatch.setattr(
            image_generation_service,
            "generate_openai_image_artifacts",
            lambda _workspace, tid, prompt, **_kwargs: FakeResult(tid, prompt),
        )

        create_resp = client.post("/api/threads", json={"name": "Image Generate Status"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/imageGenerate model='Image Backend' Paint an egg"},
        )

        assert response.status_code == 200
        assert response.json()["success"] is True
        rows = core_state.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='user_command.status' ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
        payloads = [json.loads(row["payload_json"]) for row in rows]
        assert any(
            payload.get("command_name") == "imageGenerate"
            and payload.get("message") == "Generating image with Image Backend: Paint an egg"
            and payload.get("timeout") == 600
            for payload in payloads
        )

    def test_image_generation_route_appends_artifact_message_and_returns_metadata(self, client, monkeypatch, test_db_path):
        import eggw.image_generation_service as image_generation_service

        sha = "0123456789abcdef" * 4

        class FakeArtifact:
            def __init__(self, owner_thread_id):
                self.content_part = {
                    "type": "artifact",
                    "artifact_id": "abc12345",
                    "owner_thread_id": owner_thread_id,
                    "presentation": "image",
                    "mime_type": "image/png",
                    "filename": "generated-1.png",
                    "size_bytes": 11,
                    "sha256": sha,
                    "provenance": {
                        "kind": "openai_image_generation",
                        "provider": "openai-images",
                        "model_key": "Image Backend",
                    },
                    "options": {},
                }
                self.metadata = {
                    "artifact_id": "abc12345",
                    "owner_thread_id": owner_thread_id,
                    "presentation": "image",
                    "mime_type": "image/png",
                    "filename": "generated-1.png",
                    "size_bytes": 11,
                    "sha256": sha,
                }

        class FakeResult:
            def __init__(self, owner_thread_id, prompt):
                self.model_key = "Image Backend"
                self.provider_name = "openai-images"
                self.model_name = "gpt-image-1"
                self.prompt = prompt
                self.response_metadata = {"id": "img-resp-test"}
                self.artifacts = (FakeArtifact(owner_thread_id),)

            @property
            def content_parts(self):
                return [artifact.content_part for artifact in self.artifacts]

            @property
            def metadata(self):
                return [artifact.metadata for artifact in self.artifacts]

        calls = []

        def fake_generate(
            workspace,
            thread_id,
            prompt,
            *,
            model_key,
            models_path,
            all_models_path,
            image_generation_models_path,
            options,
        ):
            calls.append(
                {
                    "workspace": Path(workspace),
                    "thread_id": thread_id,
                    "prompt": prompt,
                    "model_key": model_key,
                    "models_path": Path(models_path),
                    "all_models_path": Path(all_models_path),
                    "image_generation_models_path": Path(image_generation_models_path),
                    "options": options,
                }
            )
            return FakeResult(thread_id, prompt)

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generation"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/image-generation",
            json={
                "prompt": "Paint a bright egg",
                "model": "Image Backend",
                "n": 2,
                "size": "1024x1024",
                "quality": "high",
                "output_format": "jpg",
                "background": "transparent",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["prompt"] == "Paint a bright egg"
        assert payload["model_key"] == "Image Backend"
        assert payload["provider_name"] == "openai-images"
        assert payload["model_name"] == "gpt-image-1"
        assert payload["response_metadata"] == {"id": "img-resp-test"}
        assert payload["metadata"] == [FakeArtifact(thread_id).metadata]
        assert payload["content_parts"][0]["type"] == "text"
        assert "Generated 1 image artifact" in payload["content_parts"][0]["text"]
        assert payload["content_parts"][1]["artifact_id"] == "abc12345"
        assert payload["content_text"].startswith("Generated 1 image artifact")
        assert "Provider artifact: image generated-1.png" in payload["content_text"]

        assert calls == [
            {
                "workspace": Path(test_db_path).parent.parent,
                "thread_id": thread_id,
                "prompt": "Paint a bright egg",
                "model_key": "Image Backend",
                "models_path": calls[0]["models_path"],
                "all_models_path": calls[0]["all_models_path"],
                "image_generation_models_path": calls[0]["image_generation_models_path"],
                "options": {
                    "n": 2,
                    "size": "1024x1024",
                    "quality": "high",
                    "background": "transparent",
                    "output_format": "jpeg",
                },
            }
        ]

        messages = client.get(f"/api/threads/{thread_id}/messages").json()
        appended = next(m for m in messages if m["id"] == payload["message_id"])
        assert appended["role"] == "assistant"
        assert appended["content"] == payload["content_parts"]
        assert "generated-bytes" not in json.dumps(appended["content"])

    def test_image_generate_slash_command_appends_artifact_message(self, client, monkeypatch, test_db_path):
        import eggw.image_generation_service as image_generation_service

        sha = "0123456789abcdef" * 4

        class FakeArtifact:
            artifact_id = "abc12345"

            def __init__(self, owner_thread_id):
                self.content_part = {
                    "type": "artifact",
                    "artifact_id": self.artifact_id,
                    "owner_thread_id": owner_thread_id,
                    "presentation": "image",
                    "mime_type": "image/png",
                    "filename": "generated-1.png",
                    "size_bytes": 11,
                    "sha256": sha,
                    "provenance": {
                        "kind": "openai_image_generation",
                        "provider": "openai-images",
                        "model_key": "Image Backend",
                    },
                    "options": {},
                }
                self.metadata = {
                    "artifact_id": self.artifact_id,
                    "owner_thread_id": owner_thread_id,
                    "presentation": "image",
                    "mime_type": "image/png",
                    "filename": "generated-1.png",
                    "size_bytes": 11,
                    "sha256": sha,
                }

        class FakeResult:
            def __init__(self, owner_thread_id, prompt):
                self.model_key = "Image Backend"
                self.provider_name = "openai-images"
                self.model_name = "gpt-image-1"
                self.prompt = prompt
                self.response_metadata = {"id": "img-resp-test"}
                self.artifacts = (FakeArtifact(owner_thread_id),)

            @property
            def content_parts(self):
                return [artifact.content_part for artifact in self.artifacts]

            @property
            def metadata(self):
                return [artifact.metadata for artifact in self.artifacts]

        calls = []

        def fake_generate(
            workspace,
            thread_id,
            prompt,
            *,
            model_key,
            models_path,
            all_models_path,
            image_generation_models_path,
            options,
        ):
            calls.append(
                {
                    "workspace": Path(workspace),
                    "thread_id": thread_id,
                    "prompt": prompt,
                    "model_key": model_key,
                    "models_path": Path(models_path),
                    "all_models_path": Path(all_models_path),
                    "image_generation_models_path": Path(image_generation_models_path),
                    "options": options,
                }
            )
            return FakeResult(thread_id, prompt)

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generate Command"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={
                "command": (
                    '/imageGenerate model="Image Backend" n=2 size=1024x1024 '
                    'quality=high output_format=jpg background=transparent Paint a bright egg'
                )
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["data"]["action"] == "image_generation"
        assert payload["command_name"] == "imageGenerate"
        assert payload["data"]["artifact_ids"] == ["abc12345"]
        assert "Generated 1 image artifact" in payload["message"]
        assert "export: /saveProviderArtifact abc12345 generated-1.png" in payload["message"]
        assert "reuse: /attachOutput abc12345" in payload["message"]

        assert calls == [
            {
                "workspace": Path(test_db_path).parent.parent,
                "thread_id": thread_id,
                "prompt": "Paint a bright egg",
                "model_key": "Image Backend",
                "models_path": calls[0]["models_path"],
                "all_models_path": calls[0]["all_models_path"],
                "image_generation_models_path": calls[0]["image_generation_models_path"],
                "options": {
                    "n": 2,
                    "size": "1024x1024",
                    "quality": "high",
                    "background": "transparent",
                    "output_format": "jpeg",
                },
            }
        ]

        messages = client.get(f"/api/threads/{thread_id}/messages").json()
        appended = next(m for m in messages if m["id"] == payload["data"]["message_id"])
        assert appended["role"] == "assistant"
        assert appended["content"][1]["artifact_id"] == "abc12345"
        assert "generated-bytes" not in json.dumps(appended["content"])

    def test_image_generate_slash_command_rejects_empty_prompt_without_calling_service(self, client, monkeypatch):
        import eggw.image_generation_service as image_generation_service

        called = False

        def fake_generate(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("service should not be called without a prompt")

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generate Empty"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/imageGenerate n=1"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is False
        assert "Usage: /imageGenerate" in payload["message"]
        assert called is False

    def test_image_generation_route_rejects_empty_prompt_without_calling_service(self, client, monkeypatch):
        import eggw.image_generation_service as image_generation_service

        called = False

        def fake_generate(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("service should not be called without a prompt")

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generation Prompt"})
        thread_id = create_resp.json()["id"]

        response = client.post(f"/api/threads/{thread_id}/image-generation", json={"prompt": "   "})

        assert response.status_code == 400
        assert "prompt" in response.json()["detail"].lower()
        assert called is False

    def test_image_generation_route_provider_failure_returns_502_without_appending_message(self, client, monkeypatch):
        from eggllm.image_generation import ImageGenerationProviderError
        import eggw.image_generation_service as image_generation_service

        calls = []

        def fake_generate(*args, **kwargs):
            calls.append((args, kwargs))
            raise ImageGenerationProviderError("provider unavailable")

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generation Failure"})
        thread_id = create_resp.json()["id"]
        before_messages = client.get(f"/api/threads/{thread_id}/messages").json()

        response = client.post(
            f"/api/threads/{thread_id}/image-generation",
            json={"prompt": "Paint an unavailable egg"},
        )

        assert response.status_code == 502
        assert response.json()["detail"] == "provider unavailable"
        assert len(calls) == 1
        after_messages = client.get(f"/api/threads/{thread_id}/messages").json()
        assert [m["id"] for m in after_messages] == [m["id"] for m in before_messages]

    def test_image_generation_route_rejects_invalid_output_format_without_calling_service(self, client, monkeypatch):
        import eggw.image_generation_service as image_generation_service

        called = False

        def fake_generate(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("service should not be called for invalid output_format")

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generation Format"})
        thread_id = create_resp.json()["id"]
        before_messages = client.get(f"/api/threads/{thread_id}/messages").json()

        response = client.post(
            f"/api/threads/{thread_id}/image-generation",
            json={"prompt": "Paint an egg", "output_format": "gif"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "output_format must be png, jpeg, or webp"
        assert called is False
        after_messages = client.get(f"/api/threads/{thread_id}/messages").json()
        assert [m["id"] for m in after_messages] == [m["id"] for m in before_messages]

    def test_image_generation_route_rejects_conflicting_model_backend_without_calling_service(self, client, monkeypatch):
        import eggw.image_generation_service as image_generation_service

        called = False

        def fake_generate(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("service should not be called for conflicting model/backend")

        monkeypatch.setattr(image_generation_service, "generate_openai_image_artifacts", fake_generate)

        create_resp = client.post("/api/threads", json={"name": "Image Generation Backend"})
        thread_id = create_resp.json()["id"]
        before_messages = client.get(f"/api/threads/{thread_id}/messages").json()

        response = client.post(
            f"/api/threads/{thread_id}/image-generation",
            json={"prompt": "Paint an egg", "model": "Image A", "backend": "Image B"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "model and backend must match when both are provided"
        assert called is False
        after_messages = client.get(f"/api/threads/{thread_id}/messages").json()
        assert [m["id"] for m in after_messages] == [m["id"] for m in before_messages]

    def test_get_provider_output_returns_bytes_and_headers(self, client, test_db_path):
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        create_resp = client.post("/api/threads", json={"name": "Provider Output"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).parent.parent
        data = b"\x89PNG\r\n\x1a\nprovider-image"
        saved = save_provider_output_bytes(
            workspace,
            thread_id,
            data,
            filename="generated image.png",
            mime_type="image/png",
            presentation="image",
        )

        inline = client.get(f"/api/threads/{thread_id}/provider-output/{saved.artifact_id}")
        download = client.get(f"/api/threads/{thread_id}/provider-output/{saved.artifact_id}?download=true")

        assert inline.status_code == 200
        assert inline.content == data
        assert inline.headers["content-type"].startswith("image/png")
        assert inline.headers["x-content-type-options"] == "nosniff"
        assert inline.headers["content-disposition"].startswith("inline;")
        assert "generated%20image.png" in inline.headers["content-disposition"]
        assert download.status_code == 200
        assert download.content == data
        assert download.headers["content-disposition"].startswith("attachment;")

    def test_promote_provider_output_returns_attachment_metadata_without_paths_or_bytes(self, client, test_db_path):
        from eggthreads.input_artifacts import resolve_input_bytes
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        create_resp = client.post("/api/threads", json={"name": "Provider Output Promote"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).parent.parent
        data = b"\x89PNG\r\n\x1a\nprovider-image"
        saved = save_provider_output_bytes(
            workspace,
            thread_id,
            data,
            filename="generated.png",
            mime_type="image/png",
            presentation="image",
            provenance={"kind": "openai_image_generation", "request_id": "req-123"},
            provider_refs={"openai": {"response_id": "resp-123"}},
        )

        response = client.post(f"/api/threads/{thread_id}/provider-output/{saved.artifact_id}/promote")

        assert response.status_code == 200
        payload = response.json()
        metadata = payload["metadata"]
        content_part = payload["content_part"]
        assert payload["input_id"] == metadata["input_id"] == content_part["input_id"]
        assert metadata["owner_thread_id"] == thread_id
        assert metadata["filename"] == "generated.png"
        assert metadata["mime_type"] == "image/png"
        assert metadata["presentation"] == "image"
        assert metadata["size_bytes"] == len(data)
        assert metadata["sha256"] == saved.metadata["sha256"]
        assert metadata["provenance"]["kind"] == "provider_output_promotion"
        assert metadata["provenance"]["source_artifact_id"] == saved.artifact_id
        assert metadata["provenance"]["source_owner_thread_id"] == thread_id
        assert metadata["provenance"]["source_provenance"] == {
            "kind": "openai_image_generation",
            "request_id": "req-123",
        }
        assert metadata["provider_refs"] == {
            "source_provider_output": {
                "artifact_id": saved.artifact_id,
                "owner_thread_id": thread_id,
                "sha256": saved.metadata["sha256"],
            },
            "source_provider_refs": {"openai": {"response_id": "resp-123"}},
        }
        assert content_part == {
            "type": "attachment",
            "input_id": payload["input_id"],
            "owner_thread_id": thread_id,
            "presentation": "image",
            "mime_type": "image/png",
            "filename": "generated.png",
            "size_bytes": len(data),
            "sha256": saved.metadata["sha256"],
            "options": {},
        }
        assert payload["content_text"].startswith("[Attachment: image generated.png image/png")

        resolved_metadata, resolved_bytes = resolve_input_bytes(workspace, core_state.db, thread_id, payload["input_id"])
        assert resolved_metadata["blob_relpath"]
        assert resolved_bytes == data

        encoded = json.dumps(payload)
        assert "blob_relpath" not in encoded
        assert "record_dir" not in encoded
        assert "blob_path" not in encoded
        assert str(workspace) not in encoded
        assert ".egg" not in encoded
        assert "provider-image" not in encoded

    def test_promote_provider_output_parent_can_promote_child_with_explicit_selector(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.input_artifacts import resolve_input_bytes
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        parent_resp = client.post("/api/threads", json={"name": "Provider Output Promote Parent"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        workspace = Path(test_db_path).parent.parent
        saved = save_provider_output_bytes(
            workspace,
            child_id,
            b"child generated",
            filename="child.txt",
            mime_type="text/plain",
            presentation="file",
        )

        response = client.post(
            f"/api/threads/{parent_id}/provider-output/{saved.artifact_id}/promote",
            params={"descendant_thread_id": child_id},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["metadata"]["owner_thread_id"] == parent_id
        assert payload["metadata"]["provenance"]["source_artifact_id"] == saved.artifact_id
        assert payload["metadata"]["provenance"]["source_owner_thread_id"] == child_id
        assert payload["content_part"]["owner_thread_id"] == parent_id
        assert payload["content_part"]["filename"] == "child.txt"
        resolved_metadata, data = resolve_input_bytes(workspace, core_state.db, parent_id, payload["input_id"])
        assert resolved_metadata["owner_thread_id"] == parent_id
        assert data == b"child generated"

    def test_promote_provider_output_parent_without_selector_gets_404_and_no_input(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        parent_resp = client.post("/api/threads", json={"name": "Provider Output Promote Missing Selector"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        workspace = Path(test_db_path).parent.parent
        saved = save_provider_output_bytes(workspace, child_id, b"child generated")

        response = client.post(f"/api/threads/{parent_id}/provider-output/{saved.artifact_id}/promote")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
        assert not (workspace / ".egg" / "egg_inputs" / parent_id).exists()

    def test_promote_provider_output_denies_child_and_sibling_access_without_input(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        parent_resp = client.post("/api/threads", json={"name": "Provider Output Promote Denied"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        sibling_id = create_child_thread(core_state.db, parent_id, name="sibling")
        workspace = Path(test_db_path).parent.parent
        parent_saved = save_provider_output_bytes(workspace, parent_id, b"parent generated")
        sibling_saved = save_provider_output_bytes(workspace, sibling_id, b"sibling generated")

        parent_response = client.post(
            f"/api/threads/{child_id}/provider-output/{parent_saved.artifact_id}/promote",
            params={"descendant_thread_id": parent_id},
        )
        sibling_response = client.post(
            f"/api/threads/{child_id}/provider-output/{sibling_saved.artifact_id}/promote",
            params={"descendant_thread_id": sibling_id},
        )

        assert parent_response.status_code == 403
        assert sibling_response.status_code == 403
        assert not (workspace / ".egg" / "egg_inputs" / child_id).exists()

    def test_promote_provider_output_rejects_bad_ids_without_path_leak_or_input(self, client, test_db_path):
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        create_resp = client.post("/api/threads", json={"name": "Provider Output Promote Bad IDs"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).parent.parent
        saved = save_provider_output_bytes(workspace, thread_id, b"secret provider bytes")

        sha_response = client.post(f"/api/threads/{thread_id}/provider-output/{saved.metadata['sha256']}/promote")
        upper_response = client.post(f"/api/threads/{thread_id}/provider-output/{saved.artifact_id.upper()}/promote")
        path_response = client.post(f"/api/threads/{thread_id}/provider-output/..%2Fbad1/promote")

        assert sha_response.status_code == 400
        assert upper_response.status_code == 400
        assert path_response.status_code == 404
        joined_details = " ".join(
            str(response.json().get("detail"))
            for response in (sha_response, upper_response, path_response)
        )
        assert str(workspace) not in joined_details
        assert ".egg" not in joined_details
        assert not (workspace / ".egg" / "egg_inputs" / thread_id).exists()

    def test_get_provider_output_parent_can_read_child_with_explicit_selector(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        parent_resp = client.post("/api/threads", json={"name": "Provider Output Parent"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        workspace = Path(test_db_path).parent.parent
        saved = save_provider_output_bytes(workspace, child_id, b"child bytes", filename="child.txt", mime_type="text/plain", presentation="file")

        response = client.get(
            f"/api/threads/{parent_id}/provider-output/{saved.artifact_id}",
            params={"descendant_thread_id": child_id},
        )

        assert response.status_code == 200
        assert response.content == b"child bytes"
        assert response.headers["content-type"].startswith("text/plain")

    def test_get_provider_output_parent_without_selector_gets_404(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        parent_resp = client.post("/api/threads", json={"name": "Provider Output Missing Selector"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        workspace = Path(test_db_path).parent.parent
        saved = save_provider_output_bytes(workspace, child_id, b"child bytes")

        response = client.get(f"/api/threads/{parent_id}/provider-output/{saved.artifact_id}")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_provider_output_denies_child_and_sibling_access(self, client, test_db_path):
        from eggthreads import create_child_thread
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        parent_resp = client.post("/api/threads", json={"name": "Provider Output Denied"})
        parent_id = parent_resp.json()["id"]
        child_id = create_child_thread(core_state.db, parent_id, name="child")
        sibling_id = create_child_thread(core_state.db, parent_id, name="sibling")
        workspace = Path(test_db_path).parent.parent
        parent_saved = save_provider_output_bytes(workspace, parent_id, b"parent bytes")
        sibling_saved = save_provider_output_bytes(workspace, sibling_id, b"sibling bytes")

        parent_response = client.get(
            f"/api/threads/{child_id}/provider-output/{parent_saved.artifact_id}",
            params={"descendant_thread_id": parent_id},
        )
        sibling_response = client.get(
            f"/api/threads/{child_id}/provider-output/{sibling_saved.artifact_id}",
            params={"descendant_thread_id": sibling_id},
        )

        assert parent_response.status_code == 403
        assert sibling_response.status_code == 403

    def test_get_provider_output_rejects_sha_and_pathlike_ids_without_path_leak(self, client, test_db_path):
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        create_resp = client.post("/api/threads", json={"name": "Provider Output Bad IDs"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).parent.parent
        saved = save_provider_output_bytes(workspace, thread_id, b"secret bytes")

        sha_response = client.get(f"/api/threads/{thread_id}/provider-output/{saved.metadata['sha256']}")
        path_response = client.get(f"/api/threads/{thread_id}/provider-output/..%2Fbad1")

        assert sha_response.status_code == 400
        assert path_response.status_code == 404
        joined_details = f"{sha_response.json().get('detail')} {path_response.json().get('detail')}"
        assert str(workspace) not in joined_details
        assert ".egg" not in joined_details

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

    def test_get_messages_preserves_persisted_streamed_tool_metadata(self, client):
        """Historical message API returns persisted tool stream metadata."""
        from eggthreads import append_message, create_snapshot

        create_resp = client.post("/api/threads", json={"name": "Stream Metadata UI"})
        thread_id = create_resp.json()["id"]

        msg_id = append_message(
            core_state.db,
            thread_id,
            "assistant",
            "assistant answer",
            extra={
                "tool_stream": {"bash": "streamed tool output body"},
                "tool_calls_stream": {"call_full_1234567890": "streamed arg body"},
            },
        )
        create_snapshot(core_state.db, thread_id)

        response = client.get(f"/api/threads/{thread_id}/messages")

        assert response.status_code == 200
        data = response.json()
        message = next(m for m in data if m["id"] == msg_id)
        assert message["tool_stream"] == {"bash": "streamed tool output body"}
        assert message["tool_calls_stream"] == {"call_full_1234567890": "streamed arg body"}

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

    def test_web_continue_does_not_interrupt_idle_scheduler_pending_ra1(self, client):
        """A resident scheduler is not a live stream and must not cancel pending RA1."""
        from eggthreads import append_message
        from eggthreads.tool_state import discover_runner_actionable_cached

        # Keep this test focused on /continue's interrupt decision.  The
        # scheduler lifecycle layer now correctly restarts stale resident
        # scheduler entries, and a real restarted scheduler could consume the
        # pending RA1 before this assertion inspects it.
        with patch("eggw.commands.thread.ensure_scheduler_for", lambda tid: None):
            create_resp = client.post("/api/threads", json={"name": "Pending RA1 Continue"})
            thread_id = create_resp.json()["id"]
            append_message(core_state.db, thread_id, "user", "Hello")

            # Simulate EggW's normal resident scheduler bookkeeping without
            # creating an open_stream lease for the thread.  Before the fix,
            # /continue treated this dictionary entry as "streaming" and called
            # interrupt_thread(), which appended a purpose='llm' boundary and made
            # the pending user message non-runnable.
            core_state.active_schedulers[thread_id] = {"scheduler": object(), "task": object()}

            response = client.post(
                f"/api/threads/{thread_id}/command",
                json={"command": "/continue"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["was_interrupted"] is False

        interrupts = core_state.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='control.interrupt'",
            (thread_id,),
        ).fetchall()
        assert interrupts == []

        ra = discover_runner_actionable_cached(core_state.db, thread_id)
        assert ra is not None
        assert ra.kind == "RA1_llm"

    def test_eggw_restarts_stale_scheduler_entry(self, client, monkeypatch):
        """A stale process-local scheduler record must not orphan a root."""
        from eggw.core.scheduler import ensure_scheduler_for

        create_resp = client.post("/api/threads", json={"name": "Pending RA1 Continue"})
        thread_id = create_resp.json()["id"]
        core_state.active_schedulers[thread_id] = {"scheduler": object(), "task": object()}

        started: list[str] = []

        class DummyScheduler:
            def __init__(self, *args, **kwargs):
                started.append(kwargs["root_thread_id"])

            async def run_forever(self, poll_sec=0.05):
                await asyncio.sleep(3600)

        class LiveTask:
            def done(self):
                return False

            def add_done_callback(self, callback):
                return None

        def fake_create_task(coro):
            coro.close()
            return LiveTask()

        monkeypatch.setattr("eggw.core.scheduler.SubtreeScheduler", DummyScheduler)
        monkeypatch.setattr("eggw.core.scheduler.asyncio.create_task", fake_create_task)

        ensure_scheduler_for(thread_id)

        assert started == [thread_id]
        entry = core_state.active_schedulers[thread_id]
        assert entry["task"].done() is False


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

    def test_partial_output_approval_stashes_full_output_artifact(self, client, monkeypatch, tmp_path):
        """Partial output approval preserves full output as a readable artifact."""
        from eggthreads import append_message, build_tool_call_states

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("eggw.routes.tools.ensure_scheduler_for", lambda tid: None)
        create_resp = client.post("/api/threads", json={"name": "Partial Output Approval"})
        thread_id = create_resp.json()["id"]
        tool_call_id = "call-partial-output-web"
        full_output = "\n".join(f"line {i:03d} " + ("x" * 40) for i in range(250))

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
                        "function": {"name": "bash", "arguments": json.dumps({"script": "long"})},
                    }
                ]
            },
        )
        core_state.db.append_event(
            event_id="approve-partial-output-web",
            thread_id=thread_id,
            type_="tool_call.approval",
            payload={"tool_call_id": tool_call_id, "decision": "granted"},
        )
        core_state.db.append_event(
            event_id="start-partial-output-web",
            thread_id=thread_id,
            type_="tool_call.execution_started",
            payload={"tool_call_id": tool_call_id},
        )
        core_state.db.append_event(
            event_id="finish-partial-output-web",
            thread_id=thread_id,
            type_="tool_call.finished",
            payload={"tool_call_id": tool_call_id, "reason": "success", "output": full_output},
        )
        assert build_tool_call_states(core_state.db, thread_id)[tool_call_id].state == "TC4"

        response = client.post(
            f"/api/threads/{thread_id}/tools/approve",
            json={"tool_call_id": tool_call_id, "approved": True, "output_decision": "partial"},
        )

        assert response.status_code == 200
        row = core_state.db.conn.execute(
            """
            SELECT payload_json FROM events
             WHERE thread_id=? AND type='tool_call.output_approval'
             ORDER BY event_seq DESC LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        payload = json.loads(row[0])
        assert payload["tool_call_id"] == tool_call_id
        assert payload["decision"] == "partial"
        assert payload["line_count"] == len(full_output.splitlines())
        assert payload["char_count"] == len(full_output)

        preview = payload["preview"]
        assert "Preview only" in preview
        assert "Artifact id:" in preview
        assert "read_long_tool_output(" in preview

        artifact_path = Path(payload["artifact_path"])
        assert artifact_path.is_dir()
        assert str(artifact_path) not in preview

        metadata_path = artifact_path / "metadata.json"
        chunk_path = artifact_path / "chunk-0001.txt"
        assert metadata_path.is_file()
        assert chunk_path.is_file()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["thread_id"] == thread_id
        assert metadata["tool_call_id"] == tool_call_id
        assert metadata["original_line_count"] == len(full_output.splitlines())
        assert metadata["original_char_count"] == len(full_output)
        assert chunk_path.read_text(encoding="utf-8").startswith("line 000")


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

    def test_eggw_dispatch_covers_shared_registry_commands(self):
        """Shared command catalog entries should have EggW dispatch coverage."""
        from eggthreads.command_catalog import create_default_command_registry
        from eggw.commands import dispatch_command

        tree = ast.parse(inspect.getsource(dispatch_command))
        dispatched_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq):
                    if (
                        isinstance(left, ast.Name)
                        and left.id == "command_name"
                        and isinstance(comparator, ast.Constant)
                        and isinstance(comparator.value, str)
                    ):
                        dispatched_names.add(comparator.value)
                    if (
                        isinstance(comparator, ast.Name)
                        and comparator.id == "command_name"
                        and isinstance(left, ast.Constant)
                        and isinstance(left.value, str)
                    ):
                        dispatched_names.add(left.value)
                left = comparator

        shared_names = set(create_default_command_registry().names(include_aliases=True))
        assert shared_names - dispatched_names == set()

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
        assert "Threads/Agents/Subagents:" in data["message"]
        assert "/spawnChildThread <text>" in data["message"]
        assert "/spawnAutoApprovedChildThread <text>" in data["message"]
        assert "/waitForThreads <threads>" in data["message"]
        assert "/setThreadPriority ..." in data["message"]
        assert "/schedulers" in data["message"]
        assert "EggW-only commands:" in data["message"]
        assert "/theme [name]" in data["message"]
        assert "/rename <name>" in data["message"]
        assert "/spawn <context>" not in data["message"]
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

    def test_spawn_alias_is_not_supported(self, client):
        """EggW should use /spawnChildThread, not the old /spawn alias."""
        create_resp = client.post("/api/threads", json={"name": "No Spawn Alias"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/spawn"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["message"] == "Unknown command: /spawn"

    def test_wait_for_threads_command_queues_shared_wait_tool(self, client, monkeypatch):
        """EggW /waitForThreads queues the shared wait tool call instead of blocking."""
        from eggthreads import build_tool_call_states, create_child_thread

        started: list[str] = []
        monkeypatch.setattr("eggw.commands.ensure_scheduler_for", lambda tid: started.append(tid))
        create_resp = client.post("/api/threads", json={"name": "Wait Manager"})
        thread_id = create_resp.json()["id"]
        child_a = create_child_thread(core_state.db, thread_id, name="Wait Child A")
        child_b = create_child_thread(core_state.db, thread_id, name="Wait Child B")

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/waitForThreads {child_a[-8:]},{child_b}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Queued /wait" in data["message"]
        assert data["data"] == {"start_schedulers": [thread_id]}
        assert started == [thread_id]

        row = core_state.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        payload = json.loads(row[0])
        assert payload["role"] == "user"
        assert payload["keep_user_turn"] is True
        assert payload["user_command_type"] == "/wait"

        tool_call = payload["tool_calls"][0]
        assert tool_call["function"]["name"] == "wait"
        args = json.loads(tool_call["function"]["arguments"])
        assert args["thread_ids"] == [child_a, child_b]

        states = build_tool_call_states(core_state.db, thread_id)
        tc = states[tool_call["id"]]
        assert tc.approval_decision == "granted"
        assert tc.state == "TC2.1"

    def test_wait_for_threads_unknown_selector_fails_without_queueing(self, client, monkeypatch):
        """Unknown /waitForThreads selectors report a useful error before queueing."""
        started: list[str] = []
        monkeypatch.setattr("eggw.commands.ensure_scheduler_for", lambda tid: started.append(tid))
        create_resp = client.post("/api/threads", json={"name": "Wait Manager"})
        thread_id = create_resp.json()["id"]

        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": "/waitForThreads definitely-missing"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "no thread matches selector 'definitely-missing'" in data["message"]
        assert started == []

        rows = core_state.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create'",
            (thread_id,),
        ).fetchall()
        payloads = [json.loads(row[0]) for row in rows]
        assert not [payload for payload in payloads if payload.get("tool_calls")]
        assert not [payload for payload in payloads if payload.get("user_command_type") == "/wait"]

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
        created.clear()

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

    def test_command_autocomplete_advertises_only_shared_or_eggw_only_commands(self, client):
        """Command autocomplete should not drift from shared or explicit EggW-only commands."""
        from eggthreads.command_catalog import EGGW_COMMAND_COMPLETIONS, create_default_command_registry

        eggw_only = {
            "rename",
            "theme",
            "attach",
            "attachments",
            "attachOutput",
            "saveProviderArtifact",
            "saveProviderOutput",
            "clearAttachments",
            "imageGenerate",
        }
        assert {f"/{name}" for name in eggw_only} <= set(EGGW_COMMAND_COMPLETIONS)
        assert "/spawn" not in EGGW_COMMAND_COMPLETIONS

        advertised_names = {cmd.removeprefix("/") for cmd in EGGW_COMMAND_COMPLETIONS}
        allowed_names = set(create_default_command_registry().names(include_aliases=True)) | eggw_only
        assert advertised_names - allowed_names == set()

        for name in eggw_only:
            line = f"/{name[:3]}"
            response = client.get("/api/autocomplete", params={"line": line, "cursor": len(line)})
            assert response.status_code == 200
            displays = {s["display"] for s in response.json()["suggestions"]}
            assert f"/{name}" in displays

        response = client.get("/api/autocomplete", params={"line": "/spawn", "cursor": len("/spawn")})
        assert response.status_code == 200
        displays = {s["display"] for s in response.json()["suggestions"]}
        assert "/spawnChildThread" in displays
        assert "/spawn" not in displays

    def test_eggw_advertises_all_meaningful_terminal_egg_commands(self, client):
        """Keep EggW's textual command surface in sync with terminal Egg."""
        from egg.attachments import register_attachment_commands
        from egg.image_generation import register_image_generation_command
        from egg.theme import register_theme_command
        from eggthreads.command_catalog import EGGW_COMMAND_COMPLETIONS, create_default_command_registry

        class DummyApp:
            def log_system(self, *_args, **_kwargs):
                pass

            def apply_theme(self, theme):
                return theme

            def redraw_static_view(self, *_args, **_kwargs):
                pass

        registry = create_default_command_registry()
        dummy = DummyApp()
        register_theme_command(registry, dummy)
        register_attachment_commands(registry, dummy)
        register_image_generation_command(registry, dummy)

        terminal_egg_commands = set(registry.names(include_aliases=True))
        eggw_commands = {command.removeprefix("/") for command in EGGW_COMMAND_COMPLETIONS}

        assert terminal_egg_commands - eggw_commands == set()

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

    def test_image_generate_command_autocomplete(self, client, monkeypatch, tmp_path):
        from eggw.core import state as core_state

        models_path = tmp_path / "models.json"
        image_models_path = tmp_path / "image-generation-models.json"
        models_path.write_text(
            json.dumps({
                "providers": {
                    "openai-images": {
                        "api_base": "https://api.openai.com/v1/images/generations",
                        "api_key_env": "OPENAI_API_KEY",
                        "models": {},
                    }
                }
            }),
            encoding="utf-8",
        )
        image_models_path.write_text(
            json.dumps({
                "models": {
                    "Image Backend": {
                        "provider": "openai-images",
                        "api_type": "openai_images",
                        "model_name": "gpt-image-1",
                    }
                }
            }),
            encoding="utf-8",
        )
        old_models_path = core_state.MODELS_PATH
        old_image_models_path = core_state.IMAGE_GENERATION_MODELS_PATH
        try:
            core_state.MODELS_PATH = models_path
            core_state.IMAGE_GENERATION_MODELS_PATH = image_models_path

            response = client.get(
                "/api/autocomplete",
                params={"line": "/imageGenerate model=I", "cursor": len("/imageGenerate model=I")},
            )

            assert response.status_code == 200
            suggestions = response.json()["suggestions"]
            assert any(s["insert"] == "model='Image Backend'" for s in suggestions)
        finally:
            core_state.MODELS_PATH = old_models_path
            core_state.IMAGE_GENERATION_MODELS_PATH = old_image_models_path

    def test_artifact_command_argument_autocomplete(self, client, test_db_path, tmp_path, monkeypatch):
        from eggthreads.provider_output_artifacts import save_provider_output_bytes

        create_resp = client.post("/api/threads", json={"name": "Artifact Autocomplete"})
        thread_id = create_resp.json()["id"]
        workspace = Path(test_db_path).resolve().parent.parent
        saved = save_provider_output_bytes(
            workspace,
            thread_id,
            b"generated image bytes",
            filename="generated-cat.png",
            mime_type="image/png",
            presentation="image",
        )

        response = client.get(
            "/api/autocomplete",
            params={"line": "/attachOutput ", "cursor": len("/attachOutput "), "thread_id": thread_id},
        )

        assert response.status_code == 200
        suggestions = response.json()["suggestions"]
        assert any(s["insert"] == saved.artifact_id for s in suggestions)
        assert any("generated-cat.png" in s["display"] for s in suggestions)

        (tmp_path / "exports").mkdir(exist_ok=True)
        monkeypatch.chdir(tmp_path)
        line = f"/saveProviderArtifact {saved.artifact_id} exp"
        response = client.get(
            "/api/autocomplete",
            params={"line": line, "cursor": len(line), "thread_id": thread_id},
        )

        assert response.status_code == 200
        inserts = [s["insert"] for s in response.json()["suggestions"]]
        assert any("exports/" in item for item in inserts)
        assert saved.artifact_id not in inserts
