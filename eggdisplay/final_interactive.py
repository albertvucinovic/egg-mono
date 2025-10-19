#!/usr/bin/env python3
"""
Final Interactive Text Editor
Combines threading + readchar for true real-time typing experience.
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
except ImportError:
    print("Installing readchar...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "readchar"])
    import readchar


class FinalInteractiveEditor:
    """Final interactive editor with real-time typing."""
    
    def __init__(self, initial_text: str = "", width: int = 80, height: int = 24):
        self.editor = TextEditor(initial_text=initial_text, width=width, height=height)
        self.console = Console()
        self.input_queue = queue.Queue()
        self.running = False
    
    def _input_thread(self):
        """Thread that captures keystrokes in real-time."""
        while self.running:
            try:
                key = readchar.readkey()
                self.input_queue.put(key)
                
                if key == readchar.key.CTRL_C:
                    break
                    
            except KeyboardInterrupt:
                self.input_queue.put(readchar.key.CTRL_C)
                break
            except Exception:
                break
    
    def _render_editor(self) -> Text:
        """Render the editor with beautiful formatting."""
        text = Text()
        
        # Header with instructions
        text.append(" 🖊️  Interactive Text Editor ", style="bold white on blue")
        text.append("\n")
        text.append("─" * 60 + "\n", style="blue")
        
        # Editor content
        lines = self.editor.lines
        cursor_row, cursor_col = self.editor.cursor.row, self.editor.cursor.col
        
        # Show context around cursor
        start_line = max(0, cursor_row - 6)
        end_line = min(len(lines), start_line + 12)
        
        for i in range(start_line, end_line):
            line_num = f"{i+1:3d} │ "
            
            if i == cursor_row:
                # Current line with blinking cursor effect
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
        
        # Fill empty space if needed
        if len(lines) < end_line - start_line:
            for _ in range(end_line - start_line - len(lines)):
                text.append("     │ \n")
        
        # Footer with status
        text.append("─" * 60 + "\n", style="blue")
        text.append(f" 📍 Line {cursor_row + 1}, Column {cursor_col + 1}")
        text.append(" | ")
        text.append(" 💡 Type directly! Ctrl+C to exit", style="yellow")
        
        return text
    
    def _process_keystroke(self, key: str) -> bool:
        """Process a keystroke and return False if should exit."""
        # Exit conditions
        if key in (readchar.key.CTRL_C, '\x03'):
            return False
        
        # Navigation
        elif key == readchar.key.UP:
            self.editor.handle_key('up')
        elif key == readchar.key.DOWN:
            self.editor.handle_key('down')
        elif key == readchar.key.LEFT:
            self.editor.handle_key('left')
        elif key == readchar.key.RIGHT:
            self.editor.handle_key('right')
        elif key == readchar.key.HOME:
            self.editor.cursor.col = 0
        elif key == readchar.key.END:
            self.editor.cursor.col = len(self.editor.lines[self.editor.cursor.row])
        
        # Editing
        elif key in (readchar.key.BACKSPACE, '\x7f', '\x08'):
            self.editor.handle_key('backspace')
        elif key == readchar.key.DELETE:
            self.editor.handle_key('delete')
        elif key in (readchar.key.ENTER, '\r', '\n'):
            self.editor.handle_key('enter')
        elif key == readchar.key.TAB:
            self.editor.handle_key('tab')
        
        # Regular characters
        elif len(key) == 1 and key.isprintable():
            self.editor.handle_key(key)
        
        return True
    
    def run(self):
        """Run the interactive editor."""
        self.running = True
        
        # Clear screen and show welcome
        self.console.clear()
        self.console.print("[bold green]🎯 Final Interactive Text Editor[/bold green]")
        self.console.print("[dim]Your keystrokes appear in real-time below...[/dim]\n")
        
        # Start input thread
        input_thread = threading.Thread(target=self._input_thread, daemon=True)
        input_thread.start()
        
        try:
            # Use Live for real-time updates
            with Live(self._render_editor(), refresh_per_second=30, screen=True) as live:
                while self.running:
                    # Process all pending keystrokes
                    processed_any = False
                    try:
                        while True:
                            key = self.input_queue.get_nowait()
                            if not self._process_keystroke(key):
                                self.running = False
                                break
                            processed_any = True
                    except queue.Empty:
                        pass
                    
                    # Update display (always, for cursor blinking effect)
                    live.update(self._render_editor())
                    
                    # Small delay to prevent CPU spinning
                    time.sleep(0.01)
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            
            # Show final result
            self.console.clear()
            self.console.print("[green]✅ Editor session completed[/green]")
            self.console.print("\n[bold]Your final text:[/bold]")
            self.console.print("─" * 60)
            final_text = self.editor.get_text()
            if final_text:
                self.console.print(final_text)
            else:
                self.console.print("[dim](No text entered)[/dim]")
            self.console.print("─" * 60)


def main():
    """Main function with comprehensive setup."""
    
    console = Console()
    
    console.print("[bold blue]🎯 FINAL INTERACTIVE TEXT EDITOR[/bold blue]")
    console.print("=" * 60)
    console.print("\nThis is the REAL DEAL - true Vim-like insert mode!")
    console.print("\n✨ Features:")
    console.print("  • Real-time typing - characters appear as you type")
    console.print("  • Arrow key navigation")
    console.print("  • Backspace, Delete, Enter, Tab")
    console.print("  • Beautiful Live display with cursor")
    console.print("  • No menus, no commands - just pure typing")
    console.print("\n🎮 How to use:")
    console.print("  1. Just start typing - your text appears above")
    console.print("  2. Use arrow keys to move around")
    console.print("  3. Backspace/Delete to remove text")
    console.print("  4. Enter for new lines")
    console.print("  5. Ctrl+C to exit")
    console.print("\n🚀 Ready? Your editor appears below...\n")
    
    # Wait for user to be ready
    input("Press Enter to start the editor...")
    
    # Create and run editor
    editor = FinalInteractiveEditor(
        initial_text="",
        width=70,
        height=20
    )
    
    # Optional: Add autocomplete
    def simple_autocomplete(line: str, row: int, col: int) -> list:
        words = ["hello", "world", "python", "editor", "text", "rich"]
        current_word = ""
        for char in reversed(line[:col]):
            if char.isalnum():
                current_word = char + current_word
            else:
                break
        return [w for w in words if w.startswith(current_word)]
    
    editor.editor.autocomplete_callback = simple_autocomplete
    
    # Run the editor
    editor.run()


if __name__ == "__main__":
    main()