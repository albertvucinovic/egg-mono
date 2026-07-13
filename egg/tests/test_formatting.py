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

    def test_keeps_orphan_runtime_roots_visible(self, egg_app):
        """Legacy orphan @runtime:* rows remain visible/inspectable."""
        orphan_id = "01ZZZZZZZZZZZZZZZZZZZZZZRT"
        egg_app.db.create_thread(
            thread_id=orphan_id,
            name="@runtime:python",
            parent_id=None,
            initial_model_key=None,
            depth=1,
        )

        tree = egg_app.format_tree()

        assert egg_app.current_thread[-8:] in tree
        assert orphan_id[-8:] in tree


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

    def test_recovery_notice_uses_continue_status_label(self, isolated_db):
        from eggthreads import create_root_thread, create_snapshot
        from eggthreads.api import append_recovery_notice
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="RecoveryLabel")
        append_recovery_notice(isolated_db, tid, "Manual /continue applied")
        create_snapshot(isolated_db, tid)

        class MinimalApp:
            def __init__(self):
                self.db = isolated_db
                self.current_thread = tid

        class TestApp(FormattingMixin, MinimalApp):
            pass

        text = TestApp().format_messages_text(tid)

        assert "[Continue Status" in text
        assert "[System" not in text

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

    def test_output_optimizer_metadata_is_shown_for_tool_messages_without_body_clutter(self, isolated_db):
        from eggthreads import append_message, create_root_thread, create_snapshot
        from egg.formatting import FormattingMixin

        tid = create_root_thread(isolated_db, name="OptimizerFormatting")
        optimized = append_message(
            isolated_db,
            tid,
            "tool",
            "optimized preview body",
            extra={
                "name": "bash",
                "tool_call_id": "call_optimized",
                "output_optimizer": {
                    "optimized": True,
                    "summary": "Egg optimized · 95% saved · raw available",
                    "summary_with_artifact": "Egg optimized · 95% saved · raw artifact rawabc123",
                    "artifact_id": "rawabc123",
                },
            },
        )
        default = append_message(
            isolated_db,
            tid,
            "tool",
            "plain preview body",
            extra={"name": "bash", "tool_call_id": "call_default"},
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

        assert f"[Tool: bash [msg_id: {optimized}] [tool_call_id: call_optimized]] [Egg optimized · 95% saved · raw artifact rawabc123]" in text
        assert f"[Tool: bash [msg_id: {default}] [tool_call_id: call_default]]" in text
        assert "raw artifact rawabc123" in text
        assert "plain preview body" not in text
        assert "plain preview body [Egg optimized" not in text

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

    def test_chat_panel_header_includes_cached_total_cost(self, egg_app, monkeypatch):
        """Chat panel text shows total cost without doing a separate stats scan."""
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "Test message")
        create_snapshot(egg_app.db, egg_app.current_thread)
        calls = {"stats": 0}

        def fake_token_stats(**kwargs):
            calls["stats"] += 1
            return 42, {
                "total_input_tokens": 100,
                "total_output_tokens": 20,
                "approx_call_count": 1,
                "cost_usd": {"total": 0.0123},
            }

        monkeypatch.setattr(egg_app, "current_token_stats", fake_token_stats)

        text = egg_app.compose_chat_panel_text()

        assert "$0.0123 cost" in text.splitlines()[0]
        assert calls["stats"] == 1


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

        assert calls == {"stats": 1, "max_seq": 0}

    def test_active_llm_stream_token_stats_reuses_stale_cache_until_stream_ends(self, egg_app, monkeypatch):
        """LLM streaming should prefer stale header token stats over rescans."""
        calls = {"stats": 0, "max_seq": 0}
        egg_app._live_state = {"active_invoke": "invoke-llm", "stream_kind": "llm"}

        def fake_thread_token_stats(db, thread_id, llm=None):
            calls["stats"] += 1
            return {"context_tokens": calls["stats"], "api_usage": {"total_input_tokens": 1}}

        def fake_max_event_seq(tid):
            calls["max_seq"] += 1
            return 100 + calls["max_seq"]

        monkeypatch.setattr("eggthreads.thread_token_stats", fake_thread_token_stats)
        monkeypatch.setattr(egg_app.db, "max_event_seq", fake_max_event_seq)

        assert egg_app.current_token_stats()[0] == 1
        assert egg_app.current_token_stats()[0] == 1
        assert calls == {"stats": 1, "max_seq": 0}

        egg_app._live_state = {"active_invoke": None, "stream_kind": None}
        assert egg_app.current_token_stats()[0] == 2
        assert calls == {"stats": 2, "max_seq": 0}

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

        assert calls == {"stats": 1, "max_seq": 0}

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


class TestFormatChildrenPanel:
    """Tests for adaptive Children panel subtree density."""

    @staticmethod
    def _child(egg_app, parent_id, suffix):
        parent = egg_app.db.get_thread(parent_id)
        thread_id = f"panel-child-{suffix:08d}"
        egg_app.db.create_thread(
            thread_id=thread_id,
            name=f"Child {suffix}",
            parent_id=parent_id,
            initial_model_key=parent.initial_model_key,
            depth=parent.depth + 1,
        )
        return thread_id

    def test_always_shows_current_id_name_and_description_on_one_line(self, egg_app):
        root = egg_app.db.get_thread(egg_app.current_thread)
        egg_app.db.conn.execute(
            "UPDATE threads SET name=?, short_recap=? WHERE thread_id=?",
            ("Current [root]\nname", "Description [details]\ncontinued", root.thread_id),
        )
        egg_app.db.conn.commit()

        first_line = egg_app.format_children_panel(root.thread_id).splitlines()[0]

        assert first_line == (
            f"[bold cyan]Current:[/] {root.thread_id} [dim]|[/] "
            r"[bold]Name:[/] Current \[root] name [dim]|[/] "
            r"[bold]Description:[/] Description \[details] continued"
        )

    def test_missing_current_metadata_uses_explicit_placeholders(self, egg_app):
        root_id = egg_app.current_thread
        egg_app.db.conn.execute(
            "UPDATE threads SET name=NULL, short_recap=NULL WHERE thread_id=?",
            (root_id,),
        )
        egg_app.db.conn.commit()

        first_line = egg_app.format_children_panel(root_id).splitlines()[0]

        assert first_line == (
            f"[bold cyan]Current:[/] {root_id} [dim]|[/] "
            "[bold]Name:[/] Unnamed [dim]|[/] "
            "[bold]Description:[/] No description"
        )

    def test_maximal_through_four_descendants_excludes_view_root_from_count(
        self, egg_app, monkeypatch
    ):
        parent = egg_app.current_thread
        child = parent
        for number in range(4):
            child = self._child(egg_app, child, number)

        calls = []
        monkeypatch.setattr(
            egg_app, "format_tree", lambda root, **kwargs: calls.append((root, kwargs)) or "MAXIMAL TREE"
        )

        text = egg_app.format_children_panel(parent)

        assert text.endswith("MAXIMAL TREE")
        assert f"Current:[/] {parent}" in text.splitlines()[0]
        assert calls == [(parent, {"include_root": False})]

    def test_maximal_leaf_does_not_repeat_current_as_tree_row(self, egg_app):
        text = egg_app.format_children_panel(egg_app.current_thread)

        assert len(text.splitlines()) == 1
        assert text.count(egg_app.current_thread) == 1
        assert "[CUR]" not in text

    @pytest.mark.parametrize("descendant_count", [5, 15])
    def test_non_maximal_boundaries_show_useful_status_groups(
        self, egg_app, descendant_count, monkeypatch
    ):
        parent = egg_app.current_thread
        descendant_ids = []
        branch_parent = parent
        for number in range(descendant_count):
            child = self._child(egg_app, branch_parent, number)
            descendant_ids.append(child)
            branch_parent = child

        streaming = descendant_ids[-1]
        assert egg_app.db.try_open_stream(
            streaming, "compact-stream", "2999-01-01 00:00:00",
            owner="test", purpose="assistant_stream"
        )
        monkeypatch.setattr(
            egg_app,
            "format_tree",
            lambda root: pytest.fail("non-maximal rendering must not format the full tree"),
        )

        lines = egg_app.format_children_panel(parent).splitlines()

        assert len(lines) == 5
        assert "Streaming (1):" in lines[1]
        assert streaming[-8:] in lines[1]
        assert f"{descendant_count} descendants · 1 direct · {descendant_count - 1} nested" in lines[2]
        assert "Direct (non-streaming):[/]" in lines[3]
        assert "Nested (non-streaming):[/]" in lines[4]
        assert "└─" not in "\n".join(lines)

    def test_non_maximal_groups_escape_id_suffixes(self, egg_app):
        parent = egg_app.current_thread
        ids = [self._child(egg_app, parent, number) for number in range(4)]
        marked_up_id = "panel-child-[x]12345"
        egg_app.db.create_thread(
            thread_id=marked_up_id,
            name="Markup",
            parent_id=parent,
            initial_model_key=egg_app.db.get_thread(parent).initial_model_key,
            depth=1,
        )
        ids.append(marked_up_id)
        for number, thread_id in enumerate((ids[1], ids[3])):
            assert egg_app.db.try_open_stream(
                thread_id, f"stream-{number}", "2999-01-01 00:00:00",
                owner="test", purpose="assistant_stream"
            )

        lines = egg_app.format_children_panel(parent).splitlines()

        assert "Streaming (2):" in lines[1]
        assert ids[1][-8:] in lines[1]
        assert ids[3][-8:] in lines[1]
        assert "Other descendants (3):" in lines[2]
        assert ids[1][-8:] not in lines[2]
        assert ids[3][-8:] not in lines[2]

    def test_large_subtree_shows_streaming_id_and_bounded_previews(
        self, egg_app, monkeypatch
    ):
        parent = egg_app.current_thread
        descendants = [self._child(egg_app, parent, number) for number in range(37)]
        streaming = descendants[-1]
        assert egg_app.db.try_open_stream(
            streaming, "minimal-stream", "2999-01-01 00:00:00",
            owner="test", purpose="assistant_stream"
        )
        monkeypatch.setattr(
            egg_app,
            "format_tree",
            lambda root: pytest.fail("large rendering must not format the full tree"),
        )

        lines = egg_app.format_children_panel(parent).splitlines()

        assert len(lines) == 3
        assert "Streaming (1):" in lines[1]
        assert streaming[-8:] in lines[1]
        assert "Other descendants (36):" in lines[2]
        assert "+32 more" in lines[2]
        assert streaming[-8:] not in lines[2]

    def test_counts_only_descendants_of_selected_root(self, egg_app):
        outer_root = egg_app.current_thread
        selected = self._child(egg_app, outer_root, 100)
        outside = self._child(egg_app, outer_root, 101)
        selected_descendants = [
            self._child(egg_app, selected, number) for number in range(5)
        ]
        for number in range(16):
            self._child(egg_app, outside, 200 + number)
        assert egg_app.db.try_open_stream(
            outside, "outside-stream", "2999-01-01 00:00:00",
            owner="test", purpose="assistant_stream"
        )
        assert egg_app.db.try_open_stream(
            selected_descendants[0], "inside-stream", "2999-01-01 00:00:00",
            owner="test", purpose="assistant_stream"
        )

        text = egg_app.format_children_panel(selected)

        assert "Other descendants (4):" in text
        assert "Streaming (1):" in text
        assert "Direct children" not in text
        assert "Nested descendants" not in text
        assert outside[-8:] not in text

    def test_non_maximal_omits_empty_streaming_row(self, egg_app):
        parent = egg_app.current_thread
        descendants = [self._child(egg_app, parent, number) for number in range(5)]

        text = egg_app.format_children_panel(parent)

        assert "Streaming" not in text
        assert "Descendants (5):" in text
        assert all(thread_id[-8:] in text for thread_id in descendants[-4:])

    def test_format_tree_stays_maximal_for_large_subtrees(self, egg_app):
        parent = egg_app.current_thread
        descendants = [self._child(egg_app, parent, number) for number in range(16)]

        text = egg_app.format_tree(parent)

        assert parent[-8:] in text
        assert all(thread_id[-8:] in text for thread_id in descendants)
        assert "16 descendants" not in text
        assert "└─" in text

    def test_format_tree_can_render_only_descendant_forest(self, egg_app):
        parent = egg_app.current_thread
        child = self._child(egg_app, parent, 900)
        grandchild = self._child(egg_app, child, 901)

        text = egg_app.format_tree(parent, include_root=False)

        assert parent[-8:] not in text
        assert child[-8:] in text
        assert grandchild[-8:] in text


def test_inline_stream_panel_materializes_only_bounded_chunked_tail(egg_app, monkeypatch):
    from eggdisplay import ChunkedText
    from egg.formatting import LIVE_PANEL_TEXT_MAX_CHARS

    class CountingText(ChunkedText):
        def __init__(self):
            self.full_materializations = 0
            super().__init__()

        def to_string(self):
            self.full_materializations += 1
            raise AssertionError("bounded live panel must not join the whole stream")

    content = CountingText()
    content.append("old" * 100_000)
    content.append("visible-tail")
    egg_app._display_is_inline = True
    egg_app._live_state = egg_app._make_live_state(
        active_invoke="invoke", stream_kind="llm"
    )
    egg_app._live_state["content"] = content
    monkeypatch.setattr(egg_app, "current_token_stats", lambda **kwargs: (None, {}))

    panel = egg_app.compose_chat_panel_text()

    assert "visible-tail" in panel
    assert len(panel) < LIVE_PANEL_TEXT_MAX_CHARS + 10_000
    assert content.full_materializations == 0
