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


class TestHandleKeySafetyShortcuts:
    """Tests for mnemonic Ctrl+Alt safety toggles."""

    @pytest.mark.parametrize(
        ("key", "command"),
        [
            ("\x1b\x01", "/toggleAutoApproval"),
            ("\x1b\x18", "/toggleSandboxing"),
        ],
    )
    def test_ctrl_alt_shortcut_runs_command_without_changing_draft(
        self, egg_app, monkeypatch, key, command
    ):
        egg_app.input_panel.editor.editor.set_text("draft stays here")
        commands = []
        monkeypatch.setattr(egg_app, "handle_command", commands.append)

        assert egg_app.handle_key(key) is True

        assert commands == [command]
        assert egg_app.input_panel.get_text() == "draft stays here"

    def test_similar_escape_sequence_is_not_a_toggle(self, egg_app, monkeypatch):
        commands = []
        monkeypatch.setattr(egg_app, "handle_command", commands.append)

        assert egg_app.handle_key("\x1bA") is True

        assert commands == []


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

    def test_ctrl_p_sanitizes_clipboard_terminal_controls(self, egg_app, monkeypatch):
        """Ctrl+P should not store terminal-control sequences from clipboard."""
        monkeypatch.setattr("egg.input.read_clipboard", lambda: "a\x1b[2Jb\r\x08c")

        result = egg_app.handle_key('\x10')

        assert result is True
        text = egg_app.input_panel.editor.editor.get_text()
        assert "\x1b" not in text
        assert "\r" not in text
        assert "\x08" not in text

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


class TestBracketedPasteThroughAppInput:
    """Regression tests for bracketed paste before app-level Enter handling."""

    def test_multiline_bracketed_paste_preserves_newlines_and_does_not_submit(self, egg_app, monkeypatch):
        submitted = []
        monkeypatch.setattr(egg_app, "on_submit", lambda t: submitted.append(t) or True)

        assert egg_app.handle_key("\x1b[200~") is True
        assert egg_app.handle_key("hello") is True
        assert egg_app.handle_key("\n") is True
        assert egg_app.handle_key("world") is True
        assert egg_app.handle_key("\x1b[201~") is True

        assert submitted == []
        assert egg_app.input_panel.editor.editor.get_text() == "hello\nworld"


class TestHandleKeyDelegation:
    """Tests for key delegation to editor."""

    def test_end_key_delegates_to_input_editor(self, egg_app):
        """End should move the message-input cursor to the end of the line."""
        editor = egg_app.input_panel.editor.editor
        editor.set_text("hello world")
        editor.cursor.row = 0
        editor.cursor.col = 2

        result = egg_app.handle_key('\x1b[F')

        assert result is True
        assert editor.cursor.col == len("hello world")

    def test_end_key_does_not_scroll_transcript(self, egg_app):
        """End belongs to the input editor even when a scrollback renderer exists."""

        class Renderer:
            def __init__(self):
                self.scrolled_to_bottom = False

            def scroll(self, _step):
                pass

            def scroll_to_bottom(self):
                self.scrolled_to_bottom = True

        renderer = Renderer()
        egg_app._renderer = renderer
        egg_app.input_panel.editor.editor.set_text("hello")
        egg_app.input_panel.editor.editor.cursor.col = 0

        result = egg_app.handle_key('\x1b[F')

        assert result is True
        assert renderer.scrolled_to_bottom is False
        assert egg_app.input_panel.editor.editor.cursor.col == len("hello")

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


class TestOrphanMouseClassifier:
    """Unit tests for the orphan SGR mouse fragment classifier.

    These cover the prefix-strip levels and partial shapes the absorber
    must recognise, exercised in isolation so they do not need a full
    app fixture.
    """

    def test_full_with_no_esc(self):
        from egg.input import _classify_mouse_fragment
        assert _classify_mouse_fragment('[<65;72;92M', allow_bare=False) == 'full'
        assert _classify_mouse_fragment('[<65;72;92m', allow_bare=False) == 'full'

    def test_full_with_no_esc_and_no_bracket(self):
        from egg.input import _classify_mouse_fragment
        assert _classify_mouse_fragment('<65;72;92M', allow_bare=False) == 'full'

    def test_full_naked_digits_only_in_window(self):
        from egg.input import _classify_mouse_fragment
        assert _classify_mouse_fragment('65;72;92M', allow_bare=True) == 'full'
        # Without the post-ESC suspicion window, naked digits must not
        # be claimed — they could be user typing.
        assert _classify_mouse_fragment('65;72;92M', allow_bare=False) == 'none'

    def test_partial_prefix_only(self):
        from egg.input import _classify_mouse_fragment
        assert _classify_mouse_fragment('[<', allow_bare=False) == 'partial'
        assert _classify_mouse_fragment('<', allow_bare=False) == 'partial'

    def test_partial_mid_body(self):
        from egg.input import _classify_mouse_fragment
        assert _classify_mouse_fragment('[<65', allow_bare=False) == 'partial'
        assert _classify_mouse_fragment('[<65;', allow_bare=False) == 'partial'
        assert _classify_mouse_fragment('[<65;72', allow_bare=False) == 'partial'
        assert _classify_mouse_fragment('<65;72', allow_bare=False) == 'partial'

    def test_intact_csi_introducer_left_to_normalize_key(self):
        # The absorber must NOT match ``\x1b[<…`` shapes — those are
        # handled by the editor's normalize_key path and would otherwise
        # double-buffer.
        from egg.input import _classify_mouse_fragment
        assert _classify_mouse_fragment('\x1b[<65;72;92M', allow_bare=False) == 'none'
        assert _classify_mouse_fragment('\x1b[<65', allow_bare=False) == 'none'

    def test_user_typing_passes_through(self):
        from egg.input import _classify_mouse_fragment
        for s in ('a', 'hello', '5', '5;', '5;3', '5;3;1', '5M', 'abc;defM'):
            assert _classify_mouse_fragment(s, allow_bare=False) == 'none', s
        # Even inside the window, only digit/semicolon shapes qualify.
        assert _classify_mouse_fragment('hello', allow_bare=True) == 'none'
        assert _classify_mouse_fragment('a;b;cM', allow_bare=True) == 'none'


class _OrphanMouseHost:
    """Minimal test host providing the InputMixin attributes needed to
    exercise ``_absorb_orphan_mouse`` / ``flush_pending_orphan_mouse_if_stale``
    without spinning up the whole app.
    """

    def __init__(self):
        self._pending_esc = False
        self._pending_esc_time = 0.0
        self._orphan_mouse_buf = ''
        self._orphan_mouse_until = 0.0
        self.flushed_to_editor = []

        class _EditorStub:
            def __init__(self, host):
                self._host = host
            def _handle_key(self, key):
                self._host.flushed_to_editor.append(key)
                return True

        class _PanelStub:
            def __init__(self, host):
                self.editor = _EditorStub(host)

        self.input_panel = _PanelStub(self)


def _bind_mixin(host):
    """Bind the InputMixin's orphan-mouse methods onto a host instance."""
    from egg.input import InputMixin
    for name in ('_absorb_orphan_mouse',
                 '_open_orphan_mouse_window',
                 'flush_pending_orphan_mouse_if_stale'):
        setattr(host.__class__, name, getattr(InputMixin, name))


class TestOrphanMouseAbsorber:
    """Tests for the multi-chunk orphan-mouse assembler."""

    def _make(self):
        host = _OrphanMouseHost()
        _bind_mixin(host)
        return host

    def test_complete_orphan_swallowed_returns_canonical(self):
        """``[<…M`` arriving as one chunk is reconstructed to
        ``\x1b[<…M`` so the wheel-scroll handler can interpret it; no
        bytes leak to the editor."""
        import time as _t
        host = self._make()
        out = host._absorb_orphan_mouse('[<65;72;92M', _t.monotonic())
        assert out == '\x1b[<65;72;92M'
        assert host.flushed_to_editor == []
        assert host._orphan_mouse_buf == ''

    def test_split_orphan_no_esc_no_bracket(self):
        """``<…`` partial then digits-tail completes to canonical form."""
        import time as _t
        host = self._make()
        # Open the window so naked-digit continuations are trusted.
        host._open_orphan_mouse_window(_t.monotonic())
        assert host._absorb_orphan_mouse('<65', _t.monotonic()) is None
        assert host._orphan_mouse_buf == '<65'
        out = host._absorb_orphan_mouse(';72;92M', _t.monotonic())
        assert out == '\x1b[<65;72;92M'
        assert host.flushed_to_editor == []

    def test_naked_digit_tail_in_window(self):
        """The shape the user reported leaking — ``65;72;92M`` with the
        whole ``\x1b[<`` introducer gone — is absorbed when the post-ESC
        suspicion window is open."""
        import time as _t
        host = self._make()
        host._open_orphan_mouse_window(_t.monotonic())
        out = host._absorb_orphan_mouse('65;72;92M', _t.monotonic())
        assert out == '\x1b[<65;72;92M'
        assert host.flushed_to_editor == []

    def test_naked_digit_tail_outside_window_passes_through(self):
        """Outside the suspicion window, naked digits are treated as
        user typing and reach the editor unchanged."""
        import time as _t
        host = self._make()
        # No window opened.
        out = host._absorb_orphan_mouse('65;72;92M', _t.monotonic())
        assert out == '65;72;92M'

    def test_multi_chunk_split_body(self):
        """Body fragments split across three readkey calls all reassemble."""
        import time as _t
        host = self._make()
        now = _t.monotonic()
        host._open_orphan_mouse_window(now)
        assert host._absorb_orphan_mouse('[<', now) is None
        assert host._absorb_orphan_mouse('65;72', now) is None
        out = host._absorb_orphan_mouse(';92M', now)
        assert out == '\x1b[<65;72;92M'
        assert host.flushed_to_editor == []

    def test_buffer_flushes_when_continuation_is_unrelated(self):
        """If a partial buffer is followed by something unrelated, the
        buffer is flushed as text + the new key proceeds normally."""
        import time as _t
        host = self._make()
        now = _t.monotonic()
        assert host._absorb_orphan_mouse('[<', now) is None
        out = host._absorb_orphan_mouse('hello', now)
        assert out == '[<hello'

    def test_stale_drop_silent_for_truncated_mouse_shape(self):
        """A buffer with a mouse-shaped prefix that times out without
        completing is silently dropped, not echoed as text."""
        import time as _t
        host = self._make()
        host._absorb_orphan_mouse('[<65;72', _t.monotonic())
        # Force the window to expire.
        host._orphan_mouse_until = _t.monotonic() - 1.0
        host.flush_pending_orphan_mouse_if_stale()
        assert host._orphan_mouse_buf == ''
        assert host.flushed_to_editor == []  # silent drop

    def test_stale_flush_recovers_ambiguous_text(self):
        """A buffer with no mouse signature (no prefix, no semicolons)
        that ages out is flushed to the editor as best-effort recovery
        rather than silently lost."""
        import time as _t
        host = self._make()
        # Open the window so a naked-digit fragment buffers.
        host._open_orphan_mouse_window(_t.monotonic())
        host._absorb_orphan_mouse('123', _t.monotonic())
        # Time out the buffer.
        host._orphan_mouse_until = _t.monotonic() - 1.0
        host.flush_pending_orphan_mouse_if_stale()
        assert host._orphan_mouse_buf == ''
        assert host.flushed_to_editor == ['123']

    def test_intact_csi_passes_through(self):
        """``\x1b[<…M`` is left for ``normalize_key`` — the absorber
        must not eat it (otherwise wheel scroll breaks)."""
        import time as _t
        host = self._make()
        out = host._absorb_orphan_mouse('\x1b[<65;72;92M', _t.monotonic())
        assert out == '\x1b[<65;72;92M'
        assert host._orphan_mouse_buf == ''
