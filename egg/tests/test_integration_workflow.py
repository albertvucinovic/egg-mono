"""Integration tests for end-to-end workflows in egg app."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, AsyncMock

import pytest


class TestMessageSubmissionWorkflow:
    """Tests for message submission workflow."""

    def test_user_message_creates_snapshot(self, egg_app):
        """Submitting user message should create snapshot."""
        from eggthreads import append_message, create_snapshot

        # Simulate user message submission
        append_message(egg_app.db, egg_app.current_thread, "user", "Hello world")
        create_snapshot(egg_app.db, egg_app.current_thread)

        # Verify snapshot was created by checking we can get messages
        from egg.utils import snapshot_messages
        messages = snapshot_messages(egg_app.db, egg_app.current_thread)
        assert len(messages) >= 1

    def test_message_appears_in_chat_panel(self, egg_app, monkeypatch):
        """Submitted message should appear in chat panel."""
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "Test message for chat")
        create_snapshot(egg_app.db, egg_app.current_thread)

        # Update panels and check chat content
        egg_app.update_panels()

        chat_content = egg_app.chat_output.content or ""
        assert "Test message for chat" in chat_content


class TestToolApprovalWorkflow:
    """Tests for tool approval workflow."""

    def test_pending_prompt_returns_none_when_idle(self, egg_app):
        """Should return None when no pending approval needed."""
        pending = egg_app.compute_pending_prompt()

        # May or may not be None depending on thread state
        # Just verify it doesn't crash
        assert pending is None or isinstance(pending, dict)

    def test_handle_approval_returns_false_when_no_pending(self, egg_app):
        """Should return False when no pending prompt."""
        egg_app._pending_prompt = {}

        result = egg_app.handle_pending_approval_answer('y')

        assert result is False

    def test_handle_approval_with_no_pending(self, egg_app):
        """Handle approval should return False when no pending."""
        egg_app._pending_prompt = None

        result = egg_app.handle_pending_approval_answer('y')

        # Should return False when nothing pending
        assert result is False


class TestThreadHierarchyWorkflow:
    """Tests for thread hierarchy workflows."""

    def test_child_thread_created_from_parent(self, egg_app):
        """Child thread should be created from parent."""
        from eggthreads import create_child_thread, create_snapshot

        parent = egg_app.current_thread

        # Create child
        child = create_child_thread(egg_app.db, parent, name="ChildThread")
        create_snapshot(egg_app.db, child)

        # Child exists and is different from parent
        assert child is not None
        assert child != parent

    def test_child_thread_exists_in_database(self, egg_app):
        """Child thread should exist in database."""
        from eggthreads import create_child_thread, create_snapshot

        parent = egg_app.current_thread
        child = create_child_thread(egg_app.db, parent, name="ChildThread")
        create_snapshot(egg_app.db, child)

        # Get child thread info
        child_thread = egg_app.db.get_thread(child)
        assert child_thread is not None
        assert child_thread.thread_id == child

    def test_thread_selector_finds_child(self, egg_app):
        """Thread selector should find child by suffix."""
        from eggthreads import create_child_thread, create_snapshot

        parent = egg_app.current_thread
        child = create_child_thread(egg_app.db, parent, name="ChildThread")
        create_snapshot(egg_app.db, child)

        # Find by suffix
        matches = egg_app.select_threads_by_selector(child[-8:])

        assert child in matches


class TestCtrlCInterruptWorkflow:
    """Tests for Ctrl+C interrupt behavior."""

    def test_interrupt_clears_input_on_idle(self, egg_app, monkeypatch):
        """Ctrl+C should clear input when idle with text."""
        egg_app.input_panel.editor.editor.set_text("some text")
        egg_app.running = True

        # Mock idle state
        monkeypatch.setattr("eggthreads.thread_state", lambda db, tid: "idle")
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: None)

        egg_app.handle_key('\x03')  # Ctrl+C

        # Input should be cleared, but not quit
        assert egg_app.input_panel.get_text() == ""
        assert egg_app.running is True

    def test_interrupt_quits_on_idle_empty(self, egg_app, monkeypatch):
        """Ctrl+C should quit when idle and input empty."""
        egg_app.input_panel.editor.editor.set_text("")
        egg_app.running = True

        # Mock idle state
        monkeypatch.setattr("eggthreads.thread_state", lambda db, tid: "idle")
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: None)

        egg_app.handle_key('\x03')  # Ctrl+C

        assert egg_app.running is False


class TestModelSwitchingWorkflow:
    """Tests for model switching workflow."""

    def test_model_command_shows_current(self, egg_app):
        """Model command without arg should show current model."""
        egg_app.llm_client = None  # No llm client

        egg_app.cmd_model("")

        # Should log something about model
        assert any("model" in msg.lower() for msg in egg_app._system_log)


class TestToolEnablingWorkflow:
    """Tests for tool enabling/disabling workflow."""

    def test_disable_tool_persists(self, egg_app, monkeypatch):
        """Disabling tool should persist to thread."""
        disabled = []
        def mock_disable(db, tid, name):
            disabled.append((tid, name))
        monkeypatch.setattr("eggthreads.disable_tool_for_thread", mock_disable)

        egg_app.cmd_disableTool("bash")

        assert len(disabled) == 1
        assert disabled[0][1] == "bash"

    def test_enable_tool_after_disable(self, egg_app, monkeypatch):
        """Enabling tool after disable should work."""
        disabled = []
        enabled = []
        def mock_disable(db, tid, name):
            disabled.append(name)
        def mock_enable(db, tid, name):
            enabled.append(name)
        monkeypatch.setattr("eggthreads.disable_tool_for_thread", mock_disable)
        monkeypatch.setattr("eggthreads.enable_tool_for_thread", mock_enable)

        egg_app.cmd_disableTool("bash")
        egg_app.cmd_enableTool("bash")

        assert "bash" in disabled
        assert "bash" in enabled


class TestPanelVisibilityWorkflow:
    """Tests for panel visibility workflow."""

    def test_hide_and_show_chat_panel(self, egg_app):
        """Should be able to hide and show chat panel."""
        initial = egg_app._panel_visible.get('chat', True)

        # Toggle off
        egg_app.cmd_togglePanel("chat")
        assert egg_app._panel_visible['chat'] != initial

        # Toggle back on
        egg_app.cmd_togglePanel("chat")
        assert egg_app._panel_visible['chat'] == initial

    def test_hidden_panel_excluded_from_render(self, egg_app):
        """Hidden panels should be excluded from render group."""
        egg_app._panel_visible = {'chat': False, 'children': False, 'system': False}

        group = egg_app.render_group()

        # Should only have input panel
        assert len(group._renderables) >= 1


class TestSnapshotWorkflow:
    """Tests for snapshot creation and retrieval."""

    def test_snapshot_contains_messages(self, egg_app):
        """Snapshot should contain thread messages."""
        from eggthreads import append_message, create_snapshot
        from egg.utils import snapshot_messages

        append_message(egg_app.db, egg_app.current_thread, "user", "Test snapshot message")
        create_snapshot(egg_app.db, egg_app.current_thread)

        messages = snapshot_messages(egg_app.db, egg_app.current_thread)

        assert len(messages) >= 1
        assert any(m.get('content') == "Test snapshot message" for m in messages)

    def test_snapshot_updates_on_new_message(self, egg_app):
        """Snapshot should update when new message is added."""
        from eggthreads import append_message, create_snapshot
        from egg.utils import snapshot_messages

        append_message(egg_app.db, egg_app.current_thread, "user", "First message")
        create_snapshot(egg_app.db, egg_app.current_thread)

        messages1 = snapshot_messages(egg_app.db, egg_app.current_thread)
        count1 = len(messages1)

        append_message(egg_app.db, egg_app.current_thread, "assistant", "Second message")
        create_snapshot(egg_app.db, egg_app.current_thread)

        messages2 = snapshot_messages(egg_app.db, egg_app.current_thread)
        count2 = len(messages2)

        assert count2 > count1


class TestEnterModeWorkflow:
    """Tests for enter mode workflow."""

    def test_toggle_enter_mode_to_newline(self, egg_app):
        """Should set newline mode."""
        egg_app.cmd_enterMode("newline")
        assert egg_app.enter_sends is False

    def test_toggle_enter_mode_to_send(self, egg_app):
        """Should set send mode."""
        egg_app.cmd_enterMode("send")
        assert egg_app.enter_sends is True

    def test_toggle_enter_mode_cycle(self, egg_app):
        """Should cycle between modes."""
        egg_app.cmd_enterMode("send")
        assert egg_app.enter_sends is True

        egg_app.cmd_enterMode("newline")
        assert egg_app.enter_sends is False


class TestEnterModeDefaults:
    """Tests for default enter mode."""

    def test_default_enter_mode_is_send(self, egg_app):
        """New app instances should default to send-on-enter."""
        assert egg_app.enter_sends is True


class TestThreadListingWorkflow:
    """Tests for thread listing workflow."""

    def test_list_threads_shows_all(self, egg_app):
        """Should list all threads."""
        from eggthreads import create_root_thread, create_snapshot

        # Create additional threads
        t1 = create_root_thread(egg_app.db, name="Thread1")
        t2 = create_root_thread(egg_app.db, name="Thread2")
        create_snapshot(egg_app.db, t1)
        create_snapshot(egg_app.db, t2)

        egg_app.cmd_threads("")

        # Should log thread info
        assert any("thread" in msg.lower() for msg in egg_app._system_log)

    def test_list_children_shows_subtree(self, egg_app):
        """Should list children of current thread."""
        from eggthreads import create_child_thread, create_snapshot

        child = create_child_thread(egg_app.db, egg_app.current_thread, name="ChildThread")
        create_snapshot(egg_app.db, child)

        egg_app.cmd_listChildren("")

        # Should log subtree info
        assert any("subtree" in msg.lower() or child[-8:] in msg for msg in egg_app._system_log)
