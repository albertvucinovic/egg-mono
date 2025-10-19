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
    
    def run(self) -> None:
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
            
            with Live(initial_render, refresh_per_second=10, screen=True) as live:
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
                char = readchar.readchar()
                self.input_queue.put(char)
                
                if char == readchar.key.CTRL_C:
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
        
        return True
    
    def run(self):
        """Run the real-time editor."""
        self.running = True
        
        # Start input thread
        input_thread = threading.Thread(target=self._input_worker, daemon=True)
        input_thread.start()
        
        self.console.print("[green]Real-time Editor Started[/green]")
        self.console.print("[dim]Start typing! Your keystrokes appear immediately. Ctrl+C to exit.[/dim]\n")
        
        try:
            with Live(self._render(), refresh_per_second=30, screen=True) as live:
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
            
            # Clear screen and show result
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
                # Use asyncio.to_thread to run blocking readchar in a thread
                key = await asyncio.to_thread(readchar.readchar)
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
        
        return True
    
    async def run_async(self):
        """Run the editor using asyncio."""
        self.running = True
        
        self.console.print("[green]Async Real-time Editor Started[/green]")
        self.console.print("[dim]Start typing! Your keystrokes appear immediately. Ctrl+C to exit.[/dim]\n")
        
        try:
            with Live(self._render(), refresh_per_second=30, screen=True) as live:
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
            
            # Show final result
            self.console.clear()
            self.console.print("[green]Editor session ended.[/green]")
            self.console.print("\n[bold]Your text:[/bold]")
            self.console.print("─" * 60)
            self.console.print(self.editor.get_text())
            self.console.print("─" * 60)
    
    def run(self):
        """Synchronous wrapper for running the async editor."""
        asyncio.run(self.run_async())


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
