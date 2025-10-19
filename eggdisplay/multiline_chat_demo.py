#!/usr/bin/env python3
"""
Multi-line Chat Demo
Specifically tests and demonstrates proper handling of multi-line messages
in both input and output panels.
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


class MultiLineChatDemo:
    """Demo focused on multi-line message handling."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Use the library's RealTimeEditor
        self.editor = RealTimeEditor(
            initial_text="",
            width=80,
            height=6
        )
        
        # Chat history
        self.chat_history = [
            "🎯 Multi-line Chat Demo",
            "──────────────────────",
            "",
            "💡 Try sending multi-line messages!",
            "• Type multiple lines in the input",
            "• Press Ctrl+D to send",
            "• Watch output panel expand properly",
            "• Each line of your message should show",
            ""
        ]
        
        # Panel state
        self.output_panel_height = 10
        self.input_panel_height = 8
        self.max_output_height = 25
    
    def _calculate_panel_sizes(self):
        """Calculate panel sizes based on multi-line content."""
        # Calculate output panel size based on actual content lines
        visible_messages = [m for m in self.chat_history if m and not m.startswith("─")]
        
        # Count total lines in output
        total_output_lines = 0
        for message in visible_messages:
            if message.startswith("["):
                # User message - count all lines
                message_content = message.split("] ", 1)[1] if "] " in message else message
                message_lines = message_content.count('\n') + 1
                total_output_lines += message_lines + 1  # +1 for timestamp/bullet
            else:
                # System message
                total_output_lines += 1
        
        # Add space for headers and padding
        total_output_lines += 3
        
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
        """Render the output panel with multi-line message support."""
        content = Text()
        
        # Header with line count info
        total_messages = len([m for m in self.chat_history if m.startswith("[")])
        total_lines = sum(m.count('\n') + 1 for m in self.chat_history if m.startswith("["))
        
        content.append(" 💬 Multi-line Output ", style="bold white on blue")
        content.append(f" | Messages: {total_messages}")
        content.append(f" | Total lines: {total_lines}")
        content.append(f" | Height: {int(self.output_panel_height)}")
        content.append("\n")
        content.append("─" * 70 + "\n", style="blue")
        
        # Show messages with proper multi-line formatting
        visible_lines = max(1, int(self.output_panel_height) - 6)
        lines_shown = 0
        
        for message in reversed(self.chat_history):
            if lines_shown >= visible_lines:
                break
                
            if message.startswith("─"):
                content.append(message + "\n", style="dim")
                lines_shown += 1
            elif message.startswith("["):
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
                if lines_shown < visible_lines:
                    content.append("💬 " + timestamp + " " + message_lines[0] + "\n")
                    lines_shown += 1
                
                # Show remaining lines indented
                for line in message_lines[1:]:
                    if lines_shown < visible_lines and line.strip():
                        content.append("   " + line + "\n")
                        lines_shown += 1
                    elif lines_shown >= visible_lines:
                        break
            elif message:
                if lines_shown < visible_lines:
                    content.append("• " + message + "\n")
                    lines_shown += 1
            else:
                if lines_shown < visible_lines:
                    content.append("\n")
                    lines_shown += 1
        
        # Show scroll indicator if needed
        if lines_shown < len(self.chat_history):
            content.append(f"\n[dim]... showing {lines_shown} of {len(self.chat_history)} items[/dim]")
        
        return Panel(
            content,
            title="[bold]Output (Multi-line Aware)[/bold]",
            border_style="blue",
            height=int(self.output_panel_height)
        )
    
    def _render_input_panel(self) -> Panel:
        """Render the input panel."""
        editor_content = Text()
        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col
        
        # Panel height includes content + status line
        available_content_lines = self.input_panel_height - 1
        
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
        
        editor_content.append(f"[dim]📝 Lines: {showing_range} | Multi-line ready! | Ctrl+D to send[/dim]")
        
        return Panel(
            editor_content,
            title="[bold]Multi-line Input[/bold]",
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
            # Send multi-line message to chat
            message = self.editor.editor.get_text().strip()
            if message:
                timestamp = time.strftime('%H:%M:%S')
                
                # Count lines in the message for debugging
                line_count = message.count('\n') + 1
                
                # Add to chat history
                self.chat_history.append(f"[{timestamp}] {message}")
                
                # Clear editor
                self.editor.editor.set_text("")
                
                # Debug output
                print(f"[DEBUG] Sent {line_count}-line message")
                print(f"[DEBUG] Output panel target height: {int(self.output_panel_height)}")
            return True
        else:
            return self.editor._handle_key(key)
    
    def run(self):
        """Run the multi-line chat demo."""
        self.running = True
        self.editor.running = True
        
        self.console.print("[bold green]Multi-line Chat Demo[/bold green]")
        self.console.print("[dim]Testing proper multi-line message handling[/dim]\n")
        
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
            
            # Show summary
            self.console.clear()
            self.console.print("[green]Multi-line demo ended.[/green]")
            total_messages = len([m for m in self.chat_history if m.startswith("[")])
            total_lines = sum(m.count('\n') + 1 for m in self.chat_history if m.startswith("["))
            self.console.print(f"\n📊 Sent {total_messages} messages with {total_lines} total lines")
            self.console.print(f"📏 Final output height: {int(self.output_panel_height)} lines")


def main():
    """Main function."""
    
    console = Console()
    
    console.print("[bold blue]🎯 Multi-line Chat Demo[/bold blue]")
    console.print("=" * 60)
    console.print("\nThis demo specifically tests multi-line message handling:")
    console.print("")
    console.print("✨ Features:")
    console.print("  • Proper counting of multi-line messages")
    console.print("  • Output panel expands based on actual content lines")
    console.print("  • Each line of multi-line messages is displayed")
    console.print("  • Smooth panel expansion animation")
    console.print("")
    console.print("🎮 Test it:")
    console.print("  1. Type a multi-line message (press Enter for new lines)")
    console.print("  2. Press Ctrl+D to send")
    console.print("  3. Watch output panel expand to show ALL lines")
    console.print("  4. Repeat with different message lengths")
    console.print("")
    console.print("🚀 Starting multi-line demo...\n")
    
    # Create and run
    demo = MultiLineChatDemo()
    demo.run()


if __name__ == "__main__":
    main()