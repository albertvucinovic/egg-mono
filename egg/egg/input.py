"""Input handling mixin for the egg application."""
from __future__ import annotations

from typing import Any, Dict

from eggthreads import interrupt_thread

from .utils import read_clipboard


class InputMixin:
    """Mixin providing keyboard input handling: handle_key."""

    def handle_key(self, key: str) -> bool:
        """Handle a single key press from the input panel.

        Returns True if the key was handled and the app should continue,
        False if the app should exit.
        """
        # Ctrl+D sends, Ctrl+E clears input, Ctrl+C exits
        try:
            import readchar  # type: ignore
            ctrl_d = getattr(readchar.key, 'CTRL_D', '\x04')
            ctrl_c = getattr(readchar.key, 'CTRL_C', '\x03')
            ctrl_e = getattr(readchar.key, 'CTRL_E', '\x05')
            ctrl_p = getattr(readchar.key, 'CTRL_P', '\x10')
            enter_key = getattr(readchar.key, 'ENTER', '\r')
        except Exception:
            ctrl_d = '\x04'
            ctrl_c = '\x03'
            ctrl_e = '\x05'
            ctrl_p = '\x10'
            enter_key = '\r'
        # Esc handling: log and forward a logical 'escape' to the editor.
        # Some terminals send a single ESC ('\x1b'), others double ('\x1b\x1b').
        try:
            esc = getattr(readchar.key, 'ESC', '\x1b')  # type: ignore[name-defined]
        except Exception:
            esc = '\x1b'
        esc2 = esc + esc
        if isinstance(key, str) and (key == esc or key == esc2):
            try:
                self.log_system(f"Esc-like key received: {repr(key)}")
            except Exception:
                pass
            try:
                ed = self.input_panel.editor.editor
                # Ask the editor to handle a logical escape first
                ed.handle_key('escape')
                # Then forcefully clear any active completion popup in case
                # the terminal sent a non-standard ESC sequence.
                if hasattr(ed, '_completion_active'):
                    ed._completion_active = False
                if hasattr(ed, '_completion_items'):
                    ed._completion_items = []
                if hasattr(ed, '_completion_index'):
                    ed._completion_index = 0
            except Exception:
                pass
            return True
        # Ctrl+C: interrupt/cancel first, quit only when idle with empty input
        if key == ctrl_c or key == '\x03':
            # Current editor contents
            try:
                current_text = self.input_panel.get_text()
            except Exception:
                current_text = ""
            text_empty = not (current_text.strip())

            # Coarse thread state
            try:
                from eggthreads import thread_state  # type: ignore
                thread_st = thread_state(self.db, self.current_thread)
            except Exception:
                thread_st = "unknown"

            # Is there an active stream (LLM or tool) for this thread?
            try:
                row_open = self.db.current_open(self.current_thread)
            except Exception:
                row_open = None
            has_active_stream = row_open is not None

            # When there is active work (streaming or pending tool approvals),
            # treat Ctrl+C as "interrupt/cancel" without quitting.
            if has_active_stream or thread_st in ("running", "waiting_tool_approval", "waiting_output_approval"):
                # Interrupt any in-flight stream (LLM or tools)
                try:
                    interrupt_thread(self.db, self.current_thread)
                except Exception:
                    pass
                # Best-effort: cancel pending/running tool calls so they do not
                # continue or require further approval.
                try:
                    self.cancel_pending_tools_on_interrupt()
                except Exception:
                    pass
                # Reset live streaming state so UI stops showing partial output
                self._live_state = {
                    "active_invoke": None,
                    "content": "",
                    "reason": "",
                    "tools": {},
                    "tc_text": {},
                    "tc_order": [],
                }
                self.log_system('Interrupted current stream/tool execution with Ctrl+C (thread remains open).')
                # Recompute any approval prompts after cancellation
                self.compute_pending_prompt()
                return True

            # No active work. If there's text in the input panel, clear it.
            if not text_empty:
                self.input_panel.clear_text()
                try:
                    self.log_system('Input cleared with Ctrl+C (press Ctrl+C again on empty input to quit).')
                except Exception:
                    pass
                return True

            # Idle thread and empty input -> quit.
            self.log_system('Exiting on Ctrl+C (no active work and empty input).')
            self.running = False
            return False
        # Send on Ctrl+D always (but if we have a pending approval
        # prompt, interpret it as an approval answer first, regardless
        # of /enterMode).
        if key == ctrl_d or key == '\x04':
            # First, try to handle any pending approval using the current
            # input text as the answer. This works in both /enterMode
            # send and newline.
            if self.handle_pending_approval_answer(self.input_panel.get_text(), source='Ctrl+D'):
                return True
            # No pending approval (or unrecognized answer), treat Ctrl+D
            # as normal send.
            text = self.input_panel.get_text().strip()
            if text:
                try:
                    should_clear = self.on_submit(text)
                except Exception as e:
                    self.log_system(f"Submit error: {e}")
                    should_clear = True
            else:
                should_clear = True
            if should_clear:
                self.input_panel.clear_text()
                self.input_panel.increment_message_count()
            return True
        # Clear input on Ctrl+E
        if key == ctrl_e or key == '\x05':
            self.input_panel.clear_text()
            try:
                self.log_system('Input cleared.')
            except Exception:
                pass
            return True
        # Paste clipboard on Ctrl+P
        if key == ctrl_p or key == '\x10':
            content = read_clipboard()
            if content is None:
                self.log_system('Failed to read clipboard.')
            elif content == '':
                self.log_system('Clipboard is empty.')
            else:
                self.input_panel.editor.editor.set_text(content)
                # Move cursor to start of pasted text so user sees beginning
                self.input_panel.editor.editor.cursor.row = 0
                self.input_panel.editor.editor.cursor.col = 0
                self.input_panel.editor.editor._clamp_cursor()
                # Reset scroll positions to show from start
                self.input_panel._scroll_top = 0
                self.input_panel._hscroll_left = 0
                self.log_system(f'Pasted {len(content)} characters from clipboard.')
            return True
        # Newline insertion shortcuts (independent of /enterMode):
        #   - Shift+Enter
        #   - Alt+Enter
        # These are normalized by eggdisplay into logical key names.
        if key in ('shift-enter', 'alt-enter'):
            try:
                self.input_panel.editor.editor.insert_newline()
            except Exception:
                pass
            return True

        # Enter behavior depends on mode
        if key in (enter_key, '\r', '\n'):
            # If we have a pending approval prompt and Enter-sends mode
            # is active, interpret y/n/o/a answers via the same helper as
            # Ctrl+D before treating Enter as a normal send.
            if self.enter_sends and self.handle_pending_approval_answer(self.input_panel.get_text(), source='Enter'):
                return True
            if self.enter_sends:
                text = self.input_panel.get_text().strip()
                if text:
                    try:
                        should_clear = self.on_submit(text)
                    except Exception as e:
                        self.log_system(f"Submit error: {e}")
                        should_clear = True
                else:
                    should_clear = True
                if should_clear:
                    self.input_panel.clear_text()
                    self.input_panel.increment_message_count()
                return True
            else:
                # Insert newline in editor
                try:
                    self.input_panel.editor.editor.insert_newline()
                except Exception:
                    pass
                return True
        # delegate to editor engine
        return self.input_panel.editor._handle_key(key)
