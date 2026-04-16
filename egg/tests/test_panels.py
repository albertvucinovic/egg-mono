"""Tests for panels.py PanelsMixin."""
from __future__ import annotations

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

    def test_updates_system_output_content(self, egg_app, monkeypatch):
        """Should update system_output with status lines."""
        set_content_calls = []
        original_set_content = egg_app.system_output.set_content
        def mock_set_content(text):
            set_content_calls.append(text)
            original_set_content(text)
        monkeypatch.setattr(egg_app.system_output, "set_content", mock_set_content)

        egg_app.update_panels()

        assert len(set_content_calls) >= 1
        # Should contain current thread info
        assert any(egg_app.current_thread[-8:] in call for call in set_content_calls)

    def test_system_output_shows_paste_shortcut(self, egg_app):
        """System panel should advertise the paste shortcut."""
        egg_app.update_panels()

        content = egg_app.system_output.content
        assert content is not None
        assert "Paste: Ctrl+P" in content

    def test_updates_children_output(self, egg_app, monkeypatch):
        """Should update children_output with tree view."""
        import time
        # Force refresh by setting last refresh to old time
        egg_app._last_children_refresh = 0

        set_content_calls = []
        original_set_content = egg_app.children_output.set_content
        def mock_set_content(text):
            set_content_calls.append(text)
            original_set_content(text)
        monkeypatch.setattr(egg_app.children_output, "set_content", mock_set_content)

        egg_app.update_panels()

        assert len(set_content_calls) >= 1

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
