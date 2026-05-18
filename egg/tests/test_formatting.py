"""Tests for formatting.py FormattingMixin."""
from __future__ import annotations

import json
import uuid

import pytest


class TestMinHiddenActivitySummary:
    """Tests for shared min-verbosity hidden activity summaries."""

    def test_formats_counts_tools_names_and_tokens(self):
        from egg.min_run_summary import MinHiddenActivitySummary, format_min_hidden_activity_summary

        summary = MinHiddenActivitySummary()
        summary.add_tool_execution(name="bash", tokens=40, tool_call_id="call_bash")
        summary.add_tool_execution(name="python_repl", tokens=20, tool_call_id="call_python")
        summary.add_tool_result(name="bash", tokens=30)
        summary.add_tool_result(name="python_repl", tokens=20)
        summary.add_reasoning_block(tokens=13)

        assert format_min_hidden_activity_summary(summary) == (
            "Executed 2 tools, got 2 tool results, 1 reasoning block, total tokens 123\n"
            "Tools: bash, python_repl"
        )


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
        # Mock current_open to return a stream with non-expired lease
        mock_row = {
            "purpose": "assistant_stream",
            "lease_until": "9999-12-31T23:59:59",  # Far future = not expired
        }
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: mock_row)

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
        # Create minimal app with mocked scheduler
        class MinimalApp:
            def __init__(self):
                self.db = db
                self.current_thread = tid
                self._live_state = {"active_invoke": None, "content": "", "tools": {}, "tc_text": {}, "tc_order": []}

        from egg.formatting import FormattingMixin
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

        from egg.formatting import FormattingMixin
        class TestApp(FormattingMixin, MinimalApp):
            pass

        app = TestApp()
        text = app.format_messages_text(tid)

        # Check for actual message content
        assert "Hello!" in text
        assert "Hi there!" in text

    def test_includes_full_message_ids_for_copyable_commands(self, isolated_db):
        """Text transcript includes full msg_ids for /compact and /continue."""
        from eggthreads import append_message, create_root_thread, create_snapshot

        tid = create_root_thread(isolated_db, name="MessageIds")
        user = append_message(isolated_db, tid, "user", "hello")
        assistant = append_message(isolated_db, tid, "assistant", "hi")
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid
                self._live_state = {"active_invoke": None, "content": "", "tools": {}, "tc_text": {}, "tc_order": []}

        from egg.formatting import FormattingMixin

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert f"msg_id: {user}" in text
        assert f"msg_id: {assistant}" in text

    def test_display_verbosity_default_max_preserves_reasoning_and_tool_output(self, isolated_db):
        """Default max text should match the existing full-body transcript format."""
        from eggthreads import append_message, create_root_thread, create_snapshot
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="VerbosityMax")
        user = append_message(isolated_db, tid, "user", "run the tool")
        assistant = append_message(
            isolated_db,
            tid,
            "assistant",
            "assistant answer",
            extra={
                "reasoning": "private reasoning body",
                "tool_calls": [
                    {
                        "id": "call_full_1234567890",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"script":"echo hello"}'},
                    }
                ],
                "tool_stream": {"bash": "streamed tool output body"},
                "tool_calls_stream": {"call_full_1234567890": "streamed arg body"},
            },
        )
        tool = append_message(
            isolated_db,
            tid,
            "tool",
            "completed tool result body",
            extra={"name": "bash", "tool_call_id": "call_full_1234567890"},
        )
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert text == (
            f"[User [msg_id: {user}]]\nrun the tool\n\n"
            f"[Reasoning [msg_id: {assistant}]]\nprivate reasoning body\n\n"
            f"[Assistant [msg_id: {assistant}]]\nassistant answer\n\n"
            "[ToolCall] bash {\"script\":\"echo hello\"}\n\n"
            "[Tool Output: bash]\nstreamed tool output body\n\n"
            "[Tool Call Args: call_full_1234567890]\nstreamed arg body\n\n"
            f"[Tool: bash [msg_id: {tool}]]\ncompleted tool result body"
        )

    def test_display_verbosity_medium_hides_detail_bodies_but_keeps_headers_and_ids(self, isolated_db):
        """Medium should collapse reasoning and tool results while preserving ids."""
        from eggthreads import append_message, create_root_thread, create_snapshot
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="VerbosityMedium")
        append_message(isolated_db, tid, "user", "run the tool")
        assistant = append_message(
            isolated_db,
            tid,
            "assistant",
            "assistant answer",
            extra={
                "reasoning": "private reasoning body",
                "tool_calls": [
                    {
                        "id": "call_full_1234567890",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"script":"echo hello and then produce a much longer argument preview for truncation testing"}',
                        },
                    }
                ],
            },
        )
        tool = append_message(
            isolated_db,
            tid,
            "tool",
            "completed tool result body",
            extra={"name": "bash", "tool_call_id": "call_full_1234567890"},
        )
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid
                self._display_verbosity = "medium"

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert "assistant answer" in text
        assert f"[Reasoning [msg_id: {assistant}]]" in text
        assert "private reasoning body" not in text
        assert f"[Tool Calls [msg_id: {assistant}]]" in text
        assert "[ToolCall [tool_call_id: call_full_1234567890]] bash" in text
        assert f"[Tool: bash [msg_id: {tool}] [tool_call_id: call_full_1234567890]]" in text
        assert "completed tool result body" not in text

    def test_display_verbosity_min_shows_conversation_and_run_summary(self, isolated_db):
        """Min should show user/assistant bodies and summarize hidden activity runs."""
        from eggthreads import append_message, create_root_thread, create_snapshot
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="VerbosityMin")
        append_message(isolated_db, tid, "system", "ordinary system prompt")
        user1 = append_message(isolated_db, tid, "user", "first question")
        assistant = append_message(
            isolated_db,
            tid,
            "assistant",
            "assistant answer",
            extra={
                "reasoning": "private reasoning body",
                "tool_calls": [
                    {
                        "id": "call_full_1234567890",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"script":"echo hello"}'},
                    }
                ],
            },
        )
        tool = append_message(
            isolated_db,
            tid,
            "tool",
            "completed tool result body",
            extra={"name": "bash", "tool_call_id": "call_full_1234567890"},
        )
        user2 = append_message(isolated_db, tid, "user", "next question")
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid
                self._display_verbosity = "min"

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert f"[User [msg_id: {user1}]]\nfirst question" in text
        assert f"[Assistant [msg_id: {assistant}]]\nassistant answer" in text
        assert f"[User [msg_id: {user2}]]\nnext question" in text
        assert "ordinary system prompt" in text
        assert "private reasoning body" not in text
        assert "completed tool result body" not in text
        assert "Hidden details:" not in text
        assert "1 reasoning block" in text
        assert "Executed 1 tool, got 1 tool result" in text
        assert "total tokens" in text
        assert "Tools: bash" in text
        assert f"[Reasoning [msg_id: {assistant}]]" not in text
        assert "[ToolCall [tool_call_id: call_full_1234567890]] bash" not in text
        assert f"[Tool: bash [msg_id: {tool}] [tool_call_id: call_full_1234567890]]" not in text
        assert text.index("1 reasoning block") < text.index("assistant answer")
        assert text.index("Executed 1 tool, got 1 tool result") < text.index("next question")

    def test_display_verbosity_min_merges_consecutive_hidden_activity(self, isolated_db):
        """Consecutive hidden activity between visible messages becomes one summary item."""
        from eggthreads import append_message, create_root_thread, create_snapshot
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="VerbosityMinMerged")
        append_message(isolated_db, tid, "user", "run both tools")
        append_message(
            isolated_db,
            tid,
            "assistant",
            "",
            extra={
                "reasoning": "private reasoning body",
                "tool_calls": [
                    {
                        "id": "call_bash_1234567890",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"script":"echo hello"}'},
                    },
                    {
                        "id": "call_python_1234567890",
                        "type": "function",
                        "function": {"name": "python_repl", "arguments": '{"code":"print(1)"}'},
                    },
                ],
            },
        )
        append_message(
            isolated_db,
            tid,
            "tool",
            "bash result body",
            extra={"name": "bash", "tool_call_id": "call_bash_1234567890"},
        )
        append_message(
            isolated_db,
            tid,
            "tool",
            "python result body",
            extra={"name": "python_repl", "tool_call_id": "call_python_1234567890"},
        )
        append_message(isolated_db, tid, "assistant", "done")
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid
                self._display_verbosity = "min"

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert "Executed 2 tools, got 2 tool results, 1 reasoning block" in text
        assert "Tools: bash, python_repl" in text
        assert text.count("Executed 2 tools, got 2 tool results, 1 reasoning block") == 1
        assert "Hidden details:" not in text
        assert "bash result body" not in text
        assert "python result body" not in text
        assert text.index("Executed 2 tools, got 2 tool results, 1 reasoning block") < text.index("done")

    @pytest.mark.parametrize("verbosity", ["max", "medium", "min"])
    def test_answer_user_preserve_turn_note_visible_at_all_verbosities(self, isolated_db, verbosity):
        """Interim assistant notes remain full visible conversation, not hidden detail."""
        from eggthreads import append_message, create_root_thread, create_snapshot
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="AnswerNoteFormatting")
        note = append_message(
            isolated_db,
            tid,
            "assistant",
            "Interim note body",
            extra={
                "answer_user_preserve_turn": True,
                "tool_calls": [
                    {
                        "id": "call_note",
                        "type": "function",
                        "function": {"name": "answer_user_while_preserving_llm_turn", "arguments": '{"message":"Interim note body"}'},
                    }
                ],
            },
        )
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid
                self._display_verbosity = verbosity

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert f"[Assistant Note [msg_id: {note}]]\nInterim note body" in text
        if verbosity == "max":
            assert "[ToolCall] answer_user_while_preserving_llm_turn" in text
        elif verbosity == "medium":
            assert "[Tool Calls" in text
            assert "answer_user_while_preserving_llm_turn" in text
        else:
            assert "Executed 1 tool" in text
            assert "answer_user_while_preserving_llm_turn" in text
        assert "Hidden details" not in text

    def test_shows_compaction_marker_without_hiding_history(self, isolated_db):
        """Chat transcript text should include a divider and keep old messages."""
        from eggthreads import append_message, commit_thread_compaction, create_root_thread, create_snapshot

        tid = create_root_thread(isolated_db, name="CompactionUI")
        old = append_message(isolated_db, tid, "user", "old visible history")
        start = append_message(isolated_db, tid, "assistant", "compact summary")
        after = append_message(isolated_db, tid, "user", "new question")
        commit_thread_compaction(isolated_db, tid, start, created_by="test")
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid
                self._live_state = {"active_invoke": None, "content": "", "tools": {}, "tc_text": {}, "tc_order": []}

        from egg.formatting import FormattingMixin

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert "old visible history" in text
        assert "compact summary" in text
        assert "new question" in text
        assert "Compaction boundary: API context now starts at msg_" in text
        assert start[-8:] in text
        assert text.index("old visible history") < text.index("Compaction boundary") < text.index("compact summary")
        assert old and after


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



    def test_caches_unchanged_idle_token_stats_until_snapshot_changes(self, egg_app, monkeypatch):
        """Repeated idle panel ticks should not rescan token stats by TTL."""
        calls = {"count": 0}

        def fake_thread_token_stats(db, thread_id, llm=None):
            calls["count"] += 1
            return {"context_tokens": 7, "api_usage": {"total_input_tokens": 1}}

        monkeypatch.setattr("eggthreads.thread_token_stats", fake_thread_token_stats)
        monkeypatch.setattr(egg_app.db, "max_event_seq", lambda tid: 3)
        ticks = iter([100.0, 10000.0])
        monkeypatch.setattr("egg.formatting.time.monotonic", lambda: next(ticks))

        assert egg_app.current_token_stats()[0] == 7
        assert egg_app.current_token_stats()[0] == 7
        assert calls["count"] == 1

    def test_idle_token_stats_rescans_when_snapshot_changes(self, egg_app, monkeypatch):
        """Snapshot watermark changes should invalidate idle token stats."""
        calls = {"count": 0}
        snapshot_seq = {"value": 1}

        def fake_thread_token_stats(db, thread_id, llm=None):
            calls["count"] += 1
            return {"context_tokens": calls["count"], "api_usage": {"total_input_tokens": 1}}

        monkeypatch.setattr("eggthreads.thread_token_stats", fake_thread_token_stats)
        monkeypatch.setattr(egg_app, "_snapshot_last_event_seq", lambda tid: snapshot_seq["value"])

        assert egg_app.current_token_stats()[0] == 1
        snapshot_seq["value"] = 2
        assert egg_app.current_token_stats()[0] == 2
        assert calls["count"] == 2

    def test_idle_token_stats_cache_ignores_unrelated_event_seq_changes(self, egg_app, monkeypatch):
        """Idle token stats should not rescan for config-only event changes."""
        calls = {"count": 0}

        def fake_thread_token_stats(db, thread_id, llm=None):
            calls["count"] += 1
            return {"context_tokens": 7, "api_usage": {"total_input_tokens": 1}}

        monkeypatch.setattr("eggthreads.thread_token_stats", fake_thread_token_stats)
        monkeypatch.setattr(egg_app.db, "max_event_seq", lambda tid: 999)

        egg_app.current_token_stats()
        egg_app.db.append_event(
            event_id="model-switch-cache-test",
            thread_id=egg_app.current_thread,
            type_="model.switch",
            payload={"model_key": "other"},
        )
        egg_app.current_token_stats()

        assert calls["count"] == 1

    def test_active_token_stats_ttl_avoids_event_seq_probe(self, egg_app, monkeypatch):
        """Typing redraws during streams should reuse stats before hitting DB."""
        calls = {"stats": 0, "max_seq": 0}
        egg_app._live_state = {"active_invoke": "invoke-live"}

        def fake_thread_token_stats(db, thread_id, llm=None):
            calls["stats"] += 1
            return {"context_tokens": 7, "api_usage": {"total_input_tokens": 1}}

        def fake_max_event_seq(tid):
            calls["max_seq"] += 1
            return calls["max_seq"]

        monkeypatch.setattr("eggthreads.thread_token_stats", fake_thread_token_stats)
        monkeypatch.setattr(egg_app.db, "max_event_seq", fake_max_event_seq)
        ticks = iter([100.0, 100.1])
        monkeypatch.setattr("egg.formatting.time.monotonic", lambda: next(ticks))

        assert egg_app.current_token_stats()[0] == 7
        assert egg_app.current_token_stats()[0] == 7

        assert calls == {"stats": 1, "max_seq": 1}

    def test_tool_stream_token_stats_reuse_snapshot_cache_without_rescan(self, egg_app, monkeypatch):
        """Tool streaming should not rescan huge snapshots just to refresh ctx."""
        calls = {"stats": 0, "max_seq": 0}
        egg_app._live_state = {"active_invoke": "invoke-tool", "stream_kind": "tool"}

        def fake_thread_token_stats(db, thread_id, llm=None):
            calls["stats"] += 1
            return {"context_tokens": 7, "api_usage": {"total_input_tokens": 1}}

        def fake_max_event_seq(tid):
            calls["max_seq"] += 1
            return 100 + calls["max_seq"]

        monkeypatch.setattr("eggthreads.thread_token_stats", fake_thread_token_stats)
        monkeypatch.setattr(egg_app.db, "max_event_seq", fake_max_event_seq)

        assert egg_app.current_token_stats()[0] == 7
        assert egg_app.current_token_stats()[0] == 7

        assert calls == {"stats": 1, "max_seq": 1}

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
