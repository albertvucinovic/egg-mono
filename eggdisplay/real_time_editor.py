#!/usr/bin/env python3
"""
Real-time Interactive Text Editor
Uses readchar for proper single-character input without blocking.
"""

import sys
import os
import threading
import queue
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import TextEditor
from rich.console import Console
from rich.live import Live
from rich.text import Text


try:
    import readchar
    HAS_READCHAR = True
except ImportError:
    HAS_READCHAR = False
    print("Installing readchar for better input handling...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "readchar"])
    import readchar


class RealTimeEditor:
    """Real-time editor with proper character-by-character input."""
    
    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24):
        self.editor = TextEditor(initial_text=initial_text, width=width, height=height)
        self.console = Console()
        self.input_queue = queue.Queue()
        self.running = False
    
    def _input_worker(self):
        """Worker that reads single characters using readchar."""
        while self.running:
            try:
                # Get single character without blocking
                char = readchar.readchar()
                self.input_queue.put(char)
                
                # Special handling for Ctrl+C
                if char == readchar.key.CTRL_C:
                    break
                    
            except KeyboardInterrupt:
                self.input_queue.put(readchar.key.CTRL_C)
                break
            except Exception as e:
                print(f"Input error: {e}")
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


def main():
    """Main function."""
    
    console = Console()
    
    console.print("[bold green]Real-time Interactive Text Editor[/bold green]")
    console.print("=" * 50)
    console.print("This editor shows your keystrokes in REAL-TIME!")
    console.print("\nFeatures:")
    console.print("  • Type directly - characters appear as you type")
    console.print("  • Arrow keys work for navigation")
    console.print("  • Backspace, Delete, Enter, Tab all work")
    console.print("  • Real-time cursor display")
    console.print("\nJust start typing! Press Ctrl+C to exit.\n")
    
    # Create editor
    editor = RealTimeEditor(
        initial_text="",
        width=70,
        height=20
    )
    
    # Add autocomplete
    def autocomplete(line: str, row: int, col: int) -> list:
        keywords = ["def", "class", "import", "from", "if", "else", "for", "while"]
        current_word = ""
        for char in reversed(line[:col]):
            if char.isalnum() or char == '_':
                current_word = char + current_word
            else:
                break
        return [kw for kw in keywords if kw.startswith(current_word)]
    
    editor.editor.autocomplete_callback = autocomplete
    
    # Run editor
    editor.run()


if __name__ == "__main__":
    main()