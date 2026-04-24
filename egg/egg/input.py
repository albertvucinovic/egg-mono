"""Input handling mixin for the egg application."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from eggthreads import interrupt_thread

from .utils import read_clipboard
from eggthreads import sanitize_terminal_text


# How long a bare ESC is held in the input pipeline before it's dispatched
# as a standalone Esc press. If the next byte(s) arrive within this window
# and look like a CSI or SS3 continuation (``[`` or ``O``), we merge them
# so downstream code sees the full escape sequence (mouse reports, arrow
# keys, etc.) rather than a bare Esc followed by stray text. Widened to
# 200 ms to cover slower split deliveries — on busy async loops readchar
# occasionally stretches the gap between the ESC byte and its tail.
_ESC_DEBOUNCE_SEC = 0.20

# Regex matching the printable body of an SGR mouse report stripped of
# its ``\x1b[<`` prefix (``<button;col;row[Mm]``). We use it as a
# belt-and-braces matcher for cases where the ESC has already been
# flushed before the tail arrived — without this the tail would leak
# into the editor as typed text.
import re as _re
_ORPHAN_SGR_MOUSE_RE = _re.compile(r"^\[<-?\d+;-?\d+;-?\d+[Mm]$")
del _re


class InputMixin:
    """Mixin providing keyboard input handling: handle_key."""

    def handle_key(self, key: str) -> bool:
        """Handle a single key press from the input panel.

        Returns True if the key was handled and the app should continue,
        False if the app should exit.
        """
        now = time.monotonic()

        # ESC re-attachment: readchar can split a long escape sequence
        # (notably SGR mouse reports like ``\x1b[<64;10;5M``) between
        # successive readkey() calls — delivering the ESC alone and the
        # rest as continuation bytes. If we recently buffered a bare ESC
        # and this key looks like a CSI/SS3 tail, merge them so
        # normalize_key / the mouse parser see the full sequence.
        if getattr(self, '_pending_esc', False):
            pending_age = now - float(getattr(self, '_pending_esc_time', 0) or 0)
            if isinstance(key, str) and key and key[0] in ('[', 'O') and pending_age < _ESC_DEBOUNCE_SEC:
                key = '\x1b' + key
                self._pending_esc = False
                self._pending_esc_time = 0.0
            else:
                # Not a continuation — dispatch the deferred Esc first,
                # then process this key as normal.
                self._pending_esc = False
                self._pending_esc_time = 0.0
                self._dispatch_bare_esc()

        # Assemble multi-part ANSI escape sequences (e.g. long SGR mouse
        # reports) so downstream checks always see complete keys. The
        # editor wrapper's normalize_key is stateful and idempotent on
        # already-complete keys, so routing through it here does not
        # break the eventual _handle_key call that also invokes it.
        try:
            ed_wrapper = self.input_panel.editor
            if hasattr(ed_wrapper, 'normalize_key'):
                normalized = ed_wrapper.normalize_key(key)
                if normalized is None:
                    return True  # still accumulating — wait for next byte
                key = normalized
        except Exception:
            pass

        # Bracketed paste must be handled before Enter/Ctrl shortcuts. In the
        # real app input flows through this mixin first, so a paste payload
        # delivered as separate chunks (especially a literal "\n") would
        # otherwise be interpreted as submit/newline instead of text.
        if isinstance(key, str):
            try:
                ed_wrapper = self.input_panel.editor
                paste_active = bool(getattr(ed_wrapper, '_bracketed_paste_active', False))
                if paste_active or '\x1b[200~' in key or '\x1b[201~' in key:
                    return bool(ed_wrapper._handle_key(key))
            except Exception:
                pass

        # Defer a bare ESC to give the rest of a split escape sequence a
        # chance to arrive before we fire the "Esc press" action. The
        # main loop calls flush_pending_esc_if_stale() after each input
        # drain to complete standalone Esc presses that see no follow-up.
        if isinstance(key, str) and key == '\x1b':
            self._pending_esc = True
            self._pending_esc_time = now
            return True

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

        # In-app scrolling (since alt-screen disables native terminal
        # scrollback). PageUp / PageDown / End address the renderer's
        # scrollback model, sliding the history view above the live region.
        # Mouse wheel events (SGR: "\x1b[<button;col;rowM|m") also map here.
        if isinstance(key, str):
            renderer = getattr(self, '_renderer', None)
            if renderer is not None and hasattr(renderer, 'scroll'):
                # PageUp: \x1b[5~ (most terminals). PageDown: \x1b[6~.
                # End: \x1b[F, \x1b[4~, or \x1bOF depending on terminal.
                if key in ('\x1b[5~', '\x1b[5;2~'):
                    try:
                        import shutil as _shutil
                        step = max(1, _shutil.get_terminal_size(fallback=(100, 24)).lines // 2)
                    except Exception:
                        step = 5
                    renderer.scroll(step)
                    return True
                if key in ('\x1b[6~', '\x1b[6;2~'):
                    try:
                        import shutil as _shutil
                        step = max(1, _shutil.get_terminal_size(fallback=(100, 24)).lines // 2)
                    except Exception:
                        step = 5
                    renderer.scroll(-step)
                    return True
                if key in ('\x1b[F', '\x1b[4~', '\x1bOF') and hasattr(renderer, 'scroll_to_bottom'):
                    renderer.scroll_to_bottom()
                    return True

                # SGR mouse events: "\x1b[<button;col;row" + ("M" = press, "m" = release).
                # Wheel buttons: 64 = up, 65 = down (modifier bits may be set).
                # Accept the orphaned tail form ``[<...M|m`` too — when
                # readchar's split delivery outran the ESC debounce, the
                # ``\x1b`` has already been flushed and only the tail
                # reaches us; still a mouse event, should not leak into
                # the editor.
                body_src: Optional[str] = None
                if key.startswith('\x1b[<') and (key.endswith('M') or key.endswith('m')):
                    body_src = key[3:-1]
                elif _ORPHAN_SGR_MOUSE_RE.match(key):
                    body_src = key[2:-1]
                if body_src is not None:
                    try:
                        fields = body_src.split(';')
                        if len(fields) >= 3:
                            button = int(fields[0])
                            # Wheel: button >= 64, bit 1 = horizontal (skip).
                            if 64 <= button < 96 and (button & 2) == 0:
                                # Only react to the press (capital "M");
                                # some terminals emit a release too.
                                if key.endswith('M'):
                                    is_down = bool(button & 1)
                                    # Shift/ctrl modifier bits boost the scroll step.
                                    fast = bool(button & (4 | 16))
                                    step = 10 if fast else 3
                                    renderer.scroll(-step if is_down else step)
                                return True
                            # Non-wheel mouse events: swallow so they don't
                            # reach the editor as garbage keystrokes.
                            return True
                    except Exception:
                        return True
        # Double-Esc (``\x1b\x1b``) sent by some terminals as a logical
        # escape press. Bare single-Esc is deferred above via the
        # _pending_esc debounce path (its dispatch eventually goes
        # through _dispatch_bare_esc() too).
        if isinstance(key, str) and key == '\x1b\x1b':
            self._dispatch_bare_esc()
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
                    "stream_kind": None,
                    "started_at": None,
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
                safe_content = sanitize_terminal_text(content)
                self.input_panel.editor.editor.set_text(safe_content)
                # Move cursor to start of pasted text so user sees beginning
                self.input_panel.editor.editor.cursor.row = 0
                self.input_panel.editor.editor.cursor.col = 0
                self.input_panel.editor.editor._clamp_cursor()
                # Reset scroll positions to show from start
                self.input_panel._scroll_top = 0
                self.input_panel._hscroll_left = 0
                self.log_system(f'Pasted {len(safe_content)} characters from clipboard.')
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

    def _dispatch_bare_esc(self) -> None:
        """Run the actions that should fire on a standalone Esc press."""
        try:
            self.log_system(f"Esc-like key received: {repr(chr(0x1b))}")
        except Exception:
            pass
        try:
            ed = self.input_panel.editor.editor
            ed.handle_key('escape')
            if hasattr(ed, '_completion_active'):
                ed._completion_active = False
            if hasattr(ed, '_completion_items'):
                ed._completion_items = []
            if hasattr(ed, '_completion_index'):
                ed._completion_index = 0
        except Exception:
            pass

    def flush_pending_esc_if_stale(self) -> None:
        """Flush a deferred bare ESC once the debounce window has elapsed.

        Called from the main loop after draining the input queue. If a
        bare ESC has been sitting long enough that no CSI/SS3 tail is
        coming, we conclude it was a standalone Esc press and dispatch
        it now.
        """
        if not getattr(self, '_pending_esc', False):
            return
        age = time.monotonic() - float(getattr(self, '_pending_esc_time', 0) or 0)
        if age < _ESC_DEBOUNCE_SEC:
            return
        self._pending_esc = False
        self._pending_esc_time = 0.0
        self._dispatch_bare_esc()
