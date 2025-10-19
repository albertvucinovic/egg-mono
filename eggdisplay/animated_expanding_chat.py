#!/usr/bin/env python3
"""
Animated Expanding Chat Demo
Shows smooth panel expansion with visual feedback.
Both panels start at bottom and expand upward as content grows.
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
from rich.align import Align


class AnimatedExpandingChat:
    """Chat with animated panel expansion."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Use the library's RealTimeEditor
        self.editor = RealTimeEditor(
            initial_text="",
            width=80,
            height=3  # Very compact start
        )
        
        # Chat history
        self.chat_history = [
            "🎯 Animated Expanding Chat Demo",
            "──────────────────────────────",
            "Watch panels expand as you type messages!",
            ""
        ]
        
        # Panel state
        self.top_panel_height = 6    # Start small
        self.bottom_panel_height = 5  # Start small
        self.max_top_height = 20      # Maximum expansion
        self.animation_speed = 0.5    # Expansion speed factor
        
        # Visual effects
        self.expansion_animation = 0
        self.last_message_time = 0
    
    def _update_panel_sizes(self):
        """Update panel sizes based on content with smooth animation."""
        # Calculate target sizes
        message_count = len([m for m in self.chat_history if m and not m.startswith("─")])
        target_top_height = max(6, min(self.max_top_height, message_count * 2 + 4))
        
        editor_lines = len(self.editor.editor.lines)
        target_bottom_height = max(4, min(8, editor_lines + 3))
        
        # Smooth animation towards target
        if self.top_panel_height < target_top_height:
            self.top_panel_height = min(target_top_height, 
                                      self.top_panel_height + self.animation_speed)
            self.expansion_animation = 1.0
        elif self.top_panel_height > target_top_height:
            self.top_panel_height = max(target_top_height, 
                                      self.top_panel_height - self.animation_speed)
        
        if self.bottom_panel_height < target_bottom_height:
            self.bottom_panel_height = min(target_bottom_height, 
                                         self.bottom_panel_height + self.animation_speed)
        elif self.bottom_panel_height > target_bottom_height:
            self.bottom_panel_height = max(target_bottom_height, 
                                         self.bottom_panel_height - self.animation_speed)
        
        # Fade expansion animation
        if self.expansion_animation > 0:
            self.expansion_animation -= 0.1
    
    def _render_top_panel(self) -> Panel:
        """Render the expanding top panel."""
        content = Text()
        
        # Show expansion animation effect
        if self.expansion_animation > 0:
            expansion_intensity = int(self.expansion_animation * 5)
            glow_char = "✨" if expansion_intensity > 2 else "●"
            content.append(f" {glow_char} " * expansion_intensity, style="yellow")
            content.append("\n")
        
        # Header
        content.append(" 🚀 Expanding Output ", style="bold white on blue")
        content.append(f" | Messages: {len(self.chat_history) - 3}")
        content.append(f" | Height: {int(self.top_panel_height)}")
        content.append("\n")
        content.append("─" * 70 + "\n", style="blue")
        
        # Show recent messages (limited by panel height)
        visible_lines = max(1, int(self.top_panel_height) - 6)
        start_idx = max(0, len(self.chat_history) - visible_lines)
        
        for message in self.chat_history[start_idx:]:
            if message.startswith("─"):
                content.append(message + "\n", style="dim")
            elif message.startswith("["):
                content.append("💬 " + message + "\n")
            elif message:
                content.append("• " + message + "\n")
            else:
                content.append("\n")
        
        # Show scroll indicator if not all messages fit
        if start_idx > 0:
            content.append(f"\n[dim]... showing {visible_lines} of {len(self.chat_history)} messages[/dim]")
        
        return Panel(
            content,
            title="[bold]Output (Expands Upward)[/bold]",
            border_style="blue" if self.expansion_animation <= 0 else "yellow",
            height=int(self.top_panel_height)
        )
    
    def _render_bottom_panel(self) -> Panel:
        """Render the compact bottom panel."""
        # Create compact editor view
        editor_content = Text()
        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col
        
        if lines and cursor_row < len(lines):
            current_line = lines[cursor_row]
            # Show compact line with cursor
            max_width = 60
            if len(current_line) > max_width:
                # Show context around cursor
                start = max(0, cursor_col - max_width // 2)
                end = min(len(current_line), start + max_width)
                visible_text = current_line[start:end]
                
                if start > 0:
                    editor_content.append("…", style="dim")
                
                cursor_pos_in_visible = cursor_col - start
                before_cursor = visible_text[:cursor_pos_in_visible]
                cursor_char = visible_text[cursor_pos_in_visible:cursor_pos_in_visible+1] if cursor_pos_in_visible < len(visible_text) else "█"
                after_cursor = visible_text[cursor_pos_in_visible+1:] if cursor_pos_in_visible < len(visible_text) else ""
                
                editor_content.append(before_cursor)
                editor_content.append(cursor_char, style="black on white")
                editor_content.append(after_cursor)
                
                if end < len(current_line):
                    editor_content.append("…", style="dim")
            else:
                before_cursor = current_line[:cursor_col]
                cursor_char = current_line[cursor_col:cursor_col+1] if cursor_col < len(current_line) else "█"
                after_cursor = current_line[cursor_col+1:] if cursor_col < len(current_line) else ""
                
                editor_content.append(before_cursor)
                editor_content.append(cursor_char, style="black on white")
                editor_content.append(after_cursor)
        else:
            editor_content.append("✏️  Start typing...", style="dim")
        
        # Status line
        editor_content.append(f"\n[dim]Line {cursor_row + 1} | Press Ctrl+D to send[/dim]")
        
        return Panel(
            editor_content,
            title="[bold]Input (Stays Compact)[/bold]",
            border_style="green",
            height=int(self.bottom_panel_height)
        )
    
    def _render_layout(self) -> Layout:
        """Render the complete animated layout."""
        layout = Layout()
        
        # Update sizes with animation
        self._update_panel_sizes()
        
        # Create layout with current sizes
        layout.split_column(
            Layout(self._render_top_panel(), name="top", size=int(self.top_panel_height)),
            Layout(self._render_bottom_panel(), name="bottom", size=int(self.bottom_panel_height))
        )
        
        return layout
    
    def _handle_key(self, key: str) -> bool:
        """Handle keyboard input."""
        if key == '\x04':  # Ctrl+D
            # Send message to chat
            message = self.editor.editor.get_text().strip()
            if message:
                timestamp = time.strftime('%H:%M:%S')
                self.chat_history.append(f"[{timestamp}] {message}")
                self.last_message_time = time.time()
                
                # Clear editor
                self.editor.editor.set_text("")
                
                # Trigger expansion animation
                self.expansion_animation = 1.0
            return True
        else:
            return self.editor._handle_key(key)
    
    def run(self):
        """Run the animated expanding chat."""
        self.running = True
        self.editor.running = True
        
        self.console.print("[bold green]Animated Expanding Chat Demo[/bold green]")
        self.console.print("[dim]Watch panels expand with smooth animation![/dim]\n")
        
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
                    
                    # Update display with animation
                    live.update(self._render_layout())
                    
                    time.sleep(0.033)  # ~30fps
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.editor.running = False
            
            # Show final summary
            self.console.clear()
            self.console.print("[green]Animated chat session ended.[/green]")
            self.console.print(f"\n📊 Final Statistics:")
            self.console.print(f"  • Messages sent: {len(self.chat_history) - 3}")
            self.console.print(f"  • Final panel height: {int(self.top_panel_height)} lines")
            self.console.print(f"  • Maximum expansion: {self.max_top_height} lines")


def main():
    """Main function."""
    
    console = Console()
    
    console.print("[bold blue]🎯 Animated Expanding Chat Demo[/bold blue]")
    console.print("=" * 60)
    console.print("\n✨ Features:")
    console.print("  • Both panels start small at bottom")
    console.print("  • Smooth animated expansion")
    console.print("  • Output panel grows upward with messages")
    console.print("  • Visual feedback during expansion")
    console.print("  • Compact input panel")
    console.print("\n🎮 How to use:")
    console.print("  1. Type a message in the bottom panel")
    console.print("  2. Press Ctrl+D to send")
    console.print("  3. Watch the top panel expand!")
    console.print("  4. Continue typing to see more expansion")
    console.print("  5. Press Ctrl+C to exit")
    console.print("\n🚀 Starting animated demo...\n")
    
    # Create and run
    chat = AnimatedExpandingChat()
    chat.run()


if __name__ == "__main__":
    main()