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
            
        current_line = self.lines[self.cursor.row]
        new_line = current_line[:self.cursor.col] + text + current_line[self.cursor.col:]
        self.lines[self.cursor.row] = new_line
        self.cursor.col += len(text)
        self._trigger_event('text_change', 'insert', self.cursor.row, self.cursor.col - len(text), text)
    
    def delete_char(self) -> None:
        """Delete character at cursor position."""
        current_line = self.lines[self.cursor.row]
        if self.cursor.col < len(current_line):
            deleted_char = current_line[self.cursor.col]
            new_line = current_line[:self.cursor.col] + current_line[self.cursor.col + 1:]
            self.lines[self.cursor.row] = new_line
            self._trigger_event('text_change', 'delete', self.cursor.row, self.cursor.col, deleted_char)
    
    def backspace(self) -> None:
        """Delete character before cursor position."""
        if self.cursor.col > 0:
            current_line = self.lines[self.cursor.row]
            deleted_char = current_line[self.cursor.col - 1]
            new_line = current_line[:self.cursor.col - 1] + current_line[self.cursor.col:]
            self.lines[self.cursor.row] = new_line
            self.cursor.col -= 1
            self._trigger_event('text_change', 'backspace', self.cursor.row, self.cursor.col, deleted_char)
        elif self.cursor.row > 0:
            # Merge with previous line
            prev_line = self.lines[self.cursor.row - 1]
            current_line = self.lines[self.cursor.row]
            self.lines[self.cursor.row - 1] = prev_line + current_line
            self.lines.pop(self.cursor.row)
            self.cursor.row -= 1
            self.cursor.col = len(prev_line)
            self._trigger_event('text_change', 'backspace_merge', self.cursor.row, self.cursor.col)
    
    def insert_newline(self) -> None:
        """Insert a newline at cursor position."""
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
            ins = completions[0].get("insert", "")
            if ins:
                self.insert_text(ins)
                self._trigger_event('autocomplete', ins, self.cursor.row, self.cursor.col)
            return True
        # If multiple, open selection UI
        if len(completions) > 1:
            self._completion_items = completions[:10]  # show top 10
            self._completion_index = 0
            self._completion_active = True
            # Fire autocomplete event with no insertion
            self._trigger_event('autocomplete', '', self.cursor.row, self.cursor.col)
            return True
        return False

    def accept_completion(self) -> bool:
        """Accept the currently highlighted completion (if active)."""
        if not (self._completion_active and self._completion_items):
            return False
        item = self._completion_items[self._completion_index]
        ins = item.get("insert", "") if isinstance(item, dict) else ""
        # Optional: number of characters to replace (delete) before cursor
        replace_n = 0
        if isinstance(item, dict):
            replace_n = int(item.get("replace", item.get("replace_chars", 0)) or 0)
        if replace_n > 0:
            # Delete replace_n characters before cursor position on the current line
            line = self.lines[self.cursor.row]
            n = min(replace_n, self.cursor.col)
            if n > 0:
                before = line[: self.cursor.col - n]
                after = line[self.cursor.col :]
                self.lines[self.cursor.row] = before + after
                self.cursor.col -= n
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
        while self.running:
            try:
                key = readchar.readkey()
                self.input_queue.put(key)
                if key == getattr(readchar.key, 'CTRL_C', '\x03'):
                    break
            except KeyboardInterrupt:
                self.input_queue.put(getattr(readchar.key, 'CTRL_C', '\x03'))
                break
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
        while self.running:
            try:
                key = await asyncio.to_thread(readchar.readkey)
                await self.input_queue.put(key)
                if key == getattr(readchar.key, 'CTRL_C', '\x03'):
                    break
            except (KeyboardInterrupt, Exception):
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
        """Calculate the optimal panel height based on content."""
        if not self.content:
            return self.current_height
            
        # Count lines in content
        content_lines = self.content.count('\n') + 1
        
        # Add space for header and padding
        total_lines_needed = content_lines + 3
        
        # Smooth animation towards target
        target_height = max(8, min(self.max_height, total_lines_needed))
        if self.current_height < target_height:
            self.current_height = min(target_height, self.current_height + 0.5)
        elif self.current_height > target_height:
            self.current_height = max(target_height, self.current_height - 0.5)
            
        return int(self.current_height)
    
    def _resolve_box(self):
        b = self.style.box
        # Allow users to pass a string name or a rich.box object
        if isinstance(b, str):
            name = b.strip().upper()
            return getattr(rich_box, name, rich_box.SQUARE)
        return b or rich_box.SQUARE

    def render(self) -> Panel:
        """Render the panel, showing only the last lines that fit."""
        height = self.calculate_height()
        
        # Create content text
        content_text = Text()
        
        # Add optional header with stats inside content area
        content_lines = self.content.count('\n') + 1 if self.content else 0
        header_lines = 0
        if self.style.show_header:
            content_text.append(f" {self.title} ", style=self.style.header_style)
            content_text.append(f" | Lines: {content_lines}")
            content_text.append(f" | Height: {height}")
            content_text.append("\n")
            sep = self.style.header_separator_char * 70
            content_text.append(sep + "\n", style=self.style.header_separator_style)
            header_lines = 2
        
        # Calculate available lines for content (after header)
        available_content_lines = max(1, height - (header_lines + 2))  # Reserve space for header and padding
        
        # Show only the last lines that fit
        if self.content:
            # Estimate available width per column to compute wrapped display lines
            term_cols = shutil.get_terminal_size(fallback=(100, 24)).columns
            # subtract a few chars for panel borders/padding
            approx_width = max(10, term_cols // self.columns_hint - 6)
            # Build display lines accounting for wrapping
            display_lines: List[str] = []
            for line in self.content.split('\n'):
                if line == "":
                    display_lines.append("")
                    continue
                wrapped = textwrap.wrap(
                    line,
                    width=approx_width,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                display_lines.extend(wrapped if wrapped else [""])
            # Now render only the tail that fits
            if len(display_lines) <= available_content_lines:
                for dl in display_lines:
                    content_text.append(dl + "\n")
            else:
                start_index = len(display_lines) - available_content_lines
                for dl in display_lines[start_index:]:
                    content_text.append(dl + "\n")
                hidden_lines = len(display_lines) - available_content_lines
                content_text.append(f"[dim]... {hidden_lines} lines above[/dim]")
        else:
            content_text.append("[dim]No content[/dim]")
        
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
        
    def calculate_height(self) -> int:
        """Calculate the optimal panel height based on editor content."""
        editor_lines = len(self.editor.editor.lines)
        # Account for autocomplete popup height when active
        extra = 0
        try:
            if getattr(self.editor.editor, "_completion_active", False):
                items = getattr(self.editor.editor, "_completion_items", []) or []
                if items:
                    extra = min(1 + len(items[:10]), 12)  # 1 title + up to 10 items
        except Exception:
            extra = 0
        target_height = max(6, min(self.max_height, editor_lines + 4 + extra))
        if self.current_height < target_height:
            self.current_height = min(target_height, self.current_height + 0.3)
        elif self.current_height > target_height:
            self.current_height = max(target_height, self.current_height - 0.3)
            
        return int(self.current_height)
    
    def get_text(self) -> str:
        """Get the current text from the editor."""
        return self.editor.editor.get_text().strip()
    
    def clear_text(self) -> None:
        """Clear the editor text."""
        self.editor.editor.set_text("")
    
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
        
        editor_content = Text()
        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col
        
        # Optional header at top
        header_lines = 0
        if self.style.show_header:
            editor_content.append(f" {self.title} ", style=self.style.header_style)
            editor_content.append("\n")
            sep = self.style.header_separator_char * 70
            editor_content.append(sep + "\n", style=self.style.header_separator_style)
            header_lines = 2

        # Check autocomplete popup and reserve space for it
        try:
            ed = self.editor.editor
            comp_active = bool(getattr(ed, "_completion_active", False))
            comp_items = list(getattr(ed, "_completion_items", []) or [])
            comp_index = int(getattr(ed, "_completion_index", 0))
        except Exception:
            comp_active, comp_items, comp_index = False, [], 0
        suggestion_lines = 0
        if comp_active and comp_items:
            suggestion_lines = 1 + min(10, len(comp_items))  # title + items

        # Panel height includes content + suggestions + status line (and optional header)
        available_content_lines = height - (1 + header_lines + suggestion_lines)
        
        # Calculate viewport
        viewport_size = max(1, available_content_lines)
        viewport_start = max(0, cursor_row - viewport_size // 2)
        viewport_end = min(len(lines), viewport_start + viewport_size)
        
        if viewport_end - viewport_start < viewport_size:
            viewport_start = max(0, viewport_end - viewport_size)
        
        # Show lines in viewport
        for i in range(viewport_start, viewport_end):
            if i < len(lines):
                line_num = f"{i+1:2d}: "
                
                if i == cursor_row:
                    editor_content.append(line_num, style=self.style.current_line_num_style)
                    line_content = lines[i]
                    
                    before_cursor = line_content[:cursor_col]
                    cursor_char = line_content[cursor_col:cursor_col+1] if cursor_col < len(line_content) else "█"
                    after_cursor = line_content[cursor_col+1:] if cursor_col < len(line_content) else ""
                    
                    editor_content.append(before_cursor)
                    editor_content.append(cursor_char, style=self.style.cursor_style)
                    editor_content.append(after_cursor)
                else:
                    editor_content.append(line_num, style=self.style.line_num_style)
                    editor_content.append(lines[i])
                
                editor_content.append("\n")
        
        # Fill empty space (leave room for suggestions block and status line)
        lines_shown = viewport_end - viewport_start
        empty_lines_needed = int(available_content_lines - lines_shown)
        for _ in range(empty_lines_needed):
            editor_content.append("\n")
        
        if comp_active and comp_items:
            editor_content.append("Suggestions:\n", style="bold cyan")
            for i, item in enumerate(comp_items[:10]):
                # Support dict form {display, insert}
                disp = item.get("display", str(item)) if isinstance(item, dict) else str(item)
                marker = "> " if i == comp_index else "  "
                style = "black on white" if i == comp_index else ""
                editor_content.append(f"{marker}{disp}\n", style=style)

        # Status line
        total_lines = len(lines)
        showing_range = f"{viewport_start + 1}-{viewport_end}/{total_lines}" if total_lines > available_content_lines else f"{total_lines}"
        
        editor_content.append(
            f"📝 Lines: {showing_range} | Messages sent: {self.message_count} | Tab: suggest/accept | ↑/↓: navigate | Esc: cancel | Ctrl+D to send",
            style=self.style.status_style
        )
        
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
