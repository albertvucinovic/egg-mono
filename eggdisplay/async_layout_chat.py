#!/usr/bin/env python3
"""
Async Rich Layout Chat Demo
Uses asyncio for cleaner concurrency in the layout chat.

Uses the AsyncRealTimeEditor class from the library!
"""

import asyncio
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import AsyncRealTimeEditor
from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text


class AsyncLayoutChatEditor:
    """Async layout-based editor with chat functionality using library classes."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Use the library's AsyncRealTimeEditor for the bottom panel
        self.editor = AsyncRealTimeEditor(
            initial_text="Type your message here...\nPress Ctrl+D to send, Ctrl+C to exit\n",
            width=80,
            height=8
        )
        
        # Chat history for top panel
        self.chat_history = [
            "Welcome to the Async Layout Chat Demo!",
            "────────────────────────────────────────",
            "Using AsyncRealTimeEditor from the library!",
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
        # Use the editor's render method from the library
        editor_content = self.editor._render()
        
        return Panel(
            editor_content,
            title="[bold]Async Message Editor (Using AsyncRealTimeEditor)[/bold]",
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
        if key == '\x04':  # Ctrl+D
            # Send message to chat
            message = self.editor.editor.get_text().strip()
            if message and message != "Type your message here...":
                timestamp = time.strftime('%H:%M:%S')
                self.chat_history.append(f"[{timestamp}] {message}")
                self.live_data["message_count"] += 1
                self.live_data["last_update"] = timestamp
                
                # Clear editor
                self.editor.editor.set_text("")
            return True
        else:
            # Delegate all other keys to the library's editor
            return self.editor._handle_key(key)
    
    async def run_async(self):
        """Run the async layout chat."""
        self.running = True
        
        # Override the editor's running state
        self.editor.running = True
        
        # Start async tasks
        live_update_task = asyncio.create_task(self._live_updater())
        
        self.console.print("[bold green]Async Layout Chat Demo[/bold green]")
        self.console.print("[dim]Using AsyncRealTimeEditor from library![/dim]")
        self.console.print("[dim]Starting async layout with real-time editor...[/dim]\n")
        
        try:
            with Live(self._render_layout(), refresh_per_second=30, screen=True) as live:
                # Start the editor's input reader
                input_task = asyncio.create_task(self.editor._input_reader())
                
                while self.running:
                    # Process keyboard input from the editor's queue
                    try:
                        key = await asyncio.wait_for(self.editor.input_queue.get(), timeout=0.01)
                        if not self._handle_key(key):
                            self.running = False
                            self.editor.running = False
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
            self.editor.running = False
            
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