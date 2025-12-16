"""
Rich-based text editor component with multi-line editing support.

Features:
- Multi-line string editing
- Arrow key navigation
- Text insertion, deletion, backspace
- Paste support
- External autocomplete on Tab key
- Initial text and direct text modification
- Event listeners/hooks for key events
"""

from typing import List, Optional, Callable, Dict, Any
import shutil
import textwrap
from rich.live import Live
from rich.text import Text
from rich.console import Console, Group
from rich.layout import Layout
from rich.columns import Columns
from rich import box as rich_box
from rich.errors import MarkupError
import threading
import time
import asyncio
from dataclasses import dataclass


@dataclass
class Cursor:
    """Cursor position in the editor."""
    row: int = 0
    col: int = 0


class TextEditor:
    """
    A rich.Live-based text editor component.
    
    This editor provides multi-line text editing capabilities with support for
    various keyboard operations and event hooks.
    """
    
    def __init__(
        self, 
        initial_text: str = "",
        autocomplete_callback: Optional[Callable[[str, int, int], List[str]]] = None,
        width: int = 80,
        height: int = 20
    ):
        """
        Initialize the text editor.
        
        Args:
            initial_text: Initial text content
            autocomplete_callback: Function called on Tab key for autocomplete
            width: Editor width in characters
            height: Editor height in lines
        """
        self.lines: List[str] = initial_text.split('\n') if initial_text else [""]
        self.cursor = Cursor()
        self.width = width
        self.height = height
        self.autocomplete_callback = autocomplete_callback
        self._event_listeners: Dict[str, List[Callable]] = {
            'key_press': [],
            'text_change': [],
            'cursor_move': [],
            'autocomplete': []
        }
        self._live: Optional[Live] = None
        self._running = False
        self._console = Console()
        # Autocomplete UI state
        # Each item: {"display": str, "insert": str}
        self._completion_items: List[Dict[str, str]] = []
        self._completion_index: int = 0
        self._completion_active: bool = False
        
        # Ensure cursor is within bounds
        self._clamp_cursor()
    
    def _clamp_cursor(self) -> None:
        """Ensure cursor position is within valid bounds."""
        if self.cursor.row >= len(self.lines):
            self.cursor.row = len(self.lines) - 1
        if self.cursor.row < 0:
            self.cursor.row = 0
        
        current_line = self.lines[self.cursor.row]
        if self.cursor.col > len(current_line):
            self.cursor.col = len(current_line)
        if self.cursor.col < 0:
            self.cursor.col = 0
    
    def add_event_listener(self, event_type: str, callback: Callable) -> None:
        """
        Add an event listener for editor events.
        
        Args:
            event_type: Type of event ('key_press', 'text_change', 'cursor_move', 'autocomplete')
            callback: Function to call when event occurs
        """
        if event_type in self._event_listeners:
            self._event_listeners[event_type].append(callback)
    
    def _trigger_event(self, event_type: str, *args, **kwargs) -> None:
        """Trigger all listeners for a specific event type."""
        for callback in self._event_listeners.get(event_type, []):
            try:
                # Ensure we pass the right number of arguments
                import inspect
                sig = inspect.signature(callback)
                expected_args = len(sig.parameters)
                
                if len(args) >= expected_args:
                    callback(*args[:expected_args])
                else:
                    # Pad with None for missing arguments
                    padded_args = list(args) + [None] * (expected_args - len(args))
                    callback(*padded_args)
            except Exception as e:
                self._console.print(f"Error in event listener: {e}")
    
    def insert_text(self, text: str) -> None:
        """Insert text at current cursor position."""
        if not text:
            return
        # If callers pass multi-line text, route through the block insert
        # implementation so we don't embed raw newlines inside a single line.
        if "\n" in text or "\r" in text:
            self.insert_text_block(text)
            return
        # Perform insertion first so refresh sees the updated token
        current_line = self.lines[self.cursor.row]
        new_line = current_line[:self.cursor.col] + text + current_line[self.cursor.col:]
        self.lines[self.cursor.row] = new_line
        self.cursor.col += len(text)
        # Live-refresh suggestions if popup is active
        if self._completion_active:
            self._refresh_completion()
        self._trigger_event('text_change', 'insert', self.cursor.row, self.cursor.col - len(text), text)

    def insert_text_block(self, text: str) -> None:
        """Insert a (possibly multi-line) text block at the cursor.

        This is used for pastes and any other bulk insertion.

        - Handles ``\n``/``\r\n``/``\r`` newlines.
        - Updates cursor position to the end of the inserted block.
        - Dismisses autocomplete popup if the inserted text contains a newline
          (newlines typically terminate the current token).
        """
        if not text:
            return

        # Normalize newlines to \n
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")

        # Fast path: single-line insert
        if "\n" not in normalized:
            self.insert_text(normalized)
            return

        # Multi-line insert
        if self._completion_active:
            self._completion_active = False
            self._completion_items = []
            self._completion_index = 0

        row = self.cursor.row
        col = self.cursor.col
        current_line = self.lines[row]
        before = current_line[:col]
        after = current_line[col:]

        parts = normalized.split("\n")
        first = parts[0]
        middle = parts[1:-1]
        last = parts[-1]

        replacement = [before + first, *middle, last + after]
        # Replace the current line with the new block.
        self.lines[row:row + 1] = replacement

        # Cursor ends at end of inserted text (before the original "after")
        self.cursor.row = row + len(replacement) - 1
        self.cursor.col = len(last)
        self._clamp_cursor()

        self._trigger_event('text_change', 'insert_block', row, col, text)
    
    def delete_char(self) -> None:
        """Delete character at cursor position."""
        current_line = self.lines[self.cursor.row]
        if self.cursor.col < len(current_line):
            deleted_char = current_line[self.cursor.col]
            new_line = current_line[:self.cursor.col] + current_line[self.cursor.col + 1:]
            self.lines[self.cursor.row] = new_line
            # Refresh after the deletion so suggestions reflect the new token
            if self._completion_active:
                self._refresh_completion()
            self._trigger_event('text_change', 'delete', self.cursor.row, self.cursor.col, deleted_char)
    
    def backspace(self) -> None:
        """Delete character before cursor position."""
        if self.cursor.col > 0:
            current_line = self.lines[self.cursor.row]
            deleted_char = current_line[self.cursor.col - 1]
            new_line = current_line[:self.cursor.col - 1] + current_line[self.cursor.col:]
            self.lines[self.cursor.row] = new_line
            self.cursor.col -= 1
            if self._completion_active:
                self._refresh_completion()
            self._trigger_event('text_change', 'backspace', self.cursor.row, self.cursor.col, deleted_char)
        elif self.cursor.row > 0:
            # Merge with previous line
            prev_line = self.lines[self.cursor.row - 1]
            current_line = self.lines[self.cursor.row]
            self.lines[self.cursor.row - 1] = prev_line + current_line
            self.lines.pop(self.cursor.row)
            self.cursor.row -= 1
            self.cursor.col = len(prev_line)
            if self._completion_active:
                self._refresh_completion()
            self._trigger_event('text_change', 'backspace_merge', self.cursor.row, self.cursor.col)
    
    def insert_newline(self) -> None:
        """Insert a newline at cursor position."""
        # Newline typically ends the current token; dismiss popup if visible
        if self._completion_active:
            self._completion_active = False
            self._completion_items = []
            self._completion_index = 0
        current_line = self.lines[self.cursor.row]
        before_cursor = current_line[:self.cursor.col]
        after_cursor = current_line[self.cursor.col:]
        
        self.lines[self.cursor.row] = before_cursor
        self.lines.insert(self.cursor.row + 1, after_cursor)
        
        self.cursor.row += 1
        self.cursor.col = 0
        self._trigger_event('text_change', 'newline', self.cursor.row - 1, self.cursor.col)
    
    def move_cursor(self, delta_row: int, delta_col: int) -> None:
        """Move cursor by specified deltas."""
        old_row, old_col = self.cursor.row, self.cursor.col
        
        self.cursor.row += delta_row
        self.cursor.col += delta_col
        self._clamp_cursor()
        
        if old_row != self.cursor.row or old_col != self.cursor.col:
            if self._completion_active:
                self._refresh_completion()
            self._trigger_event('cursor_move', old_row, old_col, self.cursor.row, self.cursor.col)
    
    def handle_key(self, key: str) -> bool:
        """
        Handle a key press.
        
        Args:
            key: The key that was pressed
            
        Returns:
            True if key was handled, False otherwise
        """
        self._trigger_event('key_press', key, self.cursor.row, self.cursor.col)
        
        # When completion is active, handle navigation keys first
        if self._completion_active and key in ("up", "down", "escape", "enter"):
            if key == "up":
                if self._completion_items:
                    self._completion_index = (self._completion_index - 1) % len(self._completion_items)
                return True
            if key == "down":
                if self._completion_items:
                    self._completion_index = (self._completion_index + 1) % len(self._completion_items)
                return True
            if key == "escape":
                # Dismiss the popup on Esc
                self._completion_active = False
                self._completion_items = []
                self._completion_index = 0
                return True
            if key == "enter":
                return self.accept_completion()

        if key == "up":
            self.move_cursor(-1, 0)
            return True
        elif key == "down":
            self.move_cursor(1, 0)
            return True
        elif key == "left":
            self.move_cursor(0, -1)
            return True
        elif key == "right":
            self.move_cursor(0, 1)
            return True
        elif key == "home":
            # Move to beginning of current line
            self.cursor.col = 0
            self._clamp_cursor()
            self._trigger_event('cursor_move', None, None, self.cursor.row, self.cursor.col)
            return True
        elif key == "end":
            # Move to end of current line
            self.cursor.col = len(self.lines[self.cursor.row])
            self._clamp_cursor()
            self._trigger_event('cursor_move', None, None, self.cursor.row, self.cursor.col)
            return True
        elif key == "backspace":
            self.backspace()
            return True
        elif key == "delete":
            self.delete_char()
            return True
        elif key == "enter":
            self.insert_newline()
            return True
        elif key == "tab":
            return self._handle_tab()
        elif isinstance(key, str) and len(key) > 1 and not key.startswith('\x1b'):
            # Treat multi-character printable strings as a paste chunk.
            self.insert_text_block(key)
            return True
        elif len(key) == 1 and key.isprintable():
            self.insert_text(key)
            return True
        
        return False
    
    def _handle_tab(self) -> bool:
        """Handle Tab key for autocomplete."""
        if not self.autocomplete_callback:
            return False

        current_line = self.lines[self.cursor.row]
        # If already active, accept currently selected completion
        if self._completion_active and self._completion_items:
            return self.accept_completion()

        # Not active -> fetch suggestions
        raw = self.autocomplete_callback(current_line, self.cursor.row, self.cursor.col) or []
        # Normalize to list of {display,insert}
        completions: List[Dict[str, str]] = []
        for c in raw:
            if isinstance(c, str):
                completions.append({"display": c, "insert": c})
            elif isinstance(c, dict):
                disp = str(c.get("display", c.get("insert", "")))
                ins = str(c.get("insert", ""))
                item = {"display": disp, "insert": ins}
                # Preserve optional replace count for token replacement behavior
                if "replace" in c or "replace_chars" in c:
                    try:
                        item["replace"] = int(c.get("replace", c.get("replace_chars", 0)) or 0)
                    except Exception:
                        item["replace"] = 0
                if disp or ins or item.get("replace", 0):
                    completions.append(item)
            elif isinstance(c, (list, tuple)) and len(c) >= 2:
                disp = str(c[0])
                ins = str(c[1])
                completions.append({"display": disp, "insert": ins})
        # If single suggestion, insert immediately
        if len(completions) == 1:
            # Reuse accept logic so optional 'replace' is honored
            self._completion_items = completions
            self._completion_index = 0
            self._completion_active = True
            accepted = self.accept_completion()
            # Ensure popup is closed
            self._completion_active = False
            self._completion_items = []
            self._completion_index = 0
            return accepted
        # If multiple, open selection UI
        if len(completions) > 1:
            # Keep a reasonably sized list for scrolling in the UI.
            # Rendering code is responsible for windowing the list.
            self._completion_items = completions[:200]
            self._completion_index = 0
            self._completion_active = True
            # Fire autocomplete event with no insertion
            self._trigger_event('autocomplete', '', self.cursor.row, self.cursor.col)
            return True
        return False

    def _refresh_completion(self) -> None:
        """Refresh visible completion items based on current line/cursor.

        If no suggestions are returned, dismiss the popup.
        """
        try:
            if not self.autocomplete_callback:
                self._completion_active = False
                self._completion_items = []
                self._completion_index = 0
                return
            current_line = self.lines[self.cursor.row]
            raw = self.autocomplete_callback(current_line, self.cursor.row, self.cursor.col) or []
            # Normalize like in _handle_tab
            items: List[Dict[str, str]] = []
            for c in raw:
                if isinstance(c, str):
                    items.append({"display": c, "insert": c})
                elif isinstance(c, dict):
                    disp = str(c.get("display", c.get("insert", "")))
                    ins = str(c.get("insert", ""))
                    it = {"display": disp, "insert": ins}
                    if "replace" in c or "replace_chars" in c:
                        try:
                            it["replace"] = int(c.get("replace", c.get("replace_chars", 0)) or 0)
                        except Exception:
                            it["replace"] = 0
                    if disp or ins or it.get("replace", 0):
                        items.append(it)
                elif isinstance(c, (list, tuple)) and len(c) >= 2:
                    disp = str(c[0])
                    ins = str(c[1])
                    items.append({"display": disp, "insert": ins})
            if items:
                self._completion_items = items[:200]
                if self._completion_index >= len(self._completion_items):
                    self._completion_index = 0
                self._completion_active = True
            else:
                # Dismiss when there are no results
                self._completion_active = False
                self._completion_items = []
                self._completion_index = 0
        except Exception:
            # On any error, dismiss gracefully
            self._completion_active = False
            self._completion_items = []
            self._completion_index = 0

    def accept_completion(self) -> bool:
        """Accept the currently highlighted completion (if active)."""
        if not (self._completion_active and self._completion_items):
            return False
        item = self._completion_items[self._completion_index]
        ins = item.get("insert", "") if isinstance(item, dict) else ""
        # Optional: number of characters to replace (delete) before cursor
        replace_n = 0
        if isinstance(item, dict):
            try:
                replace_n = int(item.get("replace", item.get("replace_chars", 0)) or 0)
            except Exception:
                replace_n = 0
        if replace_n > 0:
            # Delete replace_n characters of the last token before cursor, ignoring trailing whitespace
            line = self.lines[self.cursor.row]
            col = self.cursor.col
            # Find end of token ignoring trailing spaces
            i = col
            while i > 0 and line[i - 1].isspace():
                i -= 1
            n = min(replace_n, i)
            if n > 0:
                before = line[: i - n]
                after = line[i:]
                self.lines[self.cursor.row] = before + after
                self.cursor.col = i - n
        if ins:
            self.insert_text(ins)
            self._trigger_event('autocomplete', ins, self.cursor.row, self.cursor.col)
        # Clear completion state after accept
        self._completion_active = False
        self._completion_items = []
        self._completion_index = 0
        return True
    
    def get_text(self) -> str:
        """Get the current text content."""
        return '\n'.join(self.lines)
    
    def set_text(self, text: str) -> None:
        """Set the text content."""
        old_text = self.get_text()
        self.lines = text.split('\n') if text else [""]
        self._clamp_cursor()
        if old_text != text:
            self._trigger_event('text_change', 'set', -1, -1, text)
    
    def _render(self) -> Text:
        """Render the editor content."""
        text = Text()
        
        # Add some visual indicators
        text.append(f"Rich Text Editor (Lines: {len(self.lines)}, Cursor: {self.cursor.row}:{self.cursor.col})\n", style="bold blue")
        text.append("-" * min(60, self.width) + "\n")
        
        # Calculate visible lines (simple viewport)
        start_line = max(0, self.cursor.row - self.height // 2)
        end_line = min(len(self.lines), start_line + self.height - 3)  # Reserve space for header/footer
        
        for i in range(start_line, end_line):
            line = self.lines[i]
            line_num = f"{i+1:3d} | "
            
            if i == self.cursor.row:
                # Highlight current line with cursor
                text.append(line_num, style="bold green")
                before_cursor = line[:self.cursor.col]
                at_cursor = line[self.cursor.col:self.cursor.col + 1] if self.cursor.col < len(line) else " "
                after_cursor = line[self.cursor.col + 1:] if self.cursor.col < len(line) else ""
                
                text.append(before_cursor)
                text.append(at_cursor, style="black on white")
                text.append(after_cursor)
            else:
                text.append(line_num, style="dim")
                text.append(line)
            
            text.append("\n")
        
        # Add footer with instructions
        text.append("-" * min(60, self.width) + "\n", style="dim")
        text.append("Ctrl+C to exit | Tab: suggest/accept | Up/Down: navigate | Esc: cancel", style="dim")
        
        return text
    
    def run(self, *, inline: bool = True) -> None:
        """
        Run the editor in interactive mode.
        
        This starts a rich.Live session for interactive editing.
        """
        import sys
        import tty
        import termios
        
        self._running = True
        
        # Save terminal settings
        old_settings = termios.tcgetattr(sys.stdin)
        
        try:
            # Set terminal to raw mode for immediate key capture
            tty.setraw(sys.stdin.fileno())
            
            # Initial render to ensure display works
            initial_render = self._render()
            
            # Use screen=False when inline=True so the editor renders inline
            # and the terminal remains normally scrollable.
            with Live(initial_render, refresh_per_second=10, screen=not inline, console=self._console) as live:
                self._live = live
                
                # Force initial display
                live.update(initial_render, refresh=True)
                
                while self._running:
                    try:
                        # Read single character
                        char = sys.stdin.read(1)
                        
                        # Handle special keys
                        if char == '\x03':  # Ctrl+C
                            break
                        elif char == '\x1b':  # Escape sequence (arrows, etc.)
                            # Read next characters to determine the key
                            next_chars = sys.stdin.read(2)
                            if next_chars == '[A':
                                self.handle_key('up')
                            elif next_chars == '[B':
                                self.handle_key('down')
                            elif next_chars == '[C':
                                self.handle_key('right')
                            elif next_chars == '[D':
                                self.handle_key('left')
                            elif next_chars in ('[H', 'OH'):
                                self.handle_key('home')
                            elif next_chars in ('[F', 'OF'):
                                self.handle_key('end')
                            elif next_chars == '[3':  # Delete
                                sys.stdin.read(1)  # Read the ~
                                self.handle_key('delete')
                            elif next_chars == '[1':  # Home on some terms
                                tail = sys.stdin.read(1)
                                if tail == '~':
                                    self.handle_key('home')
                            elif next_chars == '[4':  # End on some terms
                                tail = sys.stdin.read(1)
                                if tail == '~':
                                    self.handle_key('end')
                        elif char == '\x7f':  # Backspace
                            self.handle_key('backspace')
                        elif char == '\r':  # Enter/Return
                            self.handle_key('enter')
                        elif char == '\t':  # Tab
                            self.handle_key('tab')
                        elif char.isprintable():
                            self.handle_key(char)
                        
                        # Update display
                        live.update(self._render())
                        
                    except KeyboardInterrupt:
                        break
                    except Exception as e:
                        # If there's an error, try to restore terminal and exit
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        print(f"\nError in editor: {e}")
                        break
        finally:
            # Restore terminal settings
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
    
    def stop(self) -> None:
        """Stop the editor."""
        self._running = False


# Real-time interactive editor components
try:
    import readchar
except ImportError:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "readchar"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import readchar

import threading
import queue
import time
from rich.console import Console
from rich.live import Live
from rich.text import Text
from rich.panel import Panel


class LiveEditorBase:
    """Base for real-time editors sharing rendering, key handling, and I/O hooks."""

    LABEL: str = "Real-time Text Editor"

    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24,
                 autocomplete_callback: Optional[Callable[[str, int, int], List[str]]] = None):
        self.editor = TextEditor(initial_text=initial_text, width=width, height=height,
                                 autocomplete_callback=autocomplete_callback)
        self.console = Console()
        self.running = False

    # ------------ Shared rendering ------------
    def _render(self) -> Text:
        text = Text()
        # Header
        text.append(f" {self.LABEL} ", style="bold white on blue")
        text.append("\n")
        text.append("─" * 60 + "\n", style="blue")

        # Content with cursor
        lines = self.editor.lines
        cursor_row, cursor_col = self.editor.cursor.row, self.editor.cursor.col

        # Show visible area around cursor
        start_line = max(0, cursor_row - 8)
        end_line = min(len(lines), start_line + 16)

        for i in range(start_line, end_line):
            line_num = f"{i+1:3d} │ "
            if i == cursor_row:
                text.append(line_num, style="bold green")
                line_content = lines[i]
                before_cursor = line_content[:cursor_col]
                cursor_char = line_content[cursor_col:cursor_col+1] if cursor_col < len(line_content) else "█"
                after_cursor = line_content[cursor_col+1:] if cursor_col < len(line_content) else ""
                text.append(before_cursor)
                text.append(cursor_char, style="black on white")
                text.append(after_cursor)
            else:
                text.append(line_num, style="dim")
                text.append(lines[i])
            text.append("\n")

        # Footer
        text.append("─" * 60 + "\n", style="blue")
        text.append(f" Line: {cursor_row + 1}, Column: {cursor_col + 1} ")
        text.append(" │ ")
        text.append(" Type directly! Ctrl+C to exit ", style="yellow")
        return text

    # ------------ Shared key handling ------------
    def _handle_key(self, key: str) -> bool:
        """Handle a key press and return False if should quit."""
        # Debug: uncomment to see what keys are being received
        # print(f"DEBUG: Key received: {repr(key)}")
        try:
            ctrl_c = readchar.key.CTRL_C
        except Exception:
            ctrl_c = "\x03"

        if key == ctrl_c:
            return False
        elif key == getattr(readchar.key, 'UP', object()) or key == '\x1b[A':
            self.editor.handle_key('up')
        elif key == getattr(readchar.key, 'DOWN', object()) or key == '\x1b[B':
            self.editor.handle_key('down')
        elif key == getattr(readchar.key, 'LEFT', object()) or key == '\x1b[D':
            self.editor.handle_key('left')
        elif key == getattr(readchar.key, 'RIGHT', object()) or key == '\x1b[C':
            self.editor.handle_key('right')
        elif key == '\x1b':
            # Plain ESC key: dismiss autocomplete popup
            self.editor.handle_key('escape')
        elif getattr(readchar.key, 'HOME', None) and key == readchar.key.HOME or key in ('\x1b[H', '\x1bOH', '\x1b[1~'):
            self.editor.handle_key('home')
        elif getattr(readchar.key, 'END', None) and key == readchar.key.END or key in ('\x1b[F', '\x1bOF', '\x1b[4~'):
            self.editor.handle_key('end')
        elif key in (getattr(readchar.key, 'BACKSPACE', '\x7f'), '\x7f', '\x08'):
            self.editor.handle_key('backspace')
        elif key == getattr(readchar.key, 'DELETE', '\x1b[3~') or key == '\x1b[3~':
            self.editor.handle_key('delete')
        elif key in (getattr(readchar.key, 'ENTER', '\r'), '\r', '\n'):
            self.editor.handle_key('enter')
        elif key == getattr(readchar.key, 'TAB', '\t') or key == '\t':
            self.editor.handle_key('tab')
        elif isinstance(key, str) and len(key) > 1 and not key.startswith('\x1b'):
            # Treat multi-character printable input as a paste. Some terminal
            # configurations (and some remote shells) deliver paste chunks as
            # multi-character strings.
            self.editor.insert_text_block(key)
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.editor.handle_key(key)
        # Unknown escape sequences are ignored
        return True

    # ------------ Shared lifecycle hooks ------------
    def _print_start(self):
        self.console.print(f"[green]{self.LABEL} Started[/green]")
        self.console.print("[dim]Start typing! Your keystrokes appear immediately. Ctrl+C to exit.[/dim]\n")

    def _print_end(self, inline: bool):
        if not inline:
            self.console.clear()
        self.console.print("[green]Editor session ended.[/green]")
        self.console.print("\n[bold]Your text:[/bold]")
        self.console.print("─" * 60)
        self.console.print(self.editor.get_text())
        self.console.print("─" * 60)


class RealTimeEditor(LiveEditorBase):
    """Threaded real-time editor using readchar + queue."""

    LABEL = " Real-time Text Editor "

    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24,
                 autocomplete_callback: Optional[Callable[[str, int, int], List[str]]] = None):
        super().__init__(initial_text=initial_text, width=width, height=height,
                         autocomplete_callback=autocomplete_callback)
        self.input_queue = queue.Queue()

    def _input_worker(self):
        """Background reader for RealTimeEditor.

        We treat Ctrl+C as a normal key and let the embedding
        application decide whether to quit. The loop exits when
        self.running is set to False.
        """
        while self.running:
            try:
                key = readchar.readkey()
                self.input_queue.put(key)
            except KeyboardInterrupt:
                # Normalize KeyboardInterrupt to a Ctrl+C key and
                # continue, letting the main loop handle it.
                try:
                    self.input_queue.put(getattr(readchar.key, 'CTRL_C', '\x03'))
                except Exception:
                    pass
            except Exception:
                break

    def run(self, *, inline: bool = True):
        self.running = True

        input_thread = threading.Thread(target=self._input_worker, daemon=True)
        input_thread.start()

        self._print_start()

        try:
            with Live(self._render(), refresh_per_second=30, screen=not inline, console=self.console) as live:
                while self.running:
                    try:
                        while True:
                            key = self.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                break
                    except queue.Empty:
                        pass
                    live.update(self._render())
                    time.sleep(0.01)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self._print_end(inline)


class AsyncRealTimeEditor(LiveEditorBase):
    """Async real-time editor using readchar + asyncio.to_thread."""

    LABEL = " Async Real-time Editor "

    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24,
                 autocomplete_callback: Optional[Callable[[str, int, int], List[str]]] = None):
        super().__init__(initial_text=initial_text, width=width, height=height,
                         autocomplete_callback=autocomplete_callback)
        self.input_queue: asyncio.Queue = asyncio.Queue()

    async def _input_reader(self):
        """Async background reader for AsyncRealTimeEditor.

        Ctrl+C is forwarded as a regular key and the embedding
        application decides whether to quit. The loop exits when
        self.running is set to False.
        """
        while self.running:
            try:
                key = await asyncio.to_thread(readchar.readkey)
                await self.input_queue.put(key)
            except KeyboardInterrupt:
                # Normalize KeyboardInterrupt to a Ctrl+C key and
                # continue, letting the main loop handle it.
                try:
                    await self.input_queue.put(getattr(readchar.key, 'CTRL_C', '\x03'))
                except Exception:
                    pass
            except Exception:
                break

    async def run_async(self, *, inline: bool = True):
        self.running = True
        self._print_start()

        try:
            with Live(self._render(), refresh_per_second=30, screen=not inline, console=self.console) as live:
                input_task = asyncio.create_task(self._input_reader())
                while self.running:
                    try:
                        key = await asyncio.wait_for(self.input_queue.get(), timeout=0.01)
                        if not self._handle_key(key):
                            self.running = False
                            break
                    except asyncio.TimeoutError:
                        pass
                    live.update(self._render())

                input_task.cancel()
                try:
                    await input_task
                except asyncio.CancelledError:
                    pass
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self._print_end(inline)

    def run(self):
        asyncio.run(self.run_async())


class OutputPanel:
    """
    Generic output panel for displaying multi-line strings.
    
    This panel is completely agnostic about the content - it just displays
    multi-line text with automatic height adjustment.
    """
    
    @dataclass
    class PanelStyle:
        """Display options for OutputPanel."""
        border_style: str = "blue"
        box: Any = rich_box.SQUARE
        title_style: str = "bold"
        title_align: str = "left"
        show_header: bool = True
        header_style: str = "bold white on blue"
        header_separator_char: str = "─"
        header_separator_style: str = "blue"
        # How to treat long logical lines:
        #   - "wrap" (default): wrap to multiple visual lines
        #   - "crop": crop/clip each logical line to panel width
        line_wrap_mode: str = "wrap"

    def __init__(self, title: str = "Output", initial_height: int = 8, max_height: int = 20,
                 style: Optional['OutputPanel.PanelStyle'] = None, columns_hint: int = 1):
        """
        Initialize the output panel.
        
        Args:
            title: Panel title
            initial_height: Initial panel height in lines
            max_height: Maximum panel height in lines
        """
        self.title = title
        self.current_height = initial_height
        self.max_height = max_height
        self.content = ""
        self.style = style or OutputPanel.PanelStyle()
        # Hint how many equal-width columns this panel shares row with (for width estimate)
        self.columns_hint = max(1, int(columns_hint or 1))
        
    def set_content(self, content: str) -> None:
        """Set the content to display."""
        self.content = content
        
    def calculate_height(self) -> int:
        """Calculate the optimal panel height based on content.

        For chat-style UIs we prefer snappy resizing rather than
        line-by-line animation, especially when content arrives
        in large chunks (e.g. initial system prompt or when
        autocomplete suggestions appear/disappear).
        """
        if not self.content:
            return int(self.current_height)

        # Count logical lines in content
        content_lines = self.content.count('\n') + 1

        # For crop-mode panels we want one row per logical line, plus
        # header, but no extra padding; for wrapped panels we keep the
        # previous behaviour.
        if getattr(self.style, "line_wrap_mode", "wrap") == "crop":
            total_lines_needed = content_lines + (4 if self.style.show_header else 1)
        else:
            total_lines_needed = content_lines + 4
        target_height = max(1, min(self.max_height, total_lines_needed))

        # Jump directly to the target height for immediate layout updates.
        self.current_height = float(target_height)
        return target_height
    
    def _resolve_box(self):
        b = self.style.box
        # Allow users to pass a string name or a rich.box object
        if isinstance(b, str):
            name = b.strip().upper()
            return getattr(rich_box, name, rich_box.SQUARE)
        return b or rich_box.SQUARE

    def render(self) -> Panel:
        """Render the panel, showing only the last lines that fit.

        Content is interpreted as Rich markup so callers can embed
        simple markup tags (e.g. ``[yellow]``) in panel bodies.
        """
        height = self.calculate_height()

        # Create content text
        content_text = Text()

        # Add optional header with stats inside content area
        content_lines = self.content.count('\n') + 1 if self.content else 0
        header_lines = 0
        #No header for now
        #if self.style.show_header:
        #    content_text.append(f"Lines: {content_lines}")
        #    content_text.append(f" | Height: {height}")
        #    content_text.append("\n")
        #    sep = self.style.header_separator_char * 70
        #    content_text.append(sep + "\n", style=self.style.header_separator_style)
        #    header_lines = 2

        # Calculate available lines for content (after header). For
        # crop-mode panels we reserve slightly less padding so that the
        # number of visible logical lines more closely matches the
        # content lines.
        if getattr(self.style, "line_wrap_mode", "wrap") == "crop":
            padding = 1
        else:
            padding = 2
        available_content_lines = max(1, height - (header_lines + padding))

        # Show only the last lines that fit
        if self.content:
            # Estimate available width per column to compute wrapped display lines
            term_cols = shutil.get_terminal_size(fallback=(100, 24)).columns
            # subtract a few chars for panel borders/padding
            approx_width = max(10, term_cols // self.columns_hint - 6)

            from rich.console import Console as _Console
            _console = _Console()

            # For most panels (line_wrap_mode="wrap"), we want wrapped
            # visual lines. For crop mode we keep one segment per logical
            # line and rely on the Panel/terminal to clip overflow.
            if getattr(self.style, "line_wrap_mode", "wrap") == "crop":
                display_segments: List[Text] = []
                for line in self.content.split('\n'):
                    if line == "":
                        display_segments.append(Text())
                        continue
                    try:
                        rich_line = Text.from_markup(line)
                    except MarkupError:
                        rich_line = Text(line)
                    # Explicitly crop to the available width so that
                    # long lines do not expand the column and break
                    # the overall layout.
                    try:
                        rich_line.truncate(approx_width, overflow="crop")
                    except Exception:
                        pass
                    display_segments.append(rich_line)
                total_segments = len(display_segments)
                if total_segments <= available_content_lines:
                    visible = display_segments
                    hidden_lines = 0
                else:
                    hidden_lines = total_segments - available_content_lines
                    visible = display_segments[-available_content_lines:]
            else:
                # Build display segments accounting for wrapping. Use Rich's
                # Text.wrap so that markup is preserved while wrapping to the
                # panel width. Each wrapped segment represents a visual line.
                display_segments: List[Text] = []
                for line in self.content.split('\n'):
                    if line == "":
                        display_segments.append(Text())
                        continue
                    try:
                        rich_line = Text.from_markup(line)
                        wrapped_segments = rich_line.wrap(_console, width=approx_width, no_wrap=False)
                        if not wrapped_segments:
                            display_segments.append(Text())
                        else:
                            display_segments.extend(wrapped_segments)
                    except MarkupError:
                        # Fallback: use plain text wrapping if markup is
                        # invalid; this will drop styling but keep layout.
                        wrapped = textwrap.wrap(
                            line,
                            width=approx_width,
                            replace_whitespace=False,
                            drop_whitespace=False,
                            break_long_words=True,
                            break_on_hyphens=False,
                        )
                        if not wrapped:
                            display_segments.append(Text())
                        else:
                            for wl in wrapped:
                                display_segments.append(Text(wl))

                # Now render only the tail that fits
                total_segments = len(display_segments)
                if total_segments <= available_content_lines:
                    visible = display_segments
                    hidden_lines = 0
                else:
                    hidden_lines = total_segments - available_content_lines
                    visible = display_segments[-available_content_lines:]

            for seg in visible:
                seg = seg.copy()
                seg.append("\n")
                content_text.append(seg)

            if hidden_lines > 0:
                try:
                    content_text.append(Text.from_markup(f"[dim]... {hidden_lines} lines above[/dim]"))
                except MarkupError:
                    content_text.append(Text(f"... {hidden_lines} lines above"))
        else:
            # Interpret "No content" string as markup as well so callers
            # can control styling if desired.
            try:
                content_text.append(Text.from_markup("[dim]No content[/dim]"))
            except MarkupError:
                content_text.append(Text("No content"))

        panel_title = f"[{self.style.title_style}]{self.title}[/{self.style.title_style}]" if self.title else None
        return Panel(
            content_text,
            title=panel_title,
            title_align=self.style.title_align,
            border_style=self.style.border_style,
            box=self._resolve_box(),
            height=height
        )


class InputPanel:
    """
    Generic input panel with text editing capabilities.
    
    This panel provides a text editor interface for user input.
    """
    
    @dataclass
    class PanelStyle:
        """Display options for InputPanel."""
        border_style: str = "green"
        box: Any = rich_box.SQUARE
        title_style: str = "bold"
        title_align: str = "left"
        show_header: bool = False
        header_style: str = "bold white on green"
        header_separator_char: str = "─"
        header_separator_style: str = "green"
        status_style: str = "dim"
        cursor_style: str = "black on white"
        line_num_style: str = "dim"
        current_line_num_style: str = "bold green"

    def __init__(self, title: str = "Input", initial_height: int = 8, max_height: int = 12,
                 style: Optional['InputPanel.PanelStyle'] = None,
                 io_mode: str = "threaded",
                 autocomplete_callback: Optional[Callable[[str, int, int], List[str]]] = None):
        """
        Initialize the input panel.
        
        Args:
            title: Panel title
            initial_height: Initial panel height in lines
            max_height: Maximum panel height in lines
        """
        self.title = title
        self.current_height = initial_height
        self.max_height = max_height
        # Choose threaded or async real-time editor
        io_mode = (io_mode or "threaded").lower()
        if io_mode not in ("threaded", "async"):
            io_mode = "threaded"
        self.io_mode = io_mode
        if io_mode == "async":
            self.editor = AsyncRealTimeEditor(
                initial_text="",
                width=80,
                height=6,
                autocomplete_callback=autocomplete_callback,
            )
        else:
            self.editor = RealTimeEditor(
                initial_text="",
                width=80,
                height=6,
                autocomplete_callback=autocomplete_callback,
            )
        self.message_count = 0
        self.style = style or InputPanel.PanelStyle()
        # Viewport state for long edits / scrolling.
        self._scroll_top: int = 0
        # Viewport state for suggestions list.
        self._suggestion_scroll_top: int = 0
        
    def calculate_height(self) -> int:
        """Calculate the optimal panel height based on editor content.

        We resize immediately to the required height so that the
        input panel and its autocomplete popup appear/disappear
        without a slow line-by-line animation.
        """
        editor_lines = len(self.editor.editor.lines)

        # The input panel height is bounded. We still include some extra room
        # for the suggestions list, but the renderer will scroll if there are
        # too many suggestions.
        extra = 0
        try:
            if getattr(self.editor.editor, "_completion_active", False):
                items = getattr(self.editor.editor, "_completion_items", []) or []
                if items:
                    extra = min(1 + len(items), 8)  # title + up to 7 visible items
        except Exception:
            extra = 0

        # 4 = line numbers + status line etc. (legacy sizing)
        target_height = max(6, min(self.max_height, editor_lines + 4 + extra))
        self.current_height = float(target_height)
        return target_height

    def _clamp_scroll(self, *, viewport_size: int) -> None:
        """Clamp the main editor scroll position."""
        total = len(self.editor.editor.lines)
        viewport_size = max(1, int(viewport_size))
        max_top = max(0, total - viewport_size)
        if self._scroll_top < 0:
            self._scroll_top = 0
        if self._scroll_top > max_top:
            self._scroll_top = max_top

    def _ensure_cursor_visible(self, *, viewport_size: int) -> None:
        """Adjust scroll so the cursor row is visible in the viewport."""
        viewport_size = max(1, int(viewport_size))
        cur = int(self.editor.editor.cursor.row)
        if cur < self._scroll_top:
            self._scroll_top = cur
        elif cur >= self._scroll_top + viewport_size:
            self._scroll_top = cur - viewport_size + 1
        self._clamp_scroll(viewport_size=viewport_size)

    def _ensure_suggestion_visible(self, *, viewport_size: int, selected_index: int, total_items: int) -> None:
        viewport_size = max(1, int(viewport_size))
        total_items = max(0, int(total_items))
        selected_index = max(0, min(int(selected_index), max(0, total_items - 1)))
        max_top = max(0, total_items - viewport_size)
        if self._suggestion_scroll_top < 0:
            self._suggestion_scroll_top = 0
        if self._suggestion_scroll_top > max_top:
            self._suggestion_scroll_top = max_top

        if selected_index < self._suggestion_scroll_top:
            self._suggestion_scroll_top = selected_index
        elif selected_index >= self._suggestion_scroll_top + viewport_size:
            self._suggestion_scroll_top = selected_index - viewport_size + 1

        if self._suggestion_scroll_top < 0:
            self._suggestion_scroll_top = 0
        if self._suggestion_scroll_top > max_top:
            self._suggestion_scroll_top = max_top
    
    def get_text(self) -> str:
        """Get the current text from the editor."""
        return self.editor.editor.get_text().strip()
    
    def clear_text(self) -> None:
        """Clear the editor text."""
        self.editor.editor.set_text("")
        self._scroll_top = 0
        self._suggestion_scroll_top = 0
    
    def increment_message_count(self) -> None:
        """Increment the message counter."""
        self.message_count += 1
    
    def _resolve_box(self):
        b = self.style.box
        if isinstance(b, str):
            name = b.strip().upper()
            return getattr(rich_box, name, rich_box.SQUARE)
        return b or rich_box.SQUARE

    def render(self) -> Panel:
        """Render the input panel."""
        height = self.calculate_height()
        # Rich's Panel(height=...) includes the border lines. The renderable
        # content sits inside those borders.
        inner_height = max(1, int(height) - 2)

        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col

        # Check autocomplete popup state
        try:
            ed = self.editor.editor
            comp_active = bool(getattr(ed, "_completion_active", False))
            comp_items = list(getattr(ed, "_completion_items", []) or [])
            comp_index = int(getattr(ed, "_completion_index", 0))
        except Exception:
            comp_active, comp_items, comp_index = False, [], 0

        # We'll build rows (without trailing newlines) to keep height accounting exact.
        rows: List[Text] = []

        # Optional header at top
        if self.style.show_header:
            rows.append(Text(f" {self.title} ", style=self.style.header_style))
            rows.append(Text(self.style.header_separator_char * 70, style=self.style.header_separator_style))

        header_lines = len(rows)

        # Layout budgeting (within the inner content region)
        status_lines = 1

        # Header + editor + suggestions + status must fit in inner_height.
        available_body = inner_height - header_lines - status_lines
        available_body = max(1, available_body)

        # Allocate lines to suggestions (if active) but never starve editor below 1 line.
        # We allow suggestions to use *all* remaining space because the editor
        # itself has its own scrolling.
        min_editor_lines = 1
        suggestion_lines = 0
        if comp_active and comp_items and available_body > min_editor_lines:
            suggestion_lines = min(1 + len(comp_items), available_body - min_editor_lines)
        editor_lines_budget = max(min_editor_lines, available_body - suggestion_lines)

        # -------- Main editor viewport (scrolling) --------
        self._ensure_cursor_visible(viewport_size=editor_lines_budget)
        viewport_start = self._scroll_top
        viewport_end = min(len(lines), viewport_start + editor_lines_budget)

        for i in range(viewport_start, viewport_end):
            line_num = f"{i+1:2d}: "
            row = Text()
            if i == cursor_row:
                row.append(line_num, style=self.style.current_line_num_style)
                line_content = lines[i]
                before_cursor = line_content[:cursor_col]
                cursor_char = line_content[cursor_col:cursor_col+1] if cursor_col < len(line_content) else "█"
                after_cursor = line_content[cursor_col+1:] if cursor_col < len(line_content) else ""
                row.append(before_cursor)
                row.append(cursor_char, style=self.style.cursor_style)
                row.append(after_cursor)
            else:
                row.append(line_num, style=self.style.line_num_style)
                row.append(lines[i])
            rows.append(row)

        # Fill remaining editor viewport space (editor area only)
        lines_shown = viewport_end - viewport_start
        for _ in range(max(0, editor_lines_budget - lines_shown)):
            rows.append(Text(""))

        # -------- Suggestions popup (scrolling) --------
        if comp_active and comp_items and suggestion_lines >= 2:
            # One line is the title; remaining are items.
            items_viewport = suggestion_lines - 1
            total_items = len(comp_items)

            self._ensure_suggestion_visible(
                viewport_size=items_viewport,
                selected_index=comp_index,
                total_items=total_items,
            )

            rows.append(Text("Suggestions:", style="bold cyan"))
            start = self._suggestion_scroll_top
            end = min(total_items, start + items_viewport)

            for idx in range(start, end):
                item = comp_items[idx]
                disp = item.get("display", str(item)) if isinstance(item, dict) else str(item)
                marker = "> " if idx == comp_index else "  "
                style = "black on white" if idx == comp_index else ""
                rows.append(Text(f"{marker}{disp}", style=style))

            # Fill remaining suggestion viewport space
            for _ in range(max(0, items_viewport - (end - start))):
                rows.append(Text(""))
        else:
            # If suggestions are active but we couldn't allocate enough space to
            # render them, reset suggestion scroll state so the next render doesn't
            # try to reuse an out-of-range offset.
            self._suggestion_scroll_top = 0

        # -------- Status line --------
        total_lines = len(lines)
        showing_range = (
            f"{viewport_start + 1}-{viewport_end}/{total_lines}" if total_lines > editor_lines_budget else f"{total_lines}"
        )
        rows.append(
            Text(
                f"📝 Lines: {showing_range} | Messages sent: {self.message_count} | Tab: suggest/accept | ↑/↓: navigate | Esc: cancel | Ctrl+D to send",
                style=self.style.status_style,
            )
        )

        # Final assembly: exactly inner_height rows.
        if len(rows) < inner_height:
            rows.extend(Text("") for _ in range(inner_height - len(rows)))
        elif len(rows) > inner_height:
            # Prefer keeping the status line (last). Trim from the middle.
            tail = rows[-1:]
            rows = rows[: max(0, inner_height - 1)] + tail

        editor_content = Text()
        for i, row in enumerate(rows):
            editor_content.append(row)
            if i != len(rows) - 1:
                editor_content.append("\n")

        panel_title = f"[{self.style.title_style}]{self.title}[/{self.style.title_style}]" if self.title else None
        return Panel(
            editor_content,
            title=panel_title,
            title_align=self.style.title_align,
            border_style=self.style.border_style,
            box=self._resolve_box(),
            height=height
        )


# Inline-friendly layout helpers (don't claim full-screen like Layout)
class HStack:
    """Horizontal stack of renderables using Columns.

    Children may be OutputPanel/InputPanel instances (with a .render() method)
    or any Rich renderable. The resulting row height equals the max height of
    the children, so it plays well in a Live inline region.
    """
    def __init__(self, children, *, equal: bool = True, expand: bool = True, gap: int = 1):
        self.children = list(children)
        self.equal = equal
        self.expand = expand
        self.gap = gap

    def render(self):
        items = []
        for child in self.children:
            if hasattr(child, "render") and callable(getattr(child, "render")):
                items.append(child.render())
            else:
                items.append(child)
        # padding=(left_right_gap_left, left_right_gap_right) or single int
        return Columns(items, equal=self.equal, expand=self.expand, padding=self.gap)


class VStack:
    """Vertical stack using Group.

    Children may be OutputPanel/InputPanel instances or Rich renderables.
    """
    def __init__(self, children):
        self.children = list(children)

    def render(self):
        items = []
        for child in self.children:
            if hasattr(child, "render") and callable(getattr(child, "render")):
                items.append(child.render())
            else:
                items.append(child)
        return Group(*items)


def demo():
    """Demo function to showcase the text editor."""
    
    def autocomplete_demo(line: str, row: int, col: int) -> List[str]:
        """Simple autocomplete demo."""
        words = ["def", "class", "import", "from", "if", "else", "for", "while"]
        current_word = line[:col].split()[-1] if line[:col].split() else ""
        return [w for w in words if w.startswith(current_word)]
    
    editor = TextEditor(
        initial_text="Hello, World!\nThis is a multi-line\ntext editor demo.",
        autocomplete_callback=autocomplete_demo
    )
    
    # Add some event listeners
    def on_text_change(change_type, row, col, data):
        print(f"Text changed: {change_type} at ({row}, {col})")
    
    def on_cursor_move(old_row, old_col, new_row, new_col):
        print(f"Cursor moved: ({old_row}, {old_col}) -> ({new_row}, {new_col})")
    
    editor.add_event_listener('text_change', on_text_change)
    editor.add_event_listener('cursor_move', on_cursor_move)
    
    print("Starting text editor demo...")
    print("Press Ctrl+C to exit")
    print("Try typing, using arrow keys, and Tab for autocomplete")
    
    editor.run()
    
    print(f"\nFinal text:\n{editor.get_text()}")


if __name__ == "__main__":
    demo()
