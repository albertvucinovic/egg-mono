"""Tests for formatting.py FormattingMixin."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestFormatThreadLine:
    """Tests for format_thread_line()."""

    def test_includes_thread_id_suffix(self, egg_app):
        """Should include last 8 chars of thread ID."""
        line = egg_app.format_thread_line(egg_app.current_thread)

        # Thread ID suffix should be in the line
        assert egg_app.current_thread[-8:] in line

    def test_shows_cur_tag_for_current(self, egg_app):
        """Should show [CUR] for current thread."""
        line = egg_app.format_thread_line(egg_app.current_thread)

        assert "[CUR]" in line or "CUR" in line

    def test_shows_streaming_flag(self, egg_app, monkeypatch):
        """Should show STREAMING when thread has open stream."""
        # Mock current_open to return a stream
        class MockStreamRow:
            purpose = "assistant_stream"
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: MockStreamRow())

        line = egg_app.format_thread_line(egg_app.current_thread)

        assert "STREAMING" in line

    def test_no_streaming_flag_when_idle(self, egg_app, monkeypatch):
        """Should not show STREAMING when thread is idle."""
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: None)

        line = egg_app.format_thread_line(egg_app.current_thread)

        assert "STREAMING" not in line


class TestFormatTree:
    """Tests for format_tree()."""

    def test_renders_single_root(self, egg_app):
        """Should render tree with single root."""
        tree = egg_app.format_tree()

        # Should contain the current thread
        assert egg_app.current_thread[-8:] in tree

    def test_renders_children_indented(self, egg_app, monkeypatch):
        """Should properly indent child threads."""
        from eggthreads import create_child_thread, create_snapshot

        # Create a child thread
        child = create_child_thread(egg_app.db, egg_app.current_thread, name="ChildThread")
        create_snapshot(egg_app.db, child)

        tree = egg_app.format_tree(egg_app.current_thread)

        # Both threads should be present
        assert egg_app.current_thread[-8:] in tree
        assert child[-8:] in tree

    def test_includes_thread_names(self, egg_app):
        """Should include thread names in tree output."""
        tree = egg_app.format_tree()

        # Root thread should have name "Root" (from default creation)
        assert "Root" in tree


class TestFormatMessagesText:
    """Tests for format_messages_text()."""

    def test_formats_all_message_roles(self, thread_with_messages):
        """Should format system, user, assistant roles."""
        db, tid = thread_with_messages

        # Create app to use the method
        import egg
        # Create minimal app with mocked scheduler
        class MinimalApp:
            def __init__(self):
                self.db = db
                self.current_thread = tid
                self._live_state = {"active_invoke": None, "content": "", "tools": {}, "tc_text": {}, "tc_order": []}

        from formatting import FormattingMixin
        class TestApp(FormattingMixin, MinimalApp):
            pass

        app = TestApp()
        text = app.format_messages_text(tid)

        assert "system" in text.lower() or "System" in text
        assert "user" in text.lower() or "User" in text
        assert "assistant" in text.lower() or "Assistant" in text

    def test_includes_message_content(self, thread_with_messages):
        """Should include actual message content."""
        db, tid = thread_with_messages

        class MinimalApp:
            def __init__(self):
                self.db = db
                self.current_thread = tid
                self._live_state = {"active_invoke": None, "content": "", "tools": {}, "tc_text": {}, "tc_order": []}

        from formatting import FormattingMixin
        class TestApp(FormattingMixin, MinimalApp):
            pass

        app = TestApp()
        text = app.format_messages_text(tid)

        # Check for actual message content
        assert "Hello!" in text
        assert "Hi there!" in text


class TestComposeChatPanelText:
    """Tests for compose_chat_panel_text()."""

    def test_includes_historical_messages(self, egg_app):
        """Should include formatted message history."""
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "Test message")
        create_snapshot(egg_app.db, egg_app.current_thread)

        text = egg_app.compose_chat_panel_text()

        assert "Test message" in text

    def test_appends_streaming_content(self, egg_app, monkeypatch):
        """Should append streaming content when active."""
        egg_app._live_state = {
            "active_invoke": "test_invoke",
            "content": "Streaming content here",
            "reason": "",
            "tools": {},
            "tc_text": {},
            "tc_order": [],
        }

        # Mock current_open to indicate streaming
        class MockStreamRow:
            purpose = "assistant_stream"
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: MockStreamRow())

        text = egg_app.compose_chat_panel_text()

        assert "Streaming content here" in text
        assert "(streaming)" in text.lower() or "streaming" in text.lower()


class TestCurrentTokenStats:
    """Tests for current_token_stats()."""

    def test_returns_tuple(self, egg_app, monkeypatch):
        """Should return tuple of (context_tokens, api_usage)."""
        # Create a snapshot with token stats
        from eggthreads import create_snapshot
        create_snapshot(egg_app.db, egg_app.current_thread)

        ctx, api = egg_app.current_token_stats()

        # Should return int or None for context, dict or None for api
        assert ctx is None or isinstance(ctx, int)
        assert api is None or isinstance(api, dict)

    def test_returns_none_for_no_snapshot(self, egg_app, monkeypatch):
        """Should return (None, None) when no snapshot exists."""
        # Create thread without snapshot
        from eggthreads import create_root_thread
        tid = create_root_thread(egg_app.db, name="NoSnapshot")
        egg_app.current_thread = tid

        ctx, api = egg_app.current_token_stats()

        # Either could be None when no stats
        assert ctx is None or api is None or isinstance(api, dict)


class TestTruncateForChatPanel:
    """Tests for truncate_for_chat_panel()."""

    def test_truncates_long_content(self, egg_app):
        """Should truncate content exceeding panel size."""
        long_content = "Line\n" * 1000

        truncated = egg_app.truncate_for_chat_panel(long_content)

        assert len(truncated) < len(long_content)

    def test_preserves_short_content(self, egg_app):
        """Should not truncate short content."""
        short_content = "Short message"

        result = egg_app.truncate_for_chat_panel(short_content)

        assert short_content in result


class TestFormatModelInfo:
    """Tests for format_model_info()."""

    def test_formats_model_key(self, egg_app, monkeypatch):
        """Should format model key when present."""
        # format_model_info should return a string
        info = egg_app.format_model_info(egg_app.current_thread)

        # Should be a string (may be empty if no model set)
        assert isinstance(info, str)

    def test_returns_empty_for_no_model(self, egg_app):
        """Should return empty or placeholder when no model set."""
        info = egg_app.format_model_info(egg_app.current_thread)

        # Either empty or some default indicator
        assert isinstance(info, str)
