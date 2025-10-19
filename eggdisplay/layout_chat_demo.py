#!/usr/bin/env python3
"""
Rich Layout Chat Demo
Top panel: Live updating display area
Bottom panel: Real-time editor that sends text to top on Ctrl+D
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
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text


try:
    import readchar
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "readchar"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import readchar


class LayoutChatEditor:
    """Layout-based editor with chat functionality."""
    
    def __init__(self):
        self.console = Console()
        self.input_queue = queue.Queue()
        self.running = False
        
        # Create editor for bottom panel
        self.editor = TextEditor(
            initial_text="Type your message here...\nPress Ctrl+D to send, Ctrl+C to exit\n",
            width=80,
            height=8
        )
        
        # Chat history for top panel
        self.chat_history = [
            "Welcome to the Rich Layout Chat Demo!",
            "────────────────────────────────────────",
            "Type in the bottom editor and press Ctrl+D to send messages.",
            ""
        ]
        
        # Live data for top panel
        self.live_data = {
            "message_count": 0,
            "last_update": time.strftime("%H:%M:%S"),
            "active_users": 1
        }
    
    def _input_thread(self):
        """Thread for handling keyboard input."""
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
    
    def _render_top_panel(self) -> Panel:
        """Render the top panel with chat history and live data."""
        # Create content for top panel
        content = Text()
        
        # Header with live data
        content.append(" 💬 Chat History ", style="bold white on blue")
        content.append(f" | Messages: {self.live_data['message_count']}")
        content.append(f" | Active: {self.live_data['active_users']}")
        content.append(f" | Last: {self.live_data['last_update']}")
        content.append("\n")
        content.append("─" * 70 + "\n", style="blue")
        
        # Chat messages
        for message in self.chat_history[-10:]:  # Show last 10 messages
            if message.startswith("─"):
                content.append(message + "\n", style="dim")
            elif message:
                content.append("• " + message + "\n")
            else:
                content.append("\n")
        
        # If no messages yet
        if len(self.chat_history) <= 3:
            content.append("\n[dim]No messages yet. Send your first message below![/dim]")
        
        return Panel(
            content,
            title="[bold]Live Chat Panel[/bold]",
            border_style="blue"
        )
    
    def _render_bottom_panel(self) -> Panel:
        """Render the bottom panel with the editor."""
        # Create editor content with cursor
        editor_content = Text()
        lines = self.editor.lines
        cursor_row, cursor_col = self.editor.cursor.row, self.editor.cursor.col
        
        for i, line in enumerate(lines):
            if i == cursor_row:
                # Current line with cursor
                before_cursor = line[:cursor_col]
                cursor_char = line[cursor_col:cursor_col+1] if cursor_col < len(line) else "█"
                after_cursor = line[cursor_col+1:] if cursor_col < len(line) else ""
                
                editor_content.append(before_cursor)
                editor_content.append(cursor_char, style="black on white")
                editor_content.append(after_cursor)
            else:
                editor_content.append(line)
            
            if i < len(lines) - 1:
                editor_content.append("\n")
        
        # If empty, show cursor
        if not lines or (len(lines) == 1 and not lines[0]):
            editor_content.append("█", style="black on white")
        
        return Panel(
            editor_content,
            title="[bold]Message Editor[/bold]",
            subtitle="[dim]Ctrl+D: Send | Ctrl+C: Exit[/dim]",
            border_style="green"
        )
    
    def _render_layout(self) -> Layout:
        """Render the complete layout."""
        layout = Layout()
        
        # Split into top (chat) and bottom (editor)
        layout.split_column(
            Layout(self._render_top_panel(), name="top", ratio=2),
            Layout(self._render_bottom_panel(), name="bottom")
        )
        
        return layout
    
    def _handle_key(self, key: str) -> bool:
        """Handle keyboard input."""
        if key == readchar.key.CTRL_C:
            return False
        elif key == readchar.key.CTRL_D:
            # Send message to chat
            message = self.editor.get_text().strip()
            if message and message != "Type your message here...":
                self.chat_history.append(f"[{time.strftime('%H:%M:%S')}] {message}")
                self.live_data["message_count"] += 1
                self.live_data["last_update"] = time.strftime("%H:%M:%S")
                
                # Clear editor
                self.editor.set_text("")
            return True
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
            self.editor.handle_key(key)
        
        return True
    
    def run(self):
        """Run the layout chat demo."""
        self.running = True
        
        # Start input thread
        input_thread = threading.Thread(target=self._input_thread, daemon=True)
        input_thread.start()
        
        self.console.print("[bold green]Rich Layout Chat Demo[/bold green]")
        self.console.print("[dim]Starting interactive layout with real-time editor...[/dim]\n")
        
        try:
            with Live(self._render_layout(), refresh_per_second=30, screen=True) as live:
                while self.running:
                    # Process keyboard input
                    try:
                        while True:
                            key = self.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                break
                    except queue.Empty:
                        pass
                    
                    # Update live data periodically
                    if int(time.time()) % 5 == 0:  # Update every 5 seconds
                        self.live_data["active_users"] = 1 + (int(time.time()) % 3)
                    
                    # Update display
                    live.update(self._render_layout())
                    
                    time.sleep(0.01)
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            
            # Show final state
            self.console.clear()
            self.console.print("[green]Chat session ended.[/green]")
            self.console.print(f"\nTotal messages sent: {self.live_data['message_count']}")


def main():
    """Main function."""
    
    console = Console()
    
    console.print("[bold blue]🎯 Rich Layout Chat Demo[/bold blue]")
    console.print("=" * 60)
    console.print("\nThis demo showcases:")
    console.print("  • Rich Layout with multiple panels")
    console.print("  • Live updating top panel with chat history")
    console.print("  • Real-time editor in bottom panel")
    console.print("  • Interactive chat functionality")
    console.print("\n🎮 Controls:")
    console.print("  • Type in the bottom editor")
    console.print("  • Press Ctrl+D to send message to top panel")
    console.print("  • Press Ctrl+C to exit")
    console.print("\n🚀 Starting demo...\n")
    
    # Create and run the layout editor
    editor = LayoutChatEditor()
    editor.run()


if __name__ == "__main__":
    main()