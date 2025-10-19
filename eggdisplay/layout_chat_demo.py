#!/usr/bin/env python3
"""
Rich Layout Chat Demo
Top panel: Live updating display area
Bottom panel: Real-time editor that sends text to top on Ctrl+D

Uses the RealTimeEditor class from the library!
"""

import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import RealTimeEditor
from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text


class LayoutChatEditor:
    """Layout-based editor with chat functionality using library classes."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Use the library's RealTimeEditor for the bottom panel
        self.editor = RealTimeEditor(
            initial_text="Type your message here...\nPress Ctrl+D to send, Ctrl+C to exit\n",
            width=80,
            height=8
        )
        
        # Chat history for top panel
        self.chat_history = [
            "Welcome to the Rich Layout Chat Demo!",
            "────────────────────────────────────────",
            "Using RealTimeEditor from the library!",
            "Type in the bottom editor and press Ctrl+D to send messages.",
            ""
        ]
        
        # Live data for top panel
        self.live_data = {
            "message_count": 0,
            "last_update": time.strftime("%H:%M:%S"),
            "active_users": 1
        }
    
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
            title="[bold]Live Chat Panel[/bold]",
            border_style="blue"
        )
    
    def _render_bottom_panel(self) -> Panel:
        """Render the bottom panel with the editor."""
        # Use the editor's render method from the library
        editor_content = self.editor._render()
        
        return Panel(
            editor_content,
            title="[bold]Message Editor (Using RealTimeEditor)[/bold]",
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
    
    def run(self):
        """Run the layout chat demo."""
        self.running = True
        
        # Override the editor's run method to integrate with our layout
        self.editor.running = True
        
        self.console.print("[bold green]Rich Layout Chat Demo[/bold green]")
        self.console.print("[dim]Using RealTimeEditor from library![/dim]")
        self.console.print("[dim]Starting interactive layout with real-time editor...[/dim]\n")
        
        try:
            with Live(self._render_layout(), refresh_per_second=30, screen=True) as live:
                # Start the editor's input worker
                import threading
                input_thread = threading.Thread(target=self.editor._input_worker, daemon=True)
                input_thread.start()
                
                while self.running:
                    # Process keyboard input from the editor's queue
                    try:
                        while True:
                            key = self.editor.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                self.editor.running = False
                                break
                    except:
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
            self.editor.running = False
            
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