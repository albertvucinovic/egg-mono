"""Input handling mixin for the egg application."""
from __future__ import annotations

import time
from typing import Optional

from eggthreads import InputHistoryNavigator, interrupt_thread, list_input_history, record_submitted_command

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

# SGR mouse reports have shape ``\x1b[<button;col;row(M|m)``. readchar's
# split delivery can sever any combination of the leading bytes
# (``\x1b``, ``[``, ``<``) across multiple readkey() calls, and the
# numeric body itself can arrive in pieces. Each ORPHAN tail shape —
# ``\x1b``-stripped (``[<…``), ``\x1b[``-stripped (``<…``), or
# ``\x1b[<``-stripped (naked digits) — must be recognised so it does
# not leak into the editor as typed text. The intact ``\x1b[<…``
# form is handled by ``normalize_key`` in the editor wrapper, which
# already buffers incomplete CSI sequences via ``_ansi_pending``;
# duplicating that here would race with it, so the absorber below
# deliberately does NOT match ``\x1b[<…`` prefixes.
import re as _re

_MOUSE_PREFIXES = ("[<", "<")
_MOUSE_BODY_FULL_RE = _re.compile(r"^-?\d+;-?\d+;-?\d+[Mm]$")
# Partial body: any prefix of "<digits>;<digits>;<digits>" that could
# still grow into a full report. Empty body is also accepted (the input
# may be just the prefix bytes ``\x1b[<`` / ``[<`` / ``<``).
_MOUSE_BODY_PARTIAL_RE = _re.compile(r"^-?\d*(?:;-?\d*){0,2}$")

# Window after a bare-ESC dispatch during which we treat naked-digit
# fragments (no ``[<`` or ``<`` prefix) as suspected orphan mouse tails
# rather than user typing. Outside this window, naked-digit fragments
# fall through to the editor as before.
_ORPHAN_MOUSE_WINDOW_SEC = 0.30

# These mnemonic Ctrl+Alt chords pass through stock tmux and avoid the audited
# Sway and terminal bindings. Terminals encode them as ESC plus the corresponding
# control byte; Ctrl+Alt+V intentionally overrides Readline's Meta+Ctrl+V only
# inside Egg's editor.
_TOGGLE_AUTO_APPROVAL_KEY = "\x1b\x01"
_TOGGLE_SANDBOXING_KEY = "\x1b\x18"
_CYCLE_DISPLAY_VERBOSITY_KEY = "\x1b\x16"

KEYBOARD_SHORTCUTS_HELP = """Keyboard shortcuts:
  Actions:
    Ctrl+Alt+A — Toggle tool auto-approval (/toggleAutoApproval).
    Ctrl+Alt+X — Toggle sandboxing (/toggleSandboxing; X for sandboX).
    Ctrl+Alt+V — Cycle transcript verbosity min → medium → max → min.
    Enter or Ctrl+D — Send (Enter follows /enterMode).
    Shift+Enter or Alt+Enter — Insert a newline.
    Ctrl+E — Clear the input draft.
    Ctrl+P — Replace the draft with clipboard text.
    Ctrl+C — Interrupt active work; otherwise clear a draft; on idle empty input, quit.
  Input and completion:
    Tab — Accept the selected autocomplete suggestion.
    Up/Down — Move between lines/suggestions; at first/last line, recall older/newer inputs.
    Left/Right — Move the cursor; Home/End — move to the line boundary.
    Esc — Dismiss autocomplete.
    Backspace/Delete — Delete before/at the cursor.
  Transcript:
    PageUp or Shift+PageUp — Scroll up half a page.
    PageDown or Shift+PageDown — Scroll down half a page."""


def _strip_mouse_prefix(s: str) -> tuple:
    """Return (prefix, body) where prefix is one of the recognised mouse
    prefixes (``\x1b[<`` / ``[<`` / ``<``) or ``""`` if none, and body is
    the remainder. Longest prefix wins.
    """
    for p in _MOUSE_PREFIXES:
        if s.startswith(p):
            return p, s[len(p):]
    return "", s


def _classify_mouse_fragment(s: str, *, allow_bare: bool) -> str:
    """Classify ``s`` as an SGR mouse fragment.

    Returns one of ``"full"`` (a complete mouse report at any
    prefix-strip level — discard), ``"partial"`` (could still grow
    into a full report — buffer), or ``"none"`` (not a mouse fragment).

    ``allow_bare`` controls whether naked-digit tails (no ``[<`` or
    ``<`` prefix) are eligible. They are only trusted as mouse fragments
    inside the post-ESC suspicion window, otherwise they are very likely
    user input and must be passed through.
    """
    if not s:
        return "none"
    prefix, body = _strip_mouse_prefix(s)
    if prefix == "" and not allow_bare:
        return "none"
    if _MOUSE_BODY_FULL_RE.match(body):
        return "full"
    if body == "":
        # We have only the prefix bytes — definitely a partial.
        return "partial" if prefix else "none"
    if _MOUSE_BODY_PARTIAL_RE.match(body):
        return "partial"
    return "none"


del _re


class InputMixin:
    """Mixin providing keyboard input handling: handle_key."""

    # The command help renderer discovers this optional client-specific text.
    keyboard_shortcuts_help = KEYBOARD_SHORTCUTS_HELP

    def _input_history_navigator(self) -> InputHistoryNavigator:
        navigators = getattr(self, '_input_history_navigators', None)
        if not isinstance(navigators, dict):
            navigators = {}
            self._input_history_navigators = navigators
        thread_id = str(self.current_thread)
        navigator = navigators.get(thread_id)
        if navigator is None:
            try:
                entries = list_input_history(self.db, thread_id)
            except Exception as exc:
                entries = []
                self.log_system(f'Failed to load input history: {exc}')
            navigator = InputHistoryNavigator(entries)
            navigators[thread_id] = navigator
        return navigator

    def _reset_input_history_navigation(self, *, reload_entries: bool = False) -> None:
        navigators = getattr(self, '_input_history_navigators', None)
        if not isinstance(navigators, dict):
            return
        navigator = navigators.get(str(getattr(self, 'current_thread', '')))
        if navigator is None:
            return
        if reload_entries:
            try:
                entries = list_input_history(self.db, str(self.current_thread))
            except Exception as exc:
                self.log_system(f'Failed to refresh input history: {exc}')
                navigator.reset()
                return
            navigator.replace_entries(entries)
        else:
            navigator.reset()

    def _replace_input_from_history(self, text: str, *, at_start: bool) -> None:
        editor = self.input_panel.editor.editor
        editor.set_text(text)
        lines = getattr(editor, 'lines', text.split('\n') if text else [''])
        if at_start:
            editor.cursor.row = 0
            editor.cursor.col = 0
        else:
            editor.cursor.row = max(0, len(lines) - 1)
            editor.cursor.col = len(lines[editor.cursor.row]) if lines else 0
        editor._clamp_cursor()
        if at_start:
            self.input_panel._scroll_top = 0
        else:
            try:
                viewport = max(1, int(getattr(self.input_panel, 'current_height', 8)) - 2)
            except Exception:
                viewport = 6
            self.input_panel._scroll_top = max(0, int(editor.cursor.row) - viewport + 1)
        self.input_panel._hscroll_left = 0

    def _input_navigation_key(self, key: str) -> bool:
        """Return whether *key* navigates without mutating the input."""

        cursor_keys = {
            'up', 'down', 'left', 'right', 'home', 'end',
            '\x1b[A', '\x1b[B', '\x1b[C', '\x1b[D',
            '\x1b[H', '\x1b[F', '\x1bOH', '\x1bOF', '\x1b[1~', '\x1b[4~',
            '\x1b[5~', '\x1b[5;2~', '\x1b[6~', '\x1b[6;2~',
        }
        if key in cursor_keys:
            return True
        try:
            import readchar  # type: ignore

            return key in {
                getattr(readchar.key, name, object())
                for name in ('UP', 'DOWN', 'LEFT', 'RIGHT', 'HOME', 'END')
            }
        except Exception:
            return False

    @staticmethod
    def _is_up_key(key: str) -> bool:
        if key in ('\x1b[A', 'up'):
            return True
        try:
            import readchar  # type: ignore

            return key == getattr(readchar.key, 'UP', object())
        except Exception:
            return False

    @staticmethod
    def _is_down_key(key: str) -> bool:
        if key in ('\x1b[B', 'down'):
            return True
        try:
            import readchar  # type: ignore

            return key == getattr(readchar.key, 'DOWN', object())
        except Exception:
            return False

    def _navigate_input_history(self, direction: str) -> bool:
        editor = self.input_panel.editor.editor
        if bool(getattr(editor, '_completion_active', False) or getattr(editor, '_completion_pending', False)):
            return False
        cursor = getattr(editor, 'cursor', None)
        lines = getattr(editor, 'lines', None)
        if cursor is None or not isinstance(lines, list) or not lines:
            return False
        navigator = self._input_history_navigator()
        if direction == 'older':
            if int(cursor.row) != 0:
                return False
            current_text = editor.get_text() if hasattr(editor, 'get_text') else self.input_panel.get_text()
            recalled = navigator.older(current_text)
            if recalled is None:
                return False
            self._replace_input_from_history(recalled, at_start=True)
            return True
        if int(cursor.row) != len(lines) - 1:
            return False
        recalled = navigator.newer()
        if recalled is None:
            return False
        self._replace_input_from_history(recalled, at_start=False)
        return True

    def _record_submitted_command(self, text: str) -> bool:
        """Persist one command submission before dispatching it.

        Command execution must not proceed when the durable history write fails:
        otherwise a successfully executed command could disappear from reusable
        input history, violating the submission invariant introduced by this
        feature.
        """

        try:
            record_submitted_command(self.db, self.current_thread, text, source='egg')
        except Exception as exc:
            self.log_system(f'Failed to record input history: {exc}')
            return False
        self._reset_input_history_navigation(reload_entries=True)
        return True

    def _note_input_submission(self, text: str, *, reload_entries: bool = True) -> None:
        # Commands refresh immediately in _record_submitted_command(). Normal
        # messages publish input.submitted in the same process and should be
        # available on the very next Up press without waiting for the watcher.
        if reload_entries and text and not text.startswith(('/', '$')):
            self._reset_input_history_navigation(reload_entries=True)
            return
        self._reset_input_history_navigation()

    def _finish_input_submission(
        self,
        text: str,
        *,
        should_clear: bool,
        reload_entries: bool = True,
    ) -> None:
        if not should_clear:
            return
        self._note_input_submission(text, reload_entries=reload_entries)
        self.input_panel.clear_text()
        self.input_panel.increment_message_count()

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
                # then process this key as normal. Open a suspicion
                # window so any orphan SGR mouse tail that arrives next
                # (with ``\x1b[<`` already gone) is recognised even
                # without an identifying prefix.
                self._pending_esc = False
                self._pending_esc_time = 0.0
                self._open_orphan_mouse_window(now)
                self._dispatch_bare_esc()

        # Assemble multi-part ANSI escape sequences (e.g. long SGR mouse
        # reports) so downstream checks always see complete keys. The
        # editor wrapper's normalize_key is stateful and idempotent on
        # already-complete keys, so routing through it here does not
        # break the eventual _handle_key call that also invokes it.
        # Run BEFORE the orphan-mouse absorber so that intact
        # ``\x1b[<…M`` fragmented across reads is reassembled by the
        # editor's ``_ansi_pending`` path, leaving the absorber to
        # handle only the orphan tail shapes (``[<…`` / ``<…`` / naked
        # digits) that no longer carry a CSI introducer.
        try:
            ed_wrapper = self.input_panel.editor
            if hasattr(ed_wrapper, 'normalize_key'):
                normalized = ed_wrapper.normalize_key(key)
                if normalized is None:
                    return True  # still accumulating — wait for next byte
                key = normalized
        except Exception:
            pass

        # Orphan SGR mouse fragment absorber. Handles tails where the
        # ``\x1b[<`` introducer has been (partially) stripped by split
        # delivery. ``None`` means the key was buffered as an in-flight
        # partial; otherwise the (possibly canonicalised) key continues
        # through the rest of handle_key, where the wheel-scroll handler
        # interprets the full form.
        if isinstance(key, str):
            absorbed = self._absorb_orphan_mouse(key, now)
            if absorbed is None:
                return True
            key = absorbed

        # Bracketed paste must be handled before Enter/Ctrl shortcuts. In the
        # real app input flows through this mixin first, so a paste payload
        # delivered as separate chunks (especially a literal "\n") would
        # otherwise be interpreted as submit/newline instead of text.
        if isinstance(key, str):
            try:
                ed_wrapper = self.input_panel.editor
                paste_active = bool(getattr(ed_wrapper, '_bracketed_paste_active', False))
                if paste_active or '\x1b[200~' in key or '\x1b[201~' in key:
                    self._reset_input_history_navigation()
                    return bool(ed_wrapper._handle_key(key))
            except Exception:
                pass

        # Defer a bare ESC to give the rest of a split escape sequence a
        # chance to arrive before we fire the "Esc press" action. The
        # main loop calls flush_pending_esc_if_stale() after each input
        # drain to complete standalone Esc presses that see no follow-up.
        if isinstance(key, str) and key == '\x1b':
            self._reset_input_history_navigation()
            self._pending_esc = True
            self._pending_esc_time = now
            return True

        # App actions use Ctrl+Alt sequences that do not collide with default
        # Readline editing keys, terminal menus, or tmux's prefix table. Handle
        # them before ordinary editor input and leave the current draft untouched.
        if key == _TOGGLE_AUTO_APPROVAL_KEY:
            self.handle_command('/toggleAutoApproval')
            return True
        if key == _TOGGLE_SANDBOXING_KEY:
            self.handle_command('/toggleSandboxing')
            return True
        if key == _CYCLE_DISPLAY_VERBOSITY_KEY:
            levels = ('min', 'medium', 'max')
            current = getattr(self, '_display_verbosity', 'min')
            next_level = levels[(levels.index(current) + 1) % len(levels)] if current in levels else 'min'
            self.handle_command(f'/displayVerbosity {next_level}')
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
        # scrollback). PageUp / PageDown address the renderer's scrollback
        # model, sliding the history view above the live region.
        # Mouse wheel events (SGR: "\x1b[<button;col;rowM|m") also map here.
        if isinstance(key, str):
            renderer = getattr(self, '_renderer', None)
            if renderer is not None and hasattr(renderer, 'scroll'):
                # PageUp: \x1b[5~ (most terminals). PageDown: \x1b[6~.
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
                # SGR mouse events: "\x1b[<button;col;row" + ("M" = press, "m" = release).
                # Wheel buttons: 64 = up, 65 = down (modifier bits may be set).
                # Orphan tails (any prefix-strip level / split bodies)
                # are absorbed by the assembler above; here we only
                # interpret the *intact* sequence to drive scrolling.
                if key.startswith('\x1b[<') and (key.endswith('M') or key.endswith('m')):
                    try:
                        fields = key[3:-1].split(';')
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
                try:
                    self._live_state = self._make_live_state()
                except Exception:
                    self._live_state = {"active_invoke": None, "stream_kind": None}
                self.log_system('Interrupted current stream/tool execution with Ctrl+C (thread remains open).')
                # Recompute any approval prompts after cancellation
                self.compute_pending_prompt()
                return True

            # No active work. If there's text in the input panel, clear it.
            if not text_empty:
                self._reset_input_history_navigation()
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
            approval_text = self.input_panel.get_text()
            if self.handle_pending_approval_answer(approval_text, source='Ctrl+D'):
                self._reset_input_history_navigation()
                return True
            # No pending approval (or unrecognized answer), treat Ctrl+D
            # as normal send.
            text = self.input_panel.get_text().strip()
            try:
                has_staged_attachments = int(self._staged_attachment_count_for_current_thread()) > 0
            except Exception:
                has_staged_attachments = False
            submission_succeeded = False
            if text or has_staged_attachments:
                try:
                    if text.startswith('/'):
                        registry = getattr(self, 'command_registry', None)
                        parts = text[1:].split(None, 1)
                        cmd = parts[0] if parts else ''
                        if (
                            registry is not None
                            and cmd
                            and getattr(registry, 'is_async', lambda _name: False)(cmd)
                        ):
                            if not self._record_submitted_command(text):
                                return True
                            self._finish_input_submission(text, should_clear=True)
                            self._schedule_user_command(text)
                            return True
                    should_clear = self.on_submit(text)
                    submission_succeeded = True
                except Exception as e:
                    self.log_system(f"Submit error: {e}")
                    should_clear = True
                    submission_succeeded = False
            else:
                should_clear = True
                submission_succeeded = False
            self._finish_input_submission(
                text,
                should_clear=should_clear,
                reload_entries=submission_succeeded,
            )
            return True
        # Clear input on Ctrl+E
        if key == ctrl_e or key == '\x05':
            self._reset_input_history_navigation()
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
                self._reset_input_history_navigation()
                self.log_system(f'Pasted {len(safe_content)} characters from clipboard.')
            return True
        # Newline insertion shortcuts (independent of /enterMode):
        #   - Shift+Enter
        #   - Alt+Enter
        # These are normalized by eggdisplay into logical key names.
        if key in ('shift-enter', 'alt-enter'):
            self._reset_input_history_navigation()
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
            if self.enter_sends:
                approval_text = self.input_panel.get_text()
                if self.handle_pending_approval_answer(approval_text, source='Enter'):
                    self._reset_input_history_navigation()
                    return True
            if self.enter_sends:
                text = self.input_panel.get_text().strip()
                try:
                    has_staged_attachments = int(self._staged_attachment_count_for_current_thread()) > 0
                except Exception:
                    has_staged_attachments = False
                submission_succeeded = False
                if text or has_staged_attachments:
                    try:
                        if text.startswith('/'):
                            registry = getattr(self, 'command_registry', None)
                            parts = text[1:].split(None, 1)
                            cmd = parts[0] if parts else ''
                            if (
                                registry is not None
                                and cmd
                                and getattr(registry, 'is_async', lambda _name: False)(cmd)
                            ):
                                if not self._record_submitted_command(text):
                                    return True
                                self._finish_input_submission(text, should_clear=True)
                                self._schedule_user_command(text)
                                return True
                        should_clear = self.on_submit(text)
                        submission_succeeded = True
                    except Exception as e:
                        self.log_system(f"Submit error: {e}")
                        should_clear = True
                        submission_succeeded = False
                else:
                    should_clear = True
                    submission_succeeded = False
                self._finish_input_submission(
                    text,
                    should_clear=should_clear,
                    reload_entries=submission_succeeded,
                )
                return True
            else:
                # Insert newline in editor
                self._reset_input_history_navigation()
                try:
                    self.input_panel.editor.editor.insert_newline()
                except Exception:
                    pass
                return True
        editor = self.input_panel.editor.editor
        completion_owns_arrows = bool(
            getattr(editor, '_completion_active', False)
            or getattr(editor, '_completion_pending', False)
        )
        if not completion_owns_arrows:
            if self._is_up_key(key) and self._navigate_input_history('older'):
                return True
            if self._is_down_key(key) and self._navigate_input_history('newer'):
                return True

        # Navigation-only actions keep the saved draft/history position. Any
        # edit or completion action exits recall.
        if not self._input_navigation_key(key):
            self._reset_input_history_navigation()
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
        now = time.monotonic()
        age = now - float(getattr(self, '_pending_esc_time', 0) or 0)
        if age < _ESC_DEBOUNCE_SEC:
            return
        self._pending_esc = False
        self._pending_esc_time = 0.0
        # Open the orphan-mouse suspicion window: this is precisely the
        # case where readchar split a long mouse report and the body is
        # about to arrive without an identifying prefix.
        self._open_orphan_mouse_window(now)
        self._dispatch_bare_esc()

    def _open_orphan_mouse_window(self, now: float) -> None:
        """Mark that we just dispatched a bare ESC under suspicious
        circumstances (debounce timeout / non-CSI follow-up). Within the
        window, naked-digit fragments are treated as orphan SGR mouse
        tails rather than user typing.
        """
        until = float(getattr(self, '_orphan_mouse_until', 0.0) or 0.0)
        new_until = now + _ORPHAN_MOUSE_WINDOW_SEC
        if new_until > until:
            self._orphan_mouse_until = new_until

    def _absorb_orphan_mouse(self, key: str, now: float) -> Optional[str]:
        """Attempt to absorb ``key`` as part of an orphan SGR mouse fragment.

        Returns:
            ``None`` if the key was buffered as an in-flight partial.
            A string to continue processing in ``handle_key`` — either
            ``key`` unchanged, the canonical ``\x1b[<…M`` form (when an
            orphan completion has been re-prefixed so the wheel-scroll
            handler can interpret it), or ``buf + key`` when a buffered
            fragment turned out not to be a mouse event after all.
        """
        buf = getattr(self, '_orphan_mouse_buf', '') or ''
        until = float(getattr(self, '_orphan_mouse_until', 0.0) or 0.0)
        # Naked-digit fragments are ambiguous (could be user input). We
        # only trust them as mouse tails inside the post-ESC suspicion
        # window, OR when we are already mid-buffer for a mouse fragment
        # (so the chain stays consistent regardless of timing slop).
        allow_bare = now < until or bool(buf)

        def _normalise(s: str) -> str:
            prefix, body = _strip_mouse_prefix(s)
            return ('\x1b[<' + body) if prefix != '\x1b[<' else s

        if buf:
            candidate = buf + key
            cls = _classify_mouse_fragment(candidate, allow_bare=allow_bare)
            if cls == 'full':
                self._orphan_mouse_buf = ''
                self._orphan_mouse_until = 0.0
                # Hand a canonical sequence back so the wheel-scroll
                # handler can still drive scrolling on orphan completions.
                return _normalise(candidate)
            if cls == 'partial':
                self._orphan_mouse_buf = candidate
                # Refresh the window so subsequent chunks get the same
                # benefit-of-the-doubt as the leading fragment.
                self._orphan_mouse_until = now + _ORPHAN_MOUSE_WINDOW_SEC
                return None
            # Doesn't extend a mouse fragment. Flush the buffer as text
            # and let the current key be processed normally.
            self._orphan_mouse_buf = ''
            self._orphan_mouse_until = 0.0
            return buf + key

        cls = _classify_mouse_fragment(key, allow_bare=allow_bare)
        if cls == 'full':
            return _normalise(key)
        if cls == 'partial':
            self._orphan_mouse_buf = key
            self._orphan_mouse_until = now + _ORPHAN_MOUSE_WINDOW_SEC
            return None
        return key

    def flush_pending_orphan_mouse_if_stale(self) -> None:
        """Drain the orphan-mouse buffer if its window has elapsed.

        Mirrors ``flush_pending_esc_if_stale``: called from the main
        loop after draining the input queue. A buffer that has aged out
        is either silently discarded (if it has the unmistakable shape
        of a truncated mouse fragment — leading prefix or embedded
        ``;``) or flushed to the editor as plain text (best-effort
        recovery for ambiguous content).
        """
        buf = getattr(self, '_orphan_mouse_buf', '') or ''
        if not buf:
            return
        until = float(getattr(self, '_orphan_mouse_until', 0.0) or 0.0)
        if time.monotonic() < until:
            return
        prefix, body = _strip_mouse_prefix(buf)
        self._orphan_mouse_buf = ''
        self._orphan_mouse_until = 0.0
        if prefix or ';' in body:
            # Looks like a truncated mouse fragment whose terminator was
            # lost. Drop silently rather than typing garbage.
            return
        try:
            self.input_panel.editor._handle_key(buf)
        except Exception:
            pass
