"""Tests for panels.py PanelsMixin."""
from __future__ import annotations

import asyncio
import json

import pytest


class TestUpdatePanels:
    """Tests for update_panels()."""

    def test_updates_chat_output_content(self, egg_app, monkeypatch):
        """Should update chat_output with composed text."""
        set_content_calls = []
        original_set_content = egg_app.chat_output.set_content
        def mock_set_content(text):
            set_content_calls.append(text)
            original_set_content(text)
        monkeypatch.setattr(egg_app.chat_output, "set_content", mock_set_content)

        egg_app.update_panels()

        assert len(set_content_calls) >= 1

    def test_system_output_body_is_empty(self, egg_app):
        """System panel body is empty — status lives in the title line."""
        egg_app.update_panels()

        assert egg_app.system_output.content == ""

    def test_system_output_title_shows_status(self, egg_app):
        """System panel title carries sandbox and auto-approval status."""
        egg_app.update_panels()

        title = egg_app.system_output.title
        assert title.startswith("System")
        assert "Sandboxing" in title
        assert "Autoapproval" in title

    def test_updates_children_output(self, egg_app, monkeypatch):
        """Should update children_output with tree view."""
        set_content_calls = []
        original_set_content = egg_app.children_output.set_content
        def mock_set_content(text):
            set_content_calls.append(text)
            original_set_content(text)
        monkeypatch.setattr(egg_app.children_output, "set_content", mock_set_content)

        egg_app.update_panels()

        assert len(set_content_calls) >= 1

    def test_children_tree_is_not_reformatted_when_status_key_unchanged(self, egg_app, monkeypatch):
        """Idle panel ticks should not rescan the full thread tree."""
        calls = {"count": 0}

        def fake_format_tree(thread_id):
            calls["count"] += 1
            return f"tree for {thread_id[-8:]}"

        monkeypatch.setattr(egg_app, "format_tree", fake_format_tree)

        egg_app.update_panels()
        egg_app.update_panels()

        assert calls["count"] == 1

    def test_idle_update_panels_does_not_recompute_children_status_key(self, egg_app, monkeypatch):
        """Idle panel ticks should not repeat the expensive Children DB key."""
        calls = {"count": 0}

        def fake_status_key():
            calls["count"] += 1
            return (egg_app.current_thread, "children-key", calls["count"])

        monkeypatch.setattr(egg_app, "_compute_children_panel_status_key", fake_status_key)

        egg_app.update_panels()
        egg_app.update_panels()

        assert calls["count"] == 1

    def test_children_dirty_invalidation_refreshes_tree_before_fallback(self, egg_app, monkeypatch):
        """A watcher/explicit dirty mark should refresh before the 1s fallback."""
        calls = {"count": 0}

        def fake_format_tree(thread_id):
            calls["count"] += 1
            return f"tree {calls['count']} for {thread_id[-8:]}"

        monkeypatch.setattr(egg_app, "format_tree", fake_format_tree)
        monkeypatch.setattr(egg_app, "_compute_children_panel_status_key", lambda: ("stable-key",))

        egg_app.update_panels()
        egg_app._children_panel_next_status_check_at = 9999999999.0

        egg_app._mark_children_panel_dirty()
        egg_app.update_panels()

        assert calls["count"] == 2

    def test_typing_does_not_reformat_children_tree(self, egg_app, monkeypatch):
        """Input echo should not wait on expensive children tree refreshes."""
        calls = {"count": 0}

        def fake_format_tree(thread_id):
            calls["count"] += 1
            return f"tree {calls['count']} for {thread_id[-8:]}"

        monkeypatch.setattr(egg_app, "format_tree", fake_format_tree)

        egg_app.update_panels()
        assert calls["count"] == 1
        egg_app.input_panel.render()

        egg_app.input_panel.editor.editor.insert_text("x")

        def changed_status_key():
            return ("changed-by-background-heartbeat", calls["count"])

        monkeypatch.setattr(egg_app, "_compute_children_panel_status_key", changed_status_key)
        egg_app.update_panels()

        assert calls["count"] == 1

    def test_children_status_key_ignores_lease_heartbeat_extension(self, egg_app):
        """Children tree cache key should not churn on lease_until heartbeats."""
        from datetime import datetime, timedelta, timezone

        tid = egg_app.current_thread
        invoke = "invoke-stable-key"
        lease_1 = (datetime.now(timezone.utc) + timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
        lease_2 = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")

        assert egg_app.db.try_open_stream(tid, invoke, lease_1, owner="test", purpose="tool")
        key_1 = egg_app._compute_children_panel_status_key()
        assert egg_app.db.heartbeat(tid, invoke, lease_2)
        key_2 = egg_app._compute_children_panel_status_key()

        assert key_1 == key_2

    def test_shows_approval_panel_when_pending(self, egg_app):
        """Should show approval panel when pending prompt exists."""
        egg_app._pending_prompt = {'kind': 'exec'}

        egg_app.update_panels()

        content = egg_app.approval_panel.content
        assert content and "approval" in content.lower()

    def test_hides_approval_panel_when_no_pending(self, egg_app):
        """Should hide approval panel when no pending prompt."""
        egg_app._pending_prompt = {}

        egg_app.update_panels()

        content = egg_app.approval_panel.content
        assert not content or content.strip() == ""

    def test_shows_exec_approval_message(self, egg_app):
        """Should show exec approval message."""
        egg_app._pending_prompt = {'kind': 'exec'}

        egg_app.update_panels()

        content = egg_app.approval_panel.content
        assert "Execution approval" in content or "execution" in content.lower()

    def test_shows_output_approval_message(self, egg_app):
        """Should show output approval message."""
        egg_app._pending_prompt = {'kind': 'output'}

        egg_app.update_panels()

        content = egg_app.approval_panel.content
        assert "Output approval" in content or "output" in content.lower()

    def test_updates_sandbox_status_in_title(self, egg_app, monkeypatch):
        """Should update system title with sandbox status."""
        monkeypatch.setattr(
            "eggthreads.get_thread_sandbox_status",
            lambda db, tid: {'effective': True, 'provider': 'docker'}
        )

        egg_app.update_panels()

        assert "Sandbox" in egg_app.system_output.title or "sandbox" in egg_app.system_output.title.lower()

    def test_system_title_shows_autoapproval_flag(self, egg_app, monkeypatch):
        monkeypatch.setattr(
            "eggthreads.get_thread_sandbox_status",
            lambda db, tid: {'effective': False}
        )
        monkeypatch.setattr("eggthreads.get_thread_auto_approval_status", lambda db, tid: True)

        egg_app.update_panels()

        assert "Autoapproval[On]" in egg_app.system_output.title

    def test_system_status_helpers_are_cached_while_config_events_unchanged(self, egg_app, monkeypatch):
        """Idle panel ticks should not re-read sandbox/autoapproval state."""
        calls = {"sandbox": 0, "auto": 0}

        def fake_sandbox_status(db, tid):
            calls["sandbox"] += 1
            return {'effective': False}

        def fake_auto_status(db, tid):
            calls["auto"] += 1
            return False

        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", fake_sandbox_status)
        monkeypatch.setattr("eggthreads.get_thread_auto_approval_status", fake_auto_status)

        egg_app.update_panels()
        egg_app.update_panels()

        assert calls == {"sandbox": 1, "auto": 1}



    def test_live_tps_is_cached_briefly(self, egg_app, monkeypatch):
        """Multiple header reads in one UI tick should reuse live TPS."""
        calls = {"count": 0}
        egg_app._live_state = {"active_invoke": "invoke-tps", "stream_kind": "llm"}

        def fake_live_tps(db, invoke):
            calls["count"] += 1
            return 12.0

        monkeypatch.setattr("eggthreads.live_llm_tps_for_invoke", fake_live_tps)

        assert egg_app.current_stream_tps()
        assert egg_app.current_stream_tps()
        assert calls["count"] == 1

    def test_chat_header_tps_uses_snapshot_seq_cache(self, egg_app, monkeypatch):
        """Idle header TPS reads should not reparse snapshot messages."""
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "assistant", "done", extra={"tps": 8.0})
        create_snapshot(egg_app.db, egg_app.current_thread)
        egg_app._live_state = {"active_invoke": None, "stream_kind": None}
        calls = {"count": 0}

        def fake_snapshot_messages(db, thread_id):
            calls["count"] += 1
            return [{"role": "assistant", "content": "done", "tps": 8.0}]

        monkeypatch.setattr("egg.panels.snapshot_messages", fake_snapshot_messages)

        assert egg_app.current_chat_header_tps() == "8.0 tps"
        assert egg_app.current_chat_header_tps() == "8.0 tps"
        assert calls["count"] == 1

class TestRenderGroup:
    """Tests for render_group()."""

    def test_includes_visible_panels(self, egg_app):
        """Should include visible panels in group."""
        egg_app._panel_visible = {'system': True, 'children': True, 'chat': True}

        group = egg_app.render_group()

        # Group should have renderables
        assert group._renderables  # Group contains renderable items

    def test_excludes_hidden_system_panel(self, egg_app):
        """Should exclude system panel when hidden."""
        egg_app._panel_visible = {'system': False, 'children': True, 'chat': True}

        group = egg_app.render_group()

        # Should still have items (children + chat + input)
        assert group._renderables

    def test_excludes_hidden_children_panel(self, egg_app):
        """Should exclude children panel when hidden."""
        egg_app._panel_visible = {'system': True, 'children': False, 'chat': True}

        group = egg_app.render_group()

        assert group._renderables

    def test_excludes_hidden_chat_panel(self, egg_app):
        """Should exclude chat panel when hidden."""
        egg_app._panel_visible = {'system': True, 'children': True, 'chat': False}

        group = egg_app.render_group()

        assert group._renderables

    def test_always_includes_input_panel(self, egg_app):
        """Should always include input panel."""
        egg_app._panel_visible = {'system': False, 'children': False, 'chat': False}

        group = egg_app.render_group()

        # Should have at least input panel
        assert len(group._renderables) >= 1

    def test_includes_approval_panel_when_pending(self, egg_app):
        """Should include approval panel when pending prompt exists."""
        egg_app._pending_prompt = {'kind': 'exec'}
        egg_app.approval_panel.content = "Approval needed"

        group = egg_app.render_group()

        # Should have more renderables when approval is shown
        assert group._renderables


class TestLogSystem:
    """Tests for log_system()."""

    def test_appends_to_system_log(self, egg_app):
        """Should append message to _system_log."""
        egg_app._system_log = []

        egg_app.log_system("Test message")

        assert "Test message" in egg_app._system_log

    def test_creates_system_log_if_missing(self, egg_app):
        """Should create _system_log if it doesn't exist."""
        if hasattr(egg_app, '_system_log'):
            delattr(egg_app, '_system_log')

        egg_app.log_system("New message")

        assert hasattr(egg_app, '_system_log')
        assert "New message" in egg_app._system_log

    def test_preserves_existing_messages(self, egg_app):
        """Should preserve existing messages when appending."""
        egg_app._system_log = ["First", "Second"]

        egg_app.log_system("Third")

        assert egg_app._system_log == ["First", "Second", "Third"]


class TestConsolePrintMessage:
    """Tests for console_print_message()."""

    @staticmethod
    def _collect_panel_titles(printed):
        titles = []
        for args, _kwargs in printed:
            for arg in args:
                title = getattr(arg, "title", None)
                if title is not None:
                    titles.append(str(title))
        return titles

    def test_static_message_builder_returns_renderables_without_printing(self, egg_app, monkeypatch):
        """Static message builder should be reusable without console side effects."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        items = egg_app._static_transcript_message_renderables({
            'role': 'user',
            'content': 'Hello',
            'msg_id': 'msg_builder_123',
        })

        assert printed == []
        assert len(items) == 1
        panel = items[0].renderable
        assert 'User' in str(getattr(panel, 'title', ''))
        assert 'msg_builder_123' in str(getattr(panel, 'title', ''))

    def test_static_compaction_builder_returns_renderable_without_printing(self, egg_app, monkeypatch):
        """Compaction marker builder should be reusable without console side effects."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        item = egg_app._static_transcript_compaction_marker_renderable({
            'start_msg_id': 'msg_compaction_builder_12345678',
        })

        assert printed == []
        panel = item.renderable
        assert 'Compaction Boundary' in str(getattr(panel, 'title', ''))
        body = getattr(panel, 'renderable', None)
        assert 'msg_12345678' in str(getattr(body, 'plain', body))

    def test_prints_user_message(self, egg_app, monkeypatch):
        """Should print user message with green style."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({'role': 'user', 'content': 'Hello'})

        assert len(printed) >= 1

    def test_prints_assistant_message(self, egg_app, monkeypatch):
        """Should print assistant message with cyan style."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({'role': 'assistant', 'content': 'Response'})

        assert len(printed) >= 1

    def test_prints_system_message(self, egg_app, monkeypatch):
        """Should print system message with blue style."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({'role': 'system', 'content': 'System prompt'})

        assert len(printed) >= 1

    def test_prints_tool_message(self, egg_app, monkeypatch):
        """Should print tool message with yellow style."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({'role': 'tool', 'name': 'bash', 'content': 'output'})

        assert len(printed) >= 1

    def test_prints_error_message_in_red(self, egg_app, monkeypatch):
        """Should print LLM error message in red."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({'role': 'system', 'content': 'LLM Error: Connection failed'})

        assert len(printed) >= 1

    def test_prints_reasoning_if_present(self, egg_app, monkeypatch):
        """Should print reasoning content if present."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({
            'role': 'assistant',
            'content': 'Answer',
            'reasoning': 'Let me think...'
        })

        # Should print multiple panels (reasoning + content)
        assert len(printed) >= 2

    def test_prints_tps_in_reasoning_and_assistant_titles(self, egg_app, monkeypatch):
        """Reasoning and assistant panels should show TPS when present."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({
            'role': 'assistant',
            'content': 'Answer',
            'reasoning': 'Let me think...',
            'tps': 4.2,
        })

        titles = self._collect_panel_titles(printed)
        reasoning_titles = [t for t in titles if 'Reasoning' in t]
        assistant_titles = [t for t in titles if 'Assistant' in t]

        assert any('4.2 tps' in t for t in reasoning_titles)
        assert any('4.2 tps' in t for t in assistant_titles)

    def test_prints_tool_calls_if_present(self, egg_app, monkeypatch):
        """Should print tool calls if present."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({
            'role': 'assistant',
            'content': '',
            'tool_calls': [{'function': {'name': 'bash', 'arguments': {'cmd': 'ls'}}}]
        })

        assert len(printed) >= 1

    def test_prints_tps_in_tool_calls_title(self, egg_app, monkeypatch):
        """Tool calls panel should show TPS when present on the assistant message."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({
            'role': 'assistant',
            'content': '',
            'tps': 5.5,
            'tool_calls': [{'function': {'name': 'bash', 'arguments': {'cmd': 'ls'}}}],
        })

        titles = self._collect_panel_titles(printed)
        tool_call_titles = [t for t in titles if 'Tool Calls' in t]

        assert any('5.5 tps' in t for t in tool_call_titles)

    def test_prints_tps_in_tool_message_title(self, egg_app, monkeypatch):
        """Tool message panel should show TPS when present."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({'role': 'tool', 'name': 'bash', 'content': 'output', 'tps': 3.0})

        titles = self._collect_panel_titles(printed)
        tool_titles = [t for t in titles if 'bash' in t]

        assert any('3.0 tps' in t for t in tool_titles)

    @staticmethod
    def _collect_panel_bodies(printed):
        bodies = []
        for args, _kwargs in printed:
            for arg in args:
                renderable = getattr(arg, "renderable", None)
                if renderable is not None:
                    bodies.append(str(getattr(renderable, "plain", renderable)))
        return bodies


    def test_display_verbosity_default_max_keeps_detail_bodies(self, egg_app, monkeypatch):
        """Default max panels should keep reasoning and tool-result bodies."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({
            'role': 'assistant',
            'content': 'Answer body',
            'reasoning': 'Private reasoning body',
        })
        egg_app.console_print_message({'role': 'tool', 'name': 'bash', 'content': 'Completed tool result body'})

        joined_bodies = "\n".join(self._collect_panel_bodies(printed))
        assert 'Private reasoning body' in joined_bodies
        assert 'Completed tool result body' in joined_bodies

    def test_display_verbosity_medium_collapses_reasoning_and_tool_results(self, egg_app, monkeypatch):
        """Medium panels should retain titles/ids but hide noisy bodies."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))
        egg_app._display_verbosity = "medium"

        egg_app.console_print_message({
            'role': 'assistant',
            'content': 'Answer body',
            'reasoning': 'Private reasoning body',
            'tool_calls': [{
                'id': 'call_full_1234567890',
                'function': {'name': 'bash', 'arguments': {'cmd': 'echo hello'}},
            }],
            'msg_id': 'msg_assistant_medium',
            'tps': 4.2,
        })
        egg_app.console_print_message({
            'role': 'tool',
            'name': 'bash',
            'content': 'Completed tool result body',
            'msg_id': 'msg_tool_medium',
            'tool_call_id': 'call_full_1234567890',
            'tps': 3.0,
        })

        titles = self._collect_panel_titles(printed)
        bodies = self._collect_panel_bodies(printed)

        assert any('Reasoning' in title and 'msg_id: msg_assistant_medium' in title and '4.2 tps' in title for title in titles)
        assert any('Tool Calls' in title and 'msg_id: msg_assistant_medium' in title and '4.2 tps' in title for title in titles)
        assert any('bash' in title and 'msg_id: msg_tool_medium' in title and 'tool_call_id: call_full_1234567890' in title for title in titles)
        joined_bodies = "\n".join(bodies)
        assert 'Answer body' in joined_bodies
        assert 'Private reasoning body' not in joined_bodies
        assert 'Completed tool result body' not in joined_bodies
        assert 'call_full_1234567890' in joined_bodies
        assert 'echo hello' in joined_bodies

    def test_display_verbosity_min_prints_conversation_and_hidden_summary(self, egg_app, monkeypatch):
        """Min static view should summarize hidden details between visible messages."""
        from eggthreads import append_message, create_snapshot

        egg_app._display_verbosity = "min"
        append_message(egg_app.db, egg_app.current_thread, "system", "ordinary system prompt")
        user1 = append_message(egg_app.db, egg_app.current_thread, "user", "first question")
        assistant = append_message(
            egg_app.db,
            egg_app.current_thread,
            "assistant",
            "assistant answer",
            extra={
                "reasoning": "private reasoning body",
                "tool_calls": [{
                    "id": "call_full_1234567890",
                    "function": {"name": "bash", "arguments": {"cmd": "echo hello"}},
                }],
            },
        )
        tool = append_message(
            egg_app.db,
            egg_app.current_thread,
            "tool",
            "completed tool result body",
            extra={"name": "bash", "tool_call_id": "call_full_1234567890"},
        )
        user2 = append_message(egg_app.db, egg_app.current_thread, "user", "next question")
        create_snapshot(egg_app.db, egg_app.current_thread)

        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_static_view_current()

        titles = self._collect_panel_titles(printed)
        bodies = self._collect_panel_bodies(printed)
        joined_bodies = "\n".join(bodies)

        assert any('User' in title and f'msg_id: {user1}' in title for title in titles)
        assert any('Assistant' in title and f'msg_id: {assistant}' in title for title in titles)
        assert any('User' in title and f'msg_id: {user2}' in title for title in titles)
        assert not any('Hidden Details' in title for title in titles)
        assert 'first question' in joined_bodies
        assert 'assistant answer' in joined_bodies
        assert 'next question' in joined_bodies
        assert 'ordinary system prompt' in joined_bodies
        assert 'private reasoning body' not in joined_bodies
        assert 'completed tool result body' not in joined_bodies
        assert 'Hidden details:' not in joined_bodies
        assert '1 reasoning block' in joined_bodies
        assert 'Executed 1 tool, got 1 tool result' in joined_bodies
        assert 'total tokens' in joined_bodies
        assert 'Tools: bash' in joined_bodies
        assert f'msg_id: {tool}' not in joined_bodies
        assert 'tool_call_id: call_full_1234567890' not in joined_bodies

    def test_display_verbosity_min_merges_consecutive_hidden_static_panels(self, egg_app, monkeypatch):
        """Min static panels should merge a hidden run into one summary panel."""
        from eggthreads import append_message, create_snapshot

        egg_app._display_verbosity = "min"
        append_message(egg_app.db, egg_app.current_thread, "user", "run both tools")
        append_message(
            egg_app.db,
            egg_app.current_thread,
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
            egg_app.db,
            egg_app.current_thread,
            "tool",
            "bash result body",
            extra={"name": "bash", "tool_call_id": "call_bash_1234567890"},
        )
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "tool",
            "python result body",
            extra={"name": "python_repl", "tool_call_id": "call_python_1234567890"},
        )
        append_message(egg_app.db, egg_app.current_thread, "assistant", "done")
        create_snapshot(egg_app.db, egg_app.current_thread)

        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_static_view_current()

        titles = self._collect_panel_titles(printed)
        joined_bodies = "\n".join(self._collect_panel_bodies(printed))

        assert not any('Hidden Details' in title for title in titles)
        assert 'Hidden details:' not in joined_bodies
        assert 'Executed 2 tools, got 2 tool results, 1 reasoning block' in joined_bodies
        assert joined_bodies.count('Executed 2 tools, got 2 tool results, 1 reasoning block') == 1
        assert 'Tools: bash, python_repl' in joined_bodies
        assert 'private reasoning body' not in joined_bodies
        assert 'bash result body' not in joined_bodies
        assert 'python result body' not in joined_bodies
        assert joined_bodies.index('Executed 2 tools, got 2 tool results, 1 reasoning block') < joined_bodies.index('done')

    def test_uses_markdown_for_markdown_content(self, egg_app, monkeypatch):
        """Should use Markdown rendering for markdown content."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_message({
            'role': 'assistant',
            'content': '```python\nprint("hello")\n```\nMore text'
        })

        assert len(printed) >= 1


class TestPrintStaticViewCurrent:
    """Tests for print_static_view_current()."""

    def test_prints_heading_if_provided(self, egg_app, monkeypatch):
        """Should print heading if provided."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_static_view_current(heading="Test Heading")

        # Should have printed something (heading)
        assert len(printed) >= 1

    def test_prints_no_messages_for_empty_thread(self, egg_app, monkeypatch):
        """Should print 'No messages yet' for empty thread."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_static_view_current()

        # Should have printed no messages panel
        assert len(printed) >= 1

    def test_creates_snapshot_if_needed(self, egg_app, monkeypatch):
        """Should create snapshot if not streaming."""
        created = []
        def mock_create(db, tid):
            created.append(tid)
        monkeypatch.setattr("egg.panels.create_snapshot", mock_create)
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: None)
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: None)

        egg_app.print_static_view_current()

        assert egg_app.current_thread in created


    def test_prints_compaction_marker_before_start_message(self, egg_app, monkeypatch):
        """Static transcript prints a red compaction divider and full history."""
        from eggthreads import append_message, commit_thread_compaction, create_snapshot

        old = append_message(egg_app.db, egg_app.current_thread, "user", "old visible history")
        start = append_message(egg_app.db, egg_app.current_thread, "assistant", "compact summary")
        after = append_message(egg_app.db, egg_app.current_thread, "user", "new question")
        commit_thread_compaction(egg_app.db, egg_app.current_thread, start, created_by="test")
        create_snapshot(egg_app.db, egg_app.current_thread)

        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_static_view_current()

        titles = [str(getattr(arg, "title", "")) for args, _kw in printed for arg in args]
        renderables = [getattr(arg, "renderable", None) for args, _kw in printed for arg in args]
        body = "\n".join(str(getattr(renderable, "plain", renderable or "")) for renderable in renderables)
        assert any("Compaction Boundary" in title for title in titles)
        assert "Compaction boundary: API context now starts at msg_" in body
        assert start[-8:] in body
        assert any("User" in title for title in titles)
        assert any("Assistant" in title for title in titles)
        assert old and after

    def test_static_message_titles_include_full_message_ids(self, egg_app, monkeypatch):
        """Static terminal panels show full msg_ids for copy/paste workflows."""
        from eggthreads import append_message, create_snapshot

        msg_id = append_message(egg_app.db, egg_app.current_thread, "user", "copy me")
        create_snapshot(egg_app.db, egg_app.current_thread)

        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_static_view_current()

        titles = [str(getattr(arg, "title", "")) for args, _kw in printed for arg in args]
        assert any(f"msg_id: {msg_id}" in title for title in titles)

    def test_full_screen_static_view_renders_full_history(self, egg_app, monkeypatch):
        from eggthreads import append_message, create_snapshot

        egg_app._display_is_inline = False
        for i in range(6):
            append_message(egg_app.db, egg_app.current_thread, "user", f"message {i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        rendered = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: rendered.append(m.get("content")))
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: None)

        egg_app.print_static_view_current()

        assert rendered[-6:] == [f"message {i}" for i in range(6)]

    def test_inline_static_view_keeps_full_history(self, egg_app, monkeypatch):
        from eggthreads import append_message, create_snapshot

        egg_app._display_is_inline = True
        for i in range(6):
            append_message(egg_app.db, egg_app.current_thread, "user", f"message {i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        rendered = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: rendered.append(m.get("content")))
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: None)

        egg_app.print_static_view_current()

        assert rendered[-6:] == [f"message {i}" for i in range(6)]


class TestFullScreenScrollbackWiring:
    """Tests for full-screen lazy transcript source wiring."""

    class Renderer:
        def __init__(self):
            self.sources = []
            self.clear_calls = 0
            self.invalidate_calls = 0
            self.update_calls = 0
            self.bottom_calls = 0

        def set_scrollback_source(self, source):
            self.sources.append(source)

        def clear_scrollback(self):
            self.clear_calls += 1

        def invalidate(self):
            self.invalidate_calls += 1

        def update(self, renderable):
            self.update_calls += 1

        def scroll_to_bottom(self):
            self.bottom_calls += 1

    class LocalRowsRenderer(Renderer):
        def __init__(self):
            super().__init__()
            self.printed = []

        def print_above(self, *args, **kwargs):
            self.printed.append((args, kwargs))

    def test_install_transcript_source_marks_history_without_printing_messages(self, egg_app, monkeypatch):
        from egg.panels import TranscriptScrollbackSource
        from eggthreads import append_message, create_snapshot

        for i in range(4):
            append_message(egg_app.db, egg_app.current_thread, "user", f"startup lazy {i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        renderer = self.Renderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False

        printed = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: printed.append(m))

        assert egg_app._install_transcript_scrollback_source(renderer) is True

        assert len(renderer.sources) == 1
        assert isinstance(renderer.sources[-1], TranscriptScrollbackSource)
        assert printed == []
        assert egg_app._last_printed_seq_by_thread[egg_app.current_thread] >= 0

    def test_redraw_full_screen_replaces_source_and_does_not_print_history(self, egg_app, monkeypatch):
        from eggthreads import append_message, create_snapshot

        for i in range(3):
            append_message(egg_app.db, egg_app.current_thread, "user", f"redraw lazy {i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        renderer = self.Renderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False

        printed = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: printed.append(m))

        egg_app.redraw_static_view(reason="manual")

        # Reset clears rows appended with print_above, then installs a fresh
        # source and repaints the live window without eager history printing.
        assert len(renderer.sources) == 1
        assert renderer.sources[0] is not None
        assert renderer.clear_calls == 1
        assert renderer.invalidate_calls >= 1
        assert renderer.update_calls == 1
        assert printed == []

    def test_redraw_inline_still_prints_full_static_history(self, egg_app, monkeypatch):
        from eggthreads import append_message, create_snapshot

        for i in range(3):
            append_message(egg_app.db, egg_app.current_thread, "user", f"inline redraw {i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        class InlineRenderer:
            def invalidate(self):
                pass

            def print_above(self, *args, **kwargs):
                pass

        renderer = InlineRenderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = True

        rendered = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: rendered.append(m.get("content")))
        monkeypatch.setattr(egg_app, "print_banner", lambda: None)

        egg_app.redraw_static_view(reason="manual")

        assert rendered[-3:] == [f"inline redraw {i}" for i in range(3)]

    def test_thread_switch_command_refreshes_source_without_printing_history(self, egg_app, monkeypatch):
        from eggthreads import append_message, create_root_thread, create_snapshot

        new_thread = create_root_thread(egg_app.db, name="target")
        append_message(egg_app.db, new_thread, "user", "thread switch lazy")
        create_snapshot(egg_app.db, new_thread)

        renderer = self.Renderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False
        egg_app.current_thread = new_thread

        printed = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: printed.append(m))

        egg_app.print_current_thread(heading=f"Switched to thread: {new_thread}")

        assert len(renderer.sources) == 1
        assert renderer.sources[0] is not None
        assert renderer.clear_calls == 1
        assert renderer.update_calls == 1
        assert printed == []

    def test_display_verbosity_redraw_replaces_source(self, egg_app, monkeypatch):
        renderer = self.Renderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False

        printed = []
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: printed.append(m))

        egg_app.handle_command("/displayVerbosity medium")

        assert egg_app._display_verbosity == "medium"
        assert len(renderer.sources) == 1
        assert renderer.sources[0] is not None
        assert renderer.clear_calls == 1
        assert renderer.update_calls == 1
        assert printed == []

    def test_full_screen_min_updates_consecutive_hidden_summary_in_place(self, egg_app):
        """Full-screen min hidden-only messages should update one local summary item."""

        class Renderer:
            def __init__(self):
                self.sources = []
                self.replacements = []
                self.printed = []
                self.local_items = []

            def set_scrollback_source(self, source):
                self.sources.append(source)

            def replace_recent_scrollback(self, row_count, *args, **kwargs):
                self.replacements.append((row_count, args, kwargs))
                if row_count:
                    del self.local_items[-row_count:]
                self.local_items.extend(args)
                return len(args)

            def print_above(self, *args, **kwargs):
                self.printed.append((args, kwargs))
                self.local_items.extend(args)

        renderer = Renderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False
        egg_app._display_verbosity = "min"

        egg_app.console_print_message({
            "role": "assistant",
            "content": "",
            "reasoning": "private reasoning",
            "tool_calls": [{
                "id": "call_bash_1",
                "function": {"name": "bash", "arguments": {"cmd": "echo 1"}},
            }],
        })
        egg_app.console_print_message({
            "role": "tool",
            "name": "bash",
            "content": "result one",
            "tool_call_id": "call_bash_1",
        })
        egg_app.console_print_message({
            "role": "assistant",
            "content": "",
            "reasoning": "more reasoning",
            "tool_calls": [{
                "id": "call_python_1",
                "function": {"name": "python_repl", "arguments": {"code": "print(1)"}},
            }],
        })
        egg_app.console_print_message({
            "role": "tool",
            "name": "python_repl",
            "content": "result two",
            "tool_call_id": "call_python_1",
        })

        assert [row_count for row_count, _args, _kwargs in renderer.replacements] == [0, 1, 1, 1]
        assert renderer.printed == []
        assert len(renderer.local_items) == 1
        bodies = []
        for _row_count, args, _kwargs in renderer.replacements:
            for arg in args:
                renderable = getattr(arg, "renderable", None)
                if renderable is not None:
                    bodies.append(str(getattr(renderable, "plain", renderable)))
        assert bodies[-1].count("Executed") == 1
        assert "Executed 2 tools, got 2 tool results, 2 reasoning blocks" in bodies[-1]
        assert "Tools: bash, python_repl" in bodies[-1]

    def test_full_screen_min_visible_message_finalizes_and_resets_summary_update(self, egg_app):
        """Visible messages bound a full-screen min summary run."""

        class Renderer:
            def __init__(self):
                self.sources = []
                self.replacements = []
                self.printed = []

            def set_scrollback_source(self, source):
                self.sources.append(source)

            def replace_recent_scrollback(self, row_count, *args, **kwargs):
                self.replacements.append((row_count, args, kwargs))
                return 1

            def print_above(self, *args, **kwargs):
                self.printed.append((args, kwargs))

        renderer = Renderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False
        egg_app._display_verbosity = "min"

        egg_app.console_print_message({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_bash_1",
                "function": {"name": "bash", "arguments": "{}"},
            }],
        })
        egg_app.console_print_message({"role": "tool", "name": "bash", "content": "ok"})
        egg_app.console_print_message({"role": "assistant", "content": "visible answer"})
        egg_app.console_print_message({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_python_1",
                "function": {"name": "python_repl", "arguments": "{}"},
            }],
        })

        assert [row_count for row_count, _args, _kwargs in renderer.replacements] == [0, 1, 1, 0]
        assert len(renderer.printed) == 1
        visible_arg = renderer.printed[0][0][0]
        assert "Assistant" in str(getattr(visible_arg, "title", ""))

    def test_in_session_rows_survive_until_source_replacement(self, egg_app):
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "existing source row")
        create_snapshot(egg_app.db, egg_app.current_thread)

        renderer = self.LocalRowsRenderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False

        assert egg_app._install_transcript_scrollback_source(renderer) is True
        assert renderer.clear_calls == 0

        egg_app.console_print_message({"role": "user", "content": "local until refresh"})

        assert renderer.clear_calls == 0
        assert len(renderer.printed) == 1
        assert len(renderer.sources) == 1

        egg_app.redraw_static_view(reason="manual")

        assert renderer.clear_calls == 1
        assert len(renderer.printed) == 1
        assert len(renderer.sources) == 2

    def test_full_screen_new_message_prints_locally_then_redraw_clears_local_rows(self, egg_app):
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "initial history")
        create_snapshot(egg_app.db, egg_app.current_thread)

        renderer = self.LocalRowsRenderer()
        egg_app._renderer = renderer
        egg_app._display_is_inline = False
        egg_app._install_transcript_scrollback_source(renderer)

        msg_id = append_message(egg_app.db, egg_app.current_thread, "user", "new local row")
        create_snapshot(egg_app.db, egg_app.current_thread)
        row = egg_app.db.conn.execute(
            "SELECT event_seq, msg_id, ts, payload_json FROM events WHERE msg_id=?",
            (msg_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload.setdefault("msg_id", row["msg_id"])
        payload.setdefault("event_seq", int(row["event_seq"]))
        if row["ts"] is not None:
            payload.setdefault("ts", row["ts"])

        egg_app.console_print_message(payload)
        egg_app._last_printed_seq_by_thread[egg_app.current_thread] = int(row["event_seq"])

        assert renderer.clear_calls == 0
        assert len(renderer.printed) == 1
        assert len(renderer.sources) == 1

        egg_app.redraw_static_view(reason="manual")

        assert renderer.clear_calls == 1
        assert len(renderer.printed) == 1
        assert len(renderer.sources) == 2
        assert renderer.update_calls == 1

    def test_full_screen_run_installs_source_before_initial_paint_without_history_printing(self, egg_app, monkeypatch):
        from egg.panels import TranscriptScrollbackSource
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "run lazy history")
        create_snapshot(egg_app.db, egg_app.current_thread)

        events = []

        class RunRenderer(self.Renderer):
            def __enter__(self):
                events.append("enter")
                assert self.sources
                assert isinstance(self.sources[-1], TranscriptScrollbackSource)
                egg_app.running = False
                return self

            def __exit__(self, *exc):
                events.append("exit")

        async def no_watch():
            return None

        monkeypatch.setattr(egg_app, "start_watching_current", no_watch)
        monkeypatch.setattr(egg_app.input_panel.editor, "_input_worker", lambda: None)
        monkeypatch.setattr("threading.Thread", lambda *a, **kw: type("Thread", (), {"start": lambda self: None})())
        monkeypatch.setattr("egg.app.DiffRenderer", lambda *a, **kw: RunRenderer())
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: events.append("message"))
        egg_app._display_is_inline = False

        asyncio.run(egg_app.run())

        assert events == ["enter", "exit"]

    def test_mode_switch_to_full_installs_source_without_eager_reprint(self, egg_app, monkeypatch):
        from egg.panels import TranscriptScrollbackSource
        from eggthreads import append_message, create_snapshot

        append_message(egg_app.db, egg_app.current_thread, "user", "mode lazy history")
        create_snapshot(egg_app.db, egg_app.current_thread)

        events = []

        class RunRenderer(self.Renderer):
            def __enter__(self):
                events.append("enter")
                assert self.sources
                assert isinstance(self.sources[-1], TranscriptScrollbackSource)
                egg_app.running = False
                return self

            def __exit__(self, *exc):
                events.append("exit")

        async def no_watch():
            return None

        monkeypatch.setattr(egg_app, "start_watching_current", no_watch)
        monkeypatch.setattr(egg_app.input_panel.editor, "_input_worker", lambda: None)
        monkeypatch.setattr("threading.Thread", lambda *a, **kw: type("Thread", (), {"start": lambda self: None})())
        monkeypatch.setattr("egg.app.DiffRenderer", lambda *a, **kw: RunRenderer())
        monkeypatch.setattr(egg_app, "console_print_message", lambda m: events.append("message"))
        egg_app._display_is_inline = False
        egg_app._pending_mode_change = True

        asyncio.run(egg_app.run())

        assert events == ["enter", "exit"]


class TestTranscriptScrollbackSource:
    """Tests for the lazy full-screen transcript scrollback source."""

    @staticmethod
    def _fallback_rows(item, _width):
        return [str(item.fallback)]

    def test_bottom_window_renders_only_enough_tail_blocks(self, egg_app, monkeypatch):
        """A bottom viewport request should not render the full transcript."""
        from egg.panels import TranscriptScrollbackSource, _StaticTranscriptRenderable
        from eggthreads import append_message, create_snapshot

        for i in range(10):
            append_message(egg_app.db, egg_app.current_thread, "user", f"tail-lazy-{i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        rendered = []

        def fake_message_renderables(message, hidden_details=None):
            content = str(message.get("content") or "")
            rendered.append(content)
            return [_StaticTranscriptRenderable(content, content)]

        monkeypatch.setattr(egg_app, "_static_transcript_message_renderables", fake_message_renderables)

        source = TranscriptScrollbackSource(egg_app, refresh_snapshot=False)
        monkeypatch.setattr(source, "_render_static_transcript_item_rows", self._fallback_rows)

        rows = list(source.rows_from_bottom(80, bottom_offset=0, height=3))

        assert rows == ["tail-lazy-7", "tail-lazy-8", "tail-lazy-9"]
        assert rendered == ["tail-lazy-9", "tail-lazy-8", "tail-lazy-7"]
        assert "tail-lazy-0" not in rendered
        assert source.row_count(80) is None

    def test_rows_are_cached_by_width_and_verbosity(self, egg_app, monkeypatch):
        """Rendered rows are reused for the same width/verbosity cache key."""
        from egg.panels import TranscriptScrollbackSource, _StaticTranscriptRenderable
        from eggthreads import append_message, create_snapshot

        for i in range(4):
            append_message(egg_app.db, egg_app.current_thread, "user", f"cache-lazy-{i}")
        create_snapshot(egg_app.db, egg_app.current_thread)

        rendered = []

        def fake_message_renderables(message, hidden_details=None):
            content = str(message.get("content") or "")
            rendered.append((egg_app._display_verbosity, content))
            return [_StaticTranscriptRenderable(content, content)]

        monkeypatch.setattr(egg_app, "_static_transcript_message_renderables", fake_message_renderables)

        source = TranscriptScrollbackSource(egg_app, refresh_snapshot=False)
        monkeypatch.setattr(source, "_render_static_transcript_item_rows", self._fallback_rows)

        assert list(source.rows_from_bottom(80, 0, 2)) == ["cache-lazy-2", "cache-lazy-3"]
        assert rendered == [("max", "cache-lazy-3"), ("max", "cache-lazy-2")]

        assert list(source.rows_from_bottom(80, 0, 2)) == ["cache-lazy-2", "cache-lazy-3"]
        assert rendered == [("max", "cache-lazy-3"), ("max", "cache-lazy-2")]

        assert list(source.rows_from_bottom(81, 0, 2)) == ["cache-lazy-2", "cache-lazy-3"]
        assert rendered[-2:] == [("max", "cache-lazy-3"), ("max", "cache-lazy-2")]

        egg_app._display_verbosity = "medium"
        assert list(source.rows_from_bottom(80, 0, 2)) == ["cache-lazy-2", "cache-lazy-3"]
        assert rendered[-2:] == [("medium", "cache-lazy-3"), ("medium", "cache-lazy-2")]

    def test_min_hidden_activity_block_uses_run_summary(self, egg_app, monkeypatch):
        """Lazy min render blocks should aggregate consecutive hidden activity."""
        from egg.panels import TranscriptScrollbackSource
        from eggthreads import append_message, create_snapshot

        egg_app._display_verbosity = "min"
        append_message(egg_app.db, egg_app.current_thread, "user", "before hidden run")
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "assistant",
            "",
            extra={
                "reasoning": "private reasoning body",
                "tool_calls": [{
                    "id": "call_lazy_bash",
                    "function": {"name": "bash", "arguments": {"cmd": "echo lazy"}},
                }],
            },
        )
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "tool",
            "lazy tool result body",
            extra={"name": "bash", "tool_call_id": "call_lazy_bash"},
        )
        append_message(egg_app.db, egg_app.current_thread, "assistant", "after hidden run")
        create_snapshot(egg_app.db, egg_app.current_thread)

        source = TranscriptScrollbackSource(egg_app, refresh_snapshot=False)
        monkeypatch.setattr(source, "_render_static_transcript_item_rows", self._fallback_rows)

        rows = list(source.rows_from_bottom(100, bottom_offset=0, height=20))
        text = "\n".join(rows)

        # Visible messages are present
        assert "before hidden run" in text
        assert "after hidden run" in text

        # Hidden activity is aggregated into one summary
        assert "Executed 1 tool" in text
        assert "got 1 tool result" in text
        assert "1 reasoning block" in text
        assert "Tools: bash" in text

        # No legacy Hidden Details panels
        assert "Hidden Details" not in text
        assert "Hidden details:" not in text

        # Hidden raw content is not leaked
        assert "private reasoning body" not in text
        assert "lazy tool result body" not in text

        # Aggregation: only one summary row (not one per hidden block)
        summary_rows = [r for r in rows if "Executed" in r or "got" in r]
        assert len(summary_rows) == 1, f"Expected 1 aggregated summary row, got {len(summary_rows)}: {summary_rows}"

    def test_min_consecutive_hidden_blocks_aggregate_across_many(self, egg_app, monkeypatch):
        """Several consecutive hidden blocks should produce a single summary."""
        from egg.panels import TranscriptScrollbackSource
        from eggthreads import append_message, create_snapshot

        egg_app._display_verbosity = "min"
        # Two hidden assistant tool-call messages followed by two tool results,
        # with no visible message in between.
        append_message(egg_app.db, egg_app.current_thread, "user", "start")
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {"id": "t1", "function": {"name": "bash", "arguments": {"cmd": "echo 1"}}},
                ],
            },
        )
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {"id": "t2", "function": {"name": "python_repl", "arguments": {"code": "2+2"}}},
                ],
            },
        )
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "tool",
            "result 1",
            extra={"name": "bash", "tool_call_id": "t1"},
        )
        append_message(
            egg_app.db,
            egg_app.current_thread,
            "tool",
            "result 2",
            extra={"name": "python_repl", "tool_call_id": "t2"},
        )
        append_message(egg_app.db, egg_app.current_thread, "assistant", "done")
        create_snapshot(egg_app.db, egg_app.current_thread)

        source = TranscriptScrollbackSource(egg_app, refresh_snapshot=False)
        monkeypatch.setattr(source, "_render_static_transcript_item_rows", self._fallback_rows)

        rows = list(source.rows_from_bottom(100, bottom_offset=0, height=20))
        text = "\n".join(rows)

        assert "start" in text
        assert "done" in text
        assert "Executed 2 tools" in text
        assert "got 2 tool results" in text
        assert "bash" in text
        assert "python_repl" in text
        assert "Hidden Details" not in text

        # Only one summary row
        summary_rows = [r for r in rows if "Executed" in r or "got" in r]
        assert len(summary_rows) == 1, f"Expected 1 aggregated summary row, got {len(summary_rows)}: {summary_rows}"


class TestRedrawStaticView:
    """Tests for redraw_static_view()."""

    def test_clears_console(self, egg_app, monkeypatch):
        """Should clear the console."""
        cleared = []
        monkeypatch.setattr(egg_app.console, "clear", lambda: cleared.append(True))
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: None)

        egg_app.redraw_static_view()

        assert len(cleared) >= 1

    def test_prints_banner(self, egg_app, monkeypatch):
        """Should print the banner."""
        banner_printed = []
        original_print_banner = egg_app.print_banner
        def mock_banner():
            banner_printed.append(True)
            original_print_banner()
        monkeypatch.setattr(egg_app, "print_banner", mock_banner)
        monkeypatch.setattr(egg_app.console, "clear", lambda: None)
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: None)

        egg_app.redraw_static_view()

        assert len(banner_printed) >= 1

    def test_includes_reason_in_heading(self, egg_app, monkeypatch):
        """Should include reason in heading if provided."""
        printed = []
        monkeypatch.setattr(egg_app.console, "clear", lambda: None)
        def mock_print(*a, **kw):
            printed.append(str(a))
        monkeypatch.setattr(egg_app.console, "print", mock_print)

        egg_app.redraw_static_view(reason="manual")

        # Should have printed something (banner and heading)
        assert len(printed) >= 1
        # The reason appears in the heading which is printed via Panel
        # Check that at least some output was generated
        assert any("Egg" in p or "manual" in p or "Redraw" in p for p in printed)


class TestConsolePrintBlock:
    """Tests for console_print_block()."""

    def test_prints_titled_panel(self, egg_app, monkeypatch):
        """Should print a titled panel."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_block("Test Title", "Test content")

        assert len(printed) == 1

    def test_uses_custom_border_style(self, egg_app, monkeypatch):
        """Should use custom border style if provided."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.console_print_block("Title", "Content", border_style='red')

        assert len(printed) == 1

    def test_handles_print_failure_gracefully(self, egg_app, monkeypatch):
        """Should handle Panel print failure gracefully."""
        call_count = [0]
        def mock_print(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Print failed")
        monkeypatch.setattr(egg_app.console, "print", mock_print)

        # Should not raise
        egg_app.console_print_block("Title", "Content")


class TestPrintBanner:
    """Tests for print_banner()."""

    def test_prints_banner_text(self, egg_app, monkeypatch):
        """Should print the banner text."""
        printed = []
        monkeypatch.setattr(egg_app.console, "print", lambda *a, **kw: printed.append((a, kw)))

        egg_app.print_banner()

        assert len(printed) >= 1
        # Should contain "Egg Chat" or similar
        assert any("Egg" in str(p) for p in printed)

    def test_handles_print_failure(self, egg_app, monkeypatch):
        """Should handle print failure gracefully."""
        def mock_print(*a, **kw):
            raise Exception("Print failed")
        monkeypatch.setattr(egg_app.console, "print", mock_print)

        # Should not raise
        egg_app.print_banner()
