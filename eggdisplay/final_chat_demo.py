#!/usr/bin/env python3
"""
Final Chat Demo
- Messages displayed newest at bottom (like real chat)
- Proper multi-line counting and expansion
- Console messages above panels for proper flow
- Top-to-bottom: Console → Output → Input
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


class FinalChatDemo:
    """Final chat demo with proper message ordering and layout."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Use the library's RealTimeEditor
        self.editor = RealTimeEditor(
            initial_text="",
            width=80,
            height=6
        )
        
        # Chat history - will display newest at bottom
        self.chat_history = []
        
        # Panel state
        self.output_panel_height = 8
        self.input_panel_height = 8
        self.max_output_height = 20
        
        # Track sent messages for debugging
        self.message_count = 0
    
    def _calculate_panel_sizes(self):
        """Calculate panel sizes based on multi-line content."""
        # Calculate output panel size based on actual content lines
        total_output_lines = 0
        
        # Count lines for each message
        for message in self.chat_history:
            if message.startswith("["):
                # User message - count all lines
                message_content = message.split("] ", 1)[1] if "] " in message else message
                message_lines = message_content.count('\n') + 1
                total_output_lines += message_lines + 1  # +1 for timestamp/bullet
            else:
                # System message
                total_output_lines += 1
        
        # Add space for headers and padding
        total_output_lines += 3  # Header, separator, padding
        
        # Smooth animation towards target
        target_output_height = max(8, min(self.max_output_height, total_output_lines))
        if self.output_panel_height < target_output_height:
            self.output_panel_height = min(target_output_height, self.output_panel_height + 0.5)
        elif self.output_panel_height > target_output_height:
            self.output_panel_height = max(target_output_height, self.output_panel_height - 0.5)
        
        # Input panel size based on editor content
        editor_lines = len(self.editor.editor.lines)
        target_input_height = max(6, min(12, editor_lines + 4))
        if self.input_panel_height < target_input_height:
            self.input_panel_height = min(target_input_height, self.input_panel_height + 0.3)
        elif self.input_panel_height > target_input_height:
            self.input_panel_height = max(target_input_height, self.input_panel_height - 0.3)
    
    def _render_output_panel(self) -> Panel:
        """Render the output panel with newest messages at bottom."""
        content = Text()
        
        # Header with stats
        total_messages = len([m for m in self.chat_history if m.startswith("[")])
        total_lines = sum(m.count('\n') + 1 for m in self.chat_history if m.startswith("["))
        
        content.append(" 💬 Chat Output (Newest at Bottom) ", style="bold white on blue")
        content.append(f" | Messages: {total_messages}")
        content.append(f" | Total lines: {total_lines}")
        content.append(f" | Height: {int(self.output_panel_height)}")
        content.append("\n")
        content.append("─" * 70 + "\n", style="blue")
        
        # SIMPLE APPROACH: Render ALL messages into one big string
        # Then show only the bottom part that fits
        
        # First, render ALL messages into a temporary text buffer
        full_content = Text()
        for message in self.chat_history:
            if message.startswith("["):
                # User message with timestamp
                timestamp_end = message.find("]")
                if timestamp_end != -1:
                    timestamp = message[:timestamp_end + 1]
                    message_content = message[timestamp_end + 2:]
                else:
                    timestamp = ""
                    message_content = message
                
                # Split multi-line message
                message_lines = message_content.split('\n')
                
                # Show first line with timestamp
                full_content.append("💬 " + timestamp + " " + message_lines[0] + "\n")
                
                # Show remaining lines indented
                for line in message_lines[1:]:
                    if line.strip():
                        full_content.append("   " + line + "\n")
            elif message:
                full_content.append("• " + message + "\n")
            else:
                full_content.append("\n")
        
        # Now split the full content into lines
        full_text = str(full_content)
        all_lines = full_text.split('\n')
        
        # Calculate how many content lines we can show (after header)
        available_lines = max(1, int(self.output_panel_height) - 4)
        
        # Show only the bottom part that fits
        if len(all_lines) <= available_lines:
            # All lines fit, show everything
            for line in all_lines:
                # Reconstruct the rich formatting (simplified)
                if line.startswith("💬"):
                    content.append(line + "\n")
                elif line.startswith("   ") and line.strip():
                    content.append(line + "\n")
                elif line.startswith("•"):
                    content.append(line + "\n")
                elif line.strip():
                    content.append(line + "\n")
                else:
                    content.append("\n")
        else:
            # Show only the bottom 'available_lines' lines
            start_index = len(all_lines) - available_lines
            for i in range(start_index, len(all_lines)):
                line = all_lines[i]
                if line.startswith("💬"):
                    content.append(line + "\n")
                elif line.startswith("   ") and line.strip():
                    content.append(line + "\n")
                elif line.startswith("•"):
                    content.append(line + "\n")
                elif line.strip():
                    content.append(line + "\n")
                else:
                    content.append("\n")
        
        # Show scroll indicator if we're not showing all content
        if len(all_lines) > available_lines:
            hidden_lines = len(all_lines) - available_lines
            content.append(f"[dim]... {hidden_lines} lines above (scroll coming soon)[/dim]")
        
        return Panel(
            content,
            title="[bold]Chat Output[/bold]",
            border_style="blue",
            height=int(self.output_panel_height)
        )
    
    def _render_input_panel(self) -> Panel:
        """Render the input panel."""
        editor_content = Text()
        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col
        
        # Panel height includes content + status line
        available_content_lines = int(self.input_panel_height) - 1
        
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
            title="[bold]Message Input[/bold]",
            border_style="green",
            height=int(self.input_panel_height)
        )
    
    def _render_layout(self) -> Layout:
        """Render the complete layout."""
        layout = Layout()
        
        # Update panel sizes
        self._calculate_panel_sizes()
        
        # Create layout
        layout.split_column(
            Layout(self._render_output_panel(), name="top", size=int(self.output_panel_height)),
            Layout(self._render_input_panel(), name="bottom", size=int(self.input_panel_height))
        )
        
        return layout
    
    def _handle_key(self, key: str) -> bool:
        """Handle keyboard input."""
        if key == '\x04':  # Ctrl+D
            # Send message to chat
            message = self.editor.editor.get_text().strip()
            if message:
                timestamp = time.strftime('%H:%M:%S')
                
                # Count lines for debugging
                line_count = message.count('\n') + 1
                
                # Add to chat history (newest at end)
                self.chat_history.append(f"[{timestamp}] {message}")
                self.message_count += 1
                
                # Clear editor
                self.editor.editor.set_text("")
                
                # Debug output to console (above panels)
                self.console.print(f"[dim]📤 Sent {line_count}-line message #{self.message_count}[/dim]")
                
            return True
        else:
            return self.editor._handle_key(key)
    
    def run(self):
        """Run the final chat demo."""
        self.running = True
        self.editor.running = True
        
        # Console messages above the panels
        self.console.print("[bold blue]🎯 Final Chat Demo[/bold blue]")
        self.console.print("=" * 60)
        self.console.print("\n✨ Features:")
        self.console.print("  • Messages displayed newest at bottom (like real chat)")
        self.console.print("  • Proper multi-line counting and expansion")
        self.console.print("  • Console messages appear above panels")
        self.console.print("  • Top-to-bottom: Console → Output → Input")
        self.console.print("\n🎮 How to use:")
        self.console.print("  1. Type messages in the input panel below")
        self.console.print("  2. Use Enter for multi-line messages")
        self.console.print("  3. Press Ctrl+D to send")
        self.console.print("  4. Watch messages appear at bottom of output panel")
        self.console.print("  5. See console updates above panels")
        self.console.print("\n[dim]Starting chat session...[/dim]\n")
        
        # Add some initial messages to demonstrate
        self.chat_history.extend([
            "Welcome to the Final Chat Demo!",
            "──────────────────────────────",
            "Messages will appear newest at bottom",
            "Multi-line messages expand properly",
            ""
        ])
        
        try:
            with Live(self._render_layout(), refresh_per_second=30, screen=True) as live:
                # Start input handling
                import threading
                input_thread = threading.Thread(target=self.editor._input_worker, daemon=True)
                input_thread.start()
                
                while self.running:
                    # Process input
                    try:
                        while True:
                            key = self.editor.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                self.editor.running = False
                                break
                    except:
                        pass
                    
                    # Update display
                    live.update(self._render_layout())
                    
                    time.sleep(0.033)
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.editor.running = False
            
            # Final console message
            self.console.print("\n[green]✅ Chat session ended.[/green]")
            total_messages = len([m for m in self.chat_history if m.startswith("[")])
            total_lines = sum(m.count('\n') + 1 for m in self.chat_history if m.startswith("["))
            self.console.print(f"📊 Sent {total_messages} messages with {total_lines} total lines")
            self.console.print(f"📏 Final output height: {int(self.output_panel_height)} lines")


def main():
    """Main function."""
    
    console = Console()
    
    # Welcome message
    console.print("[bold magenta]🚀 FINAL CHAT DEMO STARTING[/bold magenta]")
    console.print("[dim]This demo showcases the complete chat experience[/dim]")
    console.print("")
    
    # Create and run
    demo = FinalChatDemo()
    demo.run()
    
    # Goodbye message
    console.print("\n[bold magenta]🎉 Thank you for testing the Final Chat Demo![/bold magenta]")


if __name__ == "__main__":
    main()