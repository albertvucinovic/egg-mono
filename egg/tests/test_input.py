"""Tests for input.py InputMixin handle_key functionality."""
from __future__ import annotations

import pytest


class TestHandleKeyCtrlD:
    """Tests for Ctrl+D key handling."""

    def test_ctrl_d_sends_message_when_text_present(self, egg_app, monkeypatch):
        """Ctrl+D should submit text when input is not empty."""
        # Set up input text
        egg_app.input_panel.editor.editor.set_text("Hello world")

        # Track if on_submit was called
        submitted = []
        original_on_submit = egg_app.on_submit
        def mock_on_submit(text):
            submitted.append(text)
            return True
        monkeypatch.setattr(egg_app, "on_submit", mock_on_submit)

        # Also need to mock handle_pending_approval_answer to return False
        monkeypatch.setattr(egg_app, "handle_pending_approval_answer", lambda t, source: False)

        result = egg_app.handle_key('\x04')  # Ctrl+D

        assert result is True
        assert len(submitted) == 1
        assert submitted[0] == "Hello world"

    def test_ctrl_d_clears_input_after_send(self, egg_app, monkeypatch):
        """Ctrl+D should clear input panel after sending."""
        egg_app.input_panel.editor.editor.set_text("Test message")

        monkeypatch.setattr(egg_app, "on_submit", lambda t: True)
        monkeypatch.setattr(egg_app, "handle_pending_approval_answer", lambda t, source: False)

        egg_app.handle_key('\x04')

        assert egg_app.input_panel.get_text() == ""

    def test_ctrl_d_with_pending_approval_handles_approval_first(self, egg_app, monkeypatch):
        """Ctrl+D should process approval answer before normal send."""
        egg_app.input_panel.editor.editor.set_text("y")

        # Mock approval handler to return True (handled)
        approval_called = []
        def mock_approval(text, source):
            approval_called.append((text, source))
            return True
        monkeypatch.setattr(egg_app, "handle_pending_approval_answer", mock_approval)

        result = egg_app.handle_key('\x04')

        assert result is True
        assert len(approval_called) == 1
        assert approval_called[0] == ("y", "Ctrl+D")

    def test_ctrl_d_on_empty_input_returns_true(self, egg_app, monkeypatch):
        """Ctrl+D on empty input should still return True."""
        egg_app.input_panel.editor.editor.set_text("")
        monkeypatch.setattr(egg_app, "handle_pending_approval_answer", lambda t, source: False)

        result = egg_app.handle_key('\x04')

        assert result is True


class TestHandleKeyCtrlC:
    """Tests for Ctrl+C key handling."""

    def test_ctrl_c_clears_non_empty_input_first(self, egg_app, monkeypatch):
        """Ctrl+C with text in input should clear input, not quit."""
        egg_app.input_panel.editor.editor.set_text("Some text")
        egg_app.running = True

        # Mock thread state as idle
        monkeypatch.setattr("eggthreads.thread_state", lambda db, tid: "idle")

        # Mock db.current_open to return None (no active stream)
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: None)

        result = egg_app.handle_key('\x03')  # Ctrl+C

        assert result is True
        assert egg_app.running is True
        assert egg_app.input_panel.get_text() == ""

    def test_ctrl_c_quits_on_idle_empty_input(self, egg_app, monkeypatch):
        """Ctrl+C with idle thread and empty input should quit."""
        egg_app.input_panel.editor.editor.set_text("")
        egg_app.running = True

        # Mock thread state as idle
        monkeypatch.setattr("eggthreads.thread_state", lambda db, tid: "idle")

        # Mock db.current_open to return None (no active stream)
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: None)

        result = egg_app.handle_key('\x03')

        assert result is False
        assert egg_app.running is False

    def test_ctrl_c_interrupts_active_stream(self, egg_app, monkeypatch):
        """Ctrl+C during streaming should interrupt and not quit."""
        egg_app.input_panel.editor.editor.set_text("")
        egg_app.running = True

        # Mock active stream
        class MockStreamRow:
            invoke_id = "test_invoke"
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: MockStreamRow())

        # Mock interrupt_thread in the input module where it's imported
        interrupted = []
        def mock_interrupt(db, tid):
            interrupted.append(tid)
        monkeypatch.setattr("egg.input.interrupt_thread", mock_interrupt)

        # Mock cancel_pending_tools_on_interrupt
        monkeypatch.setattr(egg_app, "cancel_pending_tools_on_interrupt", lambda: None)
        monkeypatch.setattr(egg_app, "compute_pending_prompt", lambda: None)

        result = egg_app.handle_key('\x03')

        assert result is True
        assert egg_app.running is True
        assert len(interrupted) == 1

    def test_ctrl_c_resets_live_state(self, egg_app, monkeypatch):
        """Ctrl+C should reset live streaming state."""
        egg_app._live_state = {
            "active_invoke": "some_invoke",
            "content": "partial content",
            "reason": "partial reason",
            "tools": {"tool1": "text"},
            "tc_text": {},
            "tc_order": [],
        }

        # Mock active stream
        class MockStreamRow:
            invoke_id = "test_invoke"
        monkeypatch.setattr(egg_app.db, "current_open", lambda tid: MockStreamRow())
        monkeypatch.setattr("eggthreads.interrupt_thread", lambda db, tid: None)
        monkeypatch.setattr(egg_app, "cancel_pending_tools_on_interrupt", lambda: None)
        monkeypatch.setattr(egg_app, "compute_pending_prompt", lambda: None)

        egg_app.handle_key('\x03')

        assert egg_app._live_state["active_invoke"] is None
        assert egg_app._live_state["content"] == ""


class TestHandleKeyCtrlE:
    """Tests for Ctrl+E key handling."""

    def test_ctrl_e_clears_input(self, egg_app):
        """Ctrl+E should clear the input panel."""
        egg_app.input_panel.editor.editor.set_text("Some text to clear")

        result = egg_app.handle_key('\x05')  # Ctrl+E

        assert result is True
        assert egg_app.input_panel.get_text() == ""


class TestHandleKeyCtrlP:
    """Tests for Ctrl+P key handling."""

    def test_ctrl_p_pastes_clipboard_content(self, egg_app, monkeypatch):
        """Ctrl+P should paste from clipboard."""
        monkeypatch.setattr("egg.input.read_clipboard", lambda: "clipboard content")

        result = egg_app.handle_key('\x10')  # Ctrl+P

        assert result is True
        assert egg_app.input_panel.get_text() == "clipboard content"

    def test_ctrl_p_logs_error_on_clipboard_failure(self, egg_app, monkeypatch):
        """Ctrl+P should log error when clipboard read fails."""
        monkeypatch.setattr("egg.input.read_clipboard", lambda: None)

        result = egg_app.handle_key('\x10')

        assert result is True
        # Check system log for error message - actual message is "Failed to read clipboard."
        assert any("Failed to read clipboard" in msg for msg in egg_app._system_log)

    def test_ctrl_p_logs_message_on_empty_clipboard(self, egg_app, monkeypatch):
        """Ctrl+P should log message when clipboard is empty."""
        monkeypatch.setattr("egg.input.read_clipboard", lambda: "")

        result = egg_app.handle_key('\x10')

        assert result is True
        # Actual message is "Clipboard is empty."
        assert any("Clipboard is empty" in msg for msg in egg_app._system_log)


class TestHandleKeyEnter:
    """Tests for Enter key handling."""

    def test_enter_sends_in_send_mode(self, egg_app, monkeypatch):
        """Enter should send when enter_sends is True."""
        egg_app.enter_sends = True
        egg_app.input_panel.editor.editor.set_text("Message to send")

        submitted = []
        monkeypatch.setattr(egg_app, "on_submit", lambda t: (submitted.append(t), True)[1])
        monkeypatch.setattr(egg_app, "handle_pending_approval_answer", lambda t, source: False)

        result = egg_app.handle_key('\r')

        assert result is True
        assert len(submitted) == 1
        assert submitted[0] == "Message to send"

    def test_enter_inserts_newline_in_newline_mode(self, egg_app, monkeypatch):
        """Enter should insert newline when enter_sends is False."""
        egg_app.enter_sends = False
        egg_app.input_panel.editor.editor.set_text("Line 1")

        # Track if insert_newline was called (actual implementation calls this)
        newline_inserted = []
        original_insert = egg_app.input_panel.editor.editor.insert_newline
        def mock_insert():
            newline_inserted.append(True)
            return original_insert()
        monkeypatch.setattr(egg_app.input_panel.editor.editor, "insert_newline", mock_insert)

        result = egg_app.handle_key('\r')

        assert result is True
        # Check that insert_newline was called
        assert len(newline_inserted) == 1

    def test_enter_handles_pending_approval_in_send_mode(self, egg_app, monkeypatch):
        """Enter should process approval when pending and enter_sends."""
        egg_app.enter_sends = True
        egg_app.input_panel.editor.editor.set_text("y")

        approval_called = []
        def mock_approval(text, source):
            approval_called.append((text, source))
            return True
        monkeypatch.setattr(egg_app, "handle_pending_approval_answer", mock_approval)

        result = egg_app.handle_key('\r')

        assert result is True
        assert len(approval_called) == 1
        assert approval_called[0] == ("y", "Enter")

    def test_alt_enter_inserts_newline_even_in_send_mode(self, egg_app, monkeypatch):
        """Alt+Enter should insert newline without submitting."""
        egg_app.enter_sends = True
        egg_app.input_panel.editor.editor.set_text("Line 1")

        submitted = []
        monkeypatch.setattr(egg_app, "on_submit", lambda t: (submitted.append(t), True)[1])

        newline_inserted = []
        original_insert = egg_app.input_panel.editor.editor.insert_newline

        def mock_insert():
            newline_inserted.append(True)
            return original_insert()

        monkeypatch.setattr(egg_app.input_panel.editor.editor, "insert_newline", mock_insert)

        result = egg_app.handle_key('alt-enter')

        assert result is True
        assert newline_inserted == [True]
        assert submitted == []

    def test_shift_enter_inserts_newline_even_in_send_mode(self, egg_app, monkeypatch):
        """Shift+Enter should insert newline without submitting."""
        egg_app.enter_sends = True
        egg_app.input_panel.editor.editor.set_text("Line 1")

        submitted = []
        monkeypatch.setattr(egg_app, "on_submit", lambda t: (submitted.append(t), True)[1])

        newline_inserted = []
        original_insert = egg_app.input_panel.editor.editor.insert_newline

        def mock_insert():
            newline_inserted.append(True)
            return original_insert()

        monkeypatch.setattr(egg_app.input_panel.editor.editor, "insert_newline", mock_insert)

        result = egg_app.handle_key('shift-enter')

        assert result is True
        assert newline_inserted == [True]
        assert submitted == []


class TestHandleKeyEsc:
    """Tests for Escape key handling."""

    def test_esc_clears_completion_popup(self, egg_app, monkeypatch):
        """Esc should clear active completion state.

        Bare ESC is deferred by the debounce (so split escape sequences
        like SGR mouse reports can re-attach), so the test advances
        _pending_esc_time past the debounce window and explicitly
        invokes the stale-flush that the main loop would normally run.
        """
        egg_app.input_panel.editor.editor._completion_active = True
        egg_app.input_panel.editor.editor._completion_items = ["item1", "item2"]
        egg_app.input_panel.editor.editor._completion_index = 1

        result = egg_app.handle_key('\x1b')
        assert result is True

        # Simulate the debounce window elapsing then a main-loop flush.
        egg_app._pending_esc_time -= 1.0
        egg_app.flush_pending_esc_if_stale()

        assert egg_app.input_panel.editor.editor._completion_active is False
        assert egg_app.input_panel.editor.editor._completion_items == []
        assert egg_app.input_panel.editor.editor._completion_index == 0

    def test_double_esc_also_clears(self, egg_app):
        """Double Esc sequence should also clear completion."""
        egg_app.input_panel.editor.editor._completion_active = True

        result = egg_app.handle_key('\x1b\x1b')  # Double Esc

        assert result is True
        assert egg_app.input_panel.editor.editor._completion_active is False


class TestHandleKeyDelegation:
    """Tests for key delegation to editor."""

    def test_unknown_key_delegates_to_editor(self, egg_app, monkeypatch):
        """Unknown keys should be delegated to the editor."""
        delegated = []
        def mock_handle_key(key):
            delegated.append(key)
            return True
        monkeypatch.setattr(egg_app.input_panel.editor, "_handle_key", mock_handle_key)

        result = egg_app.handle_key('a')  # Regular character

        assert result is True
        assert 'a' in delegated
