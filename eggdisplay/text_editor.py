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
from rich.live import Live
from rich.text import Text
from rich.console import Console
from rich.layout import Layout
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
        if self.autocomplete_callback:
            current_line = self.lines[self.cursor.row]
            completions = self.autocomplete_callback(current_line, self.cursor.row, self.cursor.col)
            if completions:
                # Use the first completion for now
                completion = completions[0]
                self.insert_text(completion)
                self._trigger_event('autocomplete', completion, self.cursor.row, self.cursor.col)
                return True
        return False
    
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
        text.append("Ctrl+C to exit | Tab for autocomplete | Arrows to navigate", style="dim")
        
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
                            elif next_chars == '[3':  # Delete
                                sys.stdin.read(1)  # Read the ~
                                self.handle_key('delete')
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


class RealTimeEditor:
    """
    Real-time interactive text editor with Live display.
    
    Provides Vim-like insert mode experience with real-time typing.
    Uses threading and readchar for non-blocking input.
    """
    
    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24):
        self.editor = TextEditor(initial_text=initial_text, width=width, height=height)
        self.console = Console()
        self.input_queue = queue.Queue()
        self.running = False
    
    def _input_worker(self):
        """Worker that reads single characters using readchar."""
        while self.running:
            try:
                # Use readkey instead of readchar for better escape sequence handling
                key = readchar.readkey()
                self.input_queue.put(key)
                
                if key == readchar.key.CTRL_C:
                    break
                    
            except KeyboardInterrupt:
                self.input_queue.put(readchar.key.CTRL_C)
                break
            except Exception:
                break
    
    def _render(self) -> Text:
        """Render the editor display."""
        text = Text()
        
        # Header
        text.append(" Real-time Text Editor ", style="bold white on blue")
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
                # Current line with cursor
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
    
    def _handle_key(self, key: str) -> bool:
        """Handle a key press and return False if should quit."""
        # Debug: uncomment to see what keys are being received
        # print(f"DEBUG: Key received: {repr(key)}")
        
        if key == readchar.key.CTRL_C:
            return False
        elif key == readchar.key.UP:
            self.editor.handle_key('up')
        elif key == readchar.key.DOWN:
            self.editor.handle_key('down')
        elif key == readchar.key.LEFT:
            self.editor.handle_key('left')
        elif key == readchar.key.RIGHT:
            self.editor.handle_key('right')
        elif key in (readchar.key.BACKSPACE, '\x7f', '\x08'):
            self.editor.handle_key('backspace')
        elif key == readchar.key.DELETE:
            self.editor.handle_key('delete')
        elif key in (readchar.key.ENTER, '\r', '\n'):
            self.editor.handle_key('enter')
        elif key == readchar.key.TAB:
            self.editor.handle_key('tab')
        elif len(key) == 1 and key.isprintable():
            # Regular character
            self.editor.handle_key(key)
        elif key.startswith('\x1b'):
            # Handle escape sequences that might not be caught by readchar
            if key == '\x1b[A':
                self.editor.handle_key('up')
            elif key == '\x1b[B':
                self.editor.handle_key('down')
            elif key == '\x1b[C':
                self.editor.handle_key('right')
            elif key == '\x1b[D':
                self.editor.handle_key('left')
            elif key == '\x1b[3~':
                self.editor.handle_key('delete')
            else:
                # Unknown escape sequence - ignore it
                pass
        
        return True
    
    def run(self, *, inline: bool = True):
        """Run the real-time editor.
        
        Args:
            inline: When True (default), render inline (screen=False) so the
                    terminal remains scrollable and other prints can appear
                    above the live region. When False, use alternate screen.
        """
        self.running = True
        
        # Start input thread
        input_thread = threading.Thread(target=self._input_worker, daemon=True)
        input_thread.start()
        
        self.console.print("[green]Real-time Editor Started[/green]")
        self.console.print("[dim]Start typing! Your keystrokes appear immediately. Ctrl+C to exit.[/dim]\n")
        
        try:
            with Live(self._render(), refresh_per_second=30, screen=not inline, console=self.console) as live:
                while self.running:
                    # Process any pending input
                    try:
                        while True:
                            key = self.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                break
                    except queue.Empty:
                        pass
                    
                    # Update display
                    live.update(self._render())
                    
                    # Small delay
                    time.sleep(0.01)
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            
            # Show result without clearing the screen in inline mode, so
            # the scrollback remains intact. For full-screen mode you may
            # still want to clear.
            if not inline:
                self.console.clear()
            self.console.print("[green]Editor session ended.[/green]")
            self.console.print("\n[bold]Your text:[/bold]")
            self.console.print("─" * 60)
            self.console.print(self.editor.get_text())
            self.console.print("─" * 60)


class AsyncRealTimeEditor:
    """
    Async real-time interactive text editor with Live display.
    
    Uses asyncio for cleaner concurrency compared to threading.
    """
    
    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24):
        self.editor = TextEditor(initial_text=initial_text, width=width, height=height)
        self.console = Console()
        self.running = False
        self.input_queue = asyncio.Queue()
    
    async def _input_reader(self):
        """Async task that reads keyboard input."""
        while self.running:
            try:
                # Use asyncio.to_thread to run blocking readkey in a thread
                key = await asyncio.to_thread(readchar.readkey)
                await self.input_queue.put(key)
                
                if key == readchar.key.CTRL_C:
                    break
                    
            except (KeyboardInterrupt, Exception):
                break
    
    def _render(self) -> Text:
        """Render the editor display."""
        text = Text()
        
        # Header
        text.append(" Async Real-time Editor ", style="bold white on blue")
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
                # Current line with cursor
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
    
    def _handle_key(self, key: str) -> bool:
        """Handle a key press and return False if should quit."""
        # Debug: uncomment to see what keys are being received
        # print(f"DEBUG: Key received: {repr(key)}")
        
        if key == readchar.key.CTRL_C:
            return False
        elif key == readchar.key.UP:
            self.editor.handle_key('up')
        elif key == readchar.key.DOWN:
            self.editor.handle_key('down')
        elif key == readchar.key.LEFT:
            self.editor.handle_key('left')
        elif key == readchar.key.RIGHT:
            self.editor.handle_key('right')
        elif key in (readchar.key.BACKSPACE, '\x7f', '\x08'):
            self.editor.handle_key('backspace')
        elif key == readchar.key.DELETE:
            self.editor.handle_key('delete')
        elif key in (readchar.key.ENTER, '\r', '\n'):
            self.editor.handle_key('enter')
        elif key == readchar.key.TAB:
            self.editor.handle_key('tab')
        elif len(key) == 1 and key.isprintable():
            # Regular character
            self.editor.handle_key(key)
        elif key.startswith('\x1b'):
            # Handle escape sequences that might not be caught by readchar
            if key == '\x1b[A':
                self.editor.handle_key('up')
            elif key == '\x1b[B':
                self.editor.handle_key('down')
            elif key == '\x1b[C':
                self.editor.handle_key('right')
            elif key == '\x1b[D':
                self.editor.handle_key('left')
            elif key == '\x1b[3~':
                self.editor.handle_key('delete')
            else:
                # Unknown escape sequence - ignore it
                pass
        
        return True
    
    async def run_async(self, *, inline: bool = True):
        """Run the editor using asyncio.
        
        Args:
            inline: When True (default), render inline (screen=False).
        """
        self.running = True
        
        self.console.print("[green]Async Real-time Editor Started[/green]")
        self.console.print("[dim]Start typing! Your keystrokes appear immediately. Ctrl+C to exit.[/dim]\n")
        
        try:
            with Live(self._render(), refresh_per_second=30, screen=not inline, console=self.console) as live:
                # Start input reader task
                input_task = asyncio.create_task(self._input_reader())
                
                while self.running:
                    # Wait for input with timeout
                    try:
                        key = await asyncio.wait_for(self.input_queue.get(), timeout=0.01)
                        if not self._handle_key(key):
                            self.running = False
                            break
                    except asyncio.TimeoutError:
                        # No input, just continue
                        pass
                    
                    # Update display
                    live.update(self._render())
                
                # Cancel input task
                input_task.cancel()
                try:
                    await input_task
                except asyncio.CancelledError:
                    pass
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            
            # Show final result without clearing in inline mode.
            if not inline:
                self.console.clear()
            self.console.print("[green]Editor session ended.[/green]")
            self.console.print("\n[bold]Your text:[/bold]")
            self.console.print("─" * 60)
            self.console.print(self.editor.get_text())
            self.console.print("─" * 60)
    
    def run(self):
        """Synchronous wrapper for running the async editor."""
        asyncio.run(self.run_async())


class OutputPanel:
    """
    Generic output panel for displaying multi-line strings.
    
    This panel is completely agnostic about the content - it just displays
    multi-line text with automatic height adjustment.
    """
    
    def __init__(self, title: str = "Output", initial_height: int = 8, max_height: int = 20):
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
    
    def render(self) -> Panel:
        """Render the panel, showing only the last lines that fit."""
        height = self.calculate_height()
        
        # Create content text
        content_text = Text()
        
        # Add header with stats
        content_lines = self.content.count('\n') + 1 if self.content else 0
        content_text.append(f" {self.title} ", style="bold white on blue")
        content_text.append(f" | Lines: {content_lines}")
        content_text.append(f" | Height: {height}")
        content_text.append("\n")
        content_text.append("─" * 70 + "\n", style="blue")
        
        # Calculate available lines for content (after header)
        available_content_lines = max(1, height - 4)  # Reserve space for header and padding
        
        # Show only the last lines that fit
        if self.content:
            all_lines = self.content.split('\n')
            
            if len(all_lines) <= available_content_lines:
                # All lines fit, show everything
                for line in all_lines:
                    content_text.append(line + "\n")
            else:
                # Show only the last 'available_content_lines' lines
                start_index = len(all_lines) - available_content_lines
                for i in range(start_index, len(all_lines)):
                    content_text.append(all_lines[i] + "\n")
                
                # Show scroll indicator
                hidden_lines = len(all_lines) - available_content_lines
                content_text.append(f"[dim]... {hidden_lines} lines above[/dim]")
        else:
            content_text.append("[dim]No content[/dim]")
        
        return Panel(
            content_text,
            title=f"[bold]{self.title}[/bold]",
            border_style="blue",
            height=height
        )


class InputPanel:
    """
    Generic input panel with text editing capabilities.
    
    This panel provides a text editor interface for user input.
    """
    
    def __init__(self, title: str = "Input", initial_height: int = 8, max_height: int = 12):
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
        self.editor = RealTimeEditor(
            initial_text="",
            width=80,
            height=6
        )
        self.message_count = 0
        
    def calculate_height(self) -> int:
        """Calculate the optimal panel height based on editor content."""
        editor_lines = len(self.editor.editor.lines)
        target_height = max(6, min(self.max_height, editor_lines + 4))
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
    
    def render(self) -> Panel:
        """Render the input panel."""
        height = self.calculate_height()
        
        editor_content = Text()
        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col
        
        # Panel height includes content + status line
        available_content_lines = height - 1
        
        # Calculate viewport
        viewport_size = available_content_lines
        viewport_start = max(0, cursor_row - viewport_size // 2)
        viewport_end = min(len(lines), viewport_start + viewport_size)
        
        if viewport_end - viewport_start < viewport_size:
            viewport_start = max(0, viewport_end - viewport_size)
        
        # Show lines in viewport
        for i in range(viewport_start, viewport_end):
            if i < len(lines):
                line_num = f"{i+1:2d}: "
                
                if i == cursor_row:
                    editor_content.append(line_num, style="bold green")
                    line_content = lines[i]
                    
                    before_cursor = line_content[:cursor_col]
                    cursor_char = line_content[cursor_col:cursor_col+1] if cursor_col < len(line_content) else "█"
                    after_cursor = line_content[cursor_col+1:] if cursor_col < len(line_content) else ""
                    
                    editor_content.append(before_cursor)
                    editor_content.append(cursor_char, style="black on white")
                    editor_content.append(after_cursor)
                else:
                    editor_content.append(line_num, style="dim")
                    editor_content.append(lines[i])
                
                editor_content.append("\n")
        
        # Fill empty space
        lines_shown = viewport_end - viewport_start
        empty_lines_needed = int(available_content_lines - lines_shown)
        for _ in range(empty_lines_needed):
            editor_content.append("\n")
        
        # Status line
        total_lines = len(lines)
        showing_range = f"{viewport_start + 1}-{viewport_end}/{total_lines}" if total_lines > available_content_lines else f"{total_lines}"
        
        editor_content.append(f"[dim]📝 Lines: {showing_range} | Messages sent: {self.message_count} | Ctrl+D to send[/dim]")
        
        return Panel(
            editor_content,
            title=f"[bold]{self.title}[/bold]",
            border_style="green",
            height=height
        )


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
