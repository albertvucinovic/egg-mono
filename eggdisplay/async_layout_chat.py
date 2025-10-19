#!/usr/bin/env python3
"""
Async Rich Layout Chat Demo
Uses asyncio for cleaner concurrency in the layout chat.
"""

import asyncio
import sys
import os
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


class AsyncLayoutChatEditor:
    """Async layout-based editor with chat functionality."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        self.input_queue = asyncio.Queue()
        
        # Create editor for bottom panel
        self.editor = TextEditor(
            initial_text="Type your message here...\nPress Ctrl+D to send, Ctrl+C to exit\n",
            width=80,
            height=8
        )
        
        # Chat history for top panel
        self.chat_history = [
            "Welcome to the Async Layout Chat Demo!",
            "────────────────────────────────────────",
            "This version uses asyncio for cleaner concurrency.",
            "Type in the bottom editor and press Ctrl+D to send messages.",
            ""
        ]
        
        # Live data for top panel
        self.live_data = {
            "message_count": 0,
            "last_update": time.strftime("%H:%M:%S"),
            "active_users": 1,
            "uptime": 0
        }
        
        # Async tasks
        self.tasks = []
    
    async def _input_reader(self):
        """Async task that reads keyboard input."""
        while self.running:
            try:
                key = await asyncio.to_thread(readchar.readchar)
                await self.input_queue.put(key)
                
                if key == readchar.key.CTRL_C:
                    break
                    
            except (KeyboardInterrupt, Exception):
                break
    
    async def _live_updater(self):
        """Async task that updates live data periodically."""
        start_time = time.time()
        while self.running:
            # Update uptime
            self.live_data["uptime"] = int(time.time() - start_time)
            
            # Simulate active users changing
            if int(time.time()) % 10 == 0:
                self.live_data["active_users"] = 1 + (int(time.time()) % 4)
            
            await asyncio.sleep(1)
    
    def _render_top_panel(self) -> Panel:
        """Render the top panel with chat history and live data."""
        content = Text()
        
        # Header with live data
        content.append(" 💬 Async Chat History ", style="bold white on blue")
        content.append(f" | Messages: {self.live_data['message_count']}")
        content.append(f" | Active: {self.live_data['active_users']}")
        content.append(f" | Uptime: {self.live_data['uptime']}s")
        content.append(f" | Last: {self.live_data['last_update']}")
        content.append("\n")
        content.append("─" * 70 + "\n", style="blue")
        
        # Chat messages
        for message in self.chat_history[-10:]:  # Show last 10 messages
            if message.startswith("─"):
                content.append(message + "\n", style="dim")
            elif message.startswith("["):
                # User message with timestamp
                content.append("💬 " + message + "\n")
            elif message:
                content.append("• " + message + "\n")
            else:
                content.append("\n")
        
        # If no messages yet
        if len([m for m in self.chat_history if m and not m.startswith("─")]) <= 2:
            content.append("\n[dim]No messages yet. Send your first message below![/dim]")
        
        return Panel(
            content,
            title="[bold]Live Async Chat Panel[/bold]",
            border_style="blue"
        )
    
    def _render_bottom_panel(self) -> Panel:
        """Render the bottom panel with the editor."""
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
            title="[bold]Async Message Editor[/bold]",
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
                timestamp = time.strftime('%H:%M:%S')
                self.chat_history.append(f"[{timestamp}] {message}")
                self.live_data["message_count"] += 1
                self.live_data["last_update"] = timestamp
                
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
    
    async def run_async(self):
        """Run the async layout chat."""
        self.running = True
        
        # Start async tasks
        input_task = asyncio.create_task(self._input_reader())
        live_update_task = asyncio.create_task(self._live_updater())
        
        self.console.print("[bold green]Async Layout Chat Demo[/bold green]")
        self.console.print("[dim]Starting async layout with real-time editor...[/dim]\n")
        
        try:
            with Live(self._render_layout(), refresh_per_second=30, screen=True) as live:
                while self.running:
                    # Process keyboard input with timeout
                    try:
                        key = await asyncio.wait_for(self.input_queue.get(), timeout=0.01)
                        if not self._handle_key(key):
                            self.running = False
                            break
                    except asyncio.TimeoutError:
                        # No input, continue
                        pass
                    
                    # Update display
                    live.update(self._render_layout())
                    
                    # Small async sleep
                    await asyncio.sleep(0.01)
                
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            
            # Cancel tasks
            input_task.cancel()
            live_update_task.cancel()
            
            try:
                await asyncio.gather(input_task, live_update_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            
            # Show final state
            self.console.clear()
            self.console.print("[green]Async chat session ended.[/green]")
            self.console.print(f"\nTotal messages sent: {self.live_data['message_count']}")
    
    def run(self):
        """Synchronous wrapper."""
        asyncio.run(self.run_async())


def main():
    """Main function."""
    
    console = Console()
    
    console.print("[bold blue]🎯 Async Rich Layout Chat Demo[/bold blue]")
    console.print("=" * 60)
    console.print("\nThis demo showcases asyncio-based concurrency:")
    console.print("  • Clean async/await syntax")
    console.print("  • Multiple concurrent tasks")
    console.print("  • Non-blocking input and updates")
    console.print("  • Same great user experience")
    console.print("\n🎮 Controls:")
    console.print("  • Type in the bottom editor")
    console.print("  • Press Ctrl+D to send message to top panel")
    console.print("  • Press Ctrl+C to exit")
    console.print("\n🚀 Starting async demo...\n")
    
    # Create and run the async layout editor
    editor = AsyncLayoutChatEditor()
    editor.run()


if __name__ == "__main__":
    main()