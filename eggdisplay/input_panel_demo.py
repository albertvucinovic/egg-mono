#!/usr/bin/env python3
"""
Input Panel Demo
Focuses on showing how the input panel properly displays new lines
and expands upward to keep the cursor visible.
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


class InputPanelDemo:
    """Demo focused on input panel behavior."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Use the library's RealTimeEditor
        self.editor = RealTimeEditor(
            initial_text="",
            width=80,
            height=4
        )
        
        # Panel state
        self.input_panel_height = 6
        self.max_input_height = 12
        
        # Instructions
        self.instructions = [
            "🎯 INPUT PANEL DEMO",
            "──────────────────",
            "",
            "💡 How this works:",
            "• Type text and press Enter for new lines",
            "• Input panel expands UPWARD to show context",
            "• Cursor line stays visible at bottom",
            "• Previous lines scroll up naturally",
            "",
            "🎮 Try:",
            "1. Type a line of text",
            "2. Press Enter for new line",
            "3. Watch panel expand upward",
            "4. Cursor stays at bottom!"
        ]
    
    def _update_input_panel_size(self):
        """Update input panel size based on content."""
        lines = len(self.editor.editor.lines)
        cursor_row = self.editor.editor.cursor.row
        
        # Panel grows with content, but cursor line stays at bottom
        target_height = min(self.max_input_height, max(6, lines + 3))
        
        # Smooth animation
        if self.input_panel_height < target_height:
            self.input_panel_height = min(target_height, self.input_panel_height + 0.3)
        elif self.input_panel_height > target_height:
            self.input_panel_height = max(target_height, self.input_panel_height - 0.3)
    
    def _render_instructions_panel(self) -> Panel:
        """Render the instructions panel."""
        content = Text()
        
        for line in self.instructions:
            if line.startswith("🎯"):
                content.append(line + "\n", style="bold blue")
            elif line.startswith("💡"):
                content.append(line + "\n", style="bold green")
            elif line.startswith("🎮"):
                content.append(line + "\n", style="bold yellow")
            elif line.startswith("─"):
                content.append(line + "\n", style="dim")
            elif line.startswith("•") or line.startswith("1.") or line.startswith("2.") or line.startswith("3.") or line.startswith("4."):
                content.append("  " + line + "\n")
            elif line:
                content.append(line + "\n")
            else:
                content.append("\n")
        
        return Panel(
            content,
            title="[bold]How It Works[/bold]",
            border_style="blue"
        )
    
    def _render_input_panel(self) -> Panel:
        """Render the input panel with proper footer visibility."""
        editor_content = Text()
        lines = self.editor.editor.lines
        cursor_row, cursor_col = self.editor.editor.cursor.row, self.editor.editor.cursor.col
        
        # Total panel height includes: content + cursor line + status line
        # We need to ensure the status line is always visible
        panel_height = int(self.input_panel_height)
        
        # Available lines for content (excluding status line)
        available_content_lines = panel_height - 1  # Reserve 1 line for status
        
        # We want to show the cursor line and some context around it
        # The cursor line should be visible, and we'll show lines above/below it
        
        # Calculate viewport - what range of lines to show
        # We'll center the viewport around the cursor if possible
        viewport_size = available_content_lines
        
        # Start with cursor in the middle of the viewport
        viewport_start = max(0, cursor_row - viewport_size // 2)
        viewport_end = min(len(lines), viewport_start + viewport_size)
        
        # Adjust if we're at the beginning or end
        if viewport_end - viewport_start < viewport_size:
            # Not enough lines to fill viewport, show from start
            viewport_start = max(0, viewport_end - viewport_size)
        
        # Show the lines in the viewport
        for i in range(viewport_start, viewport_end):
            if i < len(lines):
                line_num = f"{i+1:2d}: "
                
                if i == cursor_row:
                    # Current line with cursor - highlighted
                    editor_content.append(line_num, style="bold green")
                    line_content = lines[i]
                    
                    before_cursor = line_content[:cursor_col]
                    cursor_char = line_content[cursor_col:cursor_col+1] if cursor_col < len(line_content) else "█"
                    after_cursor = line_content[cursor_col+1:] if cursor_col < len(line_content) else ""
                    
                    editor_content.append(before_cursor)
                    editor_content.append(cursor_char, style="black on white")
                    editor_content.append(after_cursor)
                else:
                    # Other lines
                    editor_content.append(line_num, style="dim")
                    line_text = lines[i]
                    if len(line_text) > 70:
                        editor_content.append(line_text[:67] + "...")
                    else:
                        editor_content.append(line_text)
                
                editor_content.append("\n")
        
        # Fill remaining space if we don't have enough lines
        lines_shown = viewport_end - viewport_start
        empty_lines_needed = int(available_content_lines - lines_shown)
        for _ in range(empty_lines_needed):
            editor_content.append("\n")
        
        # Status line - ALWAYS VISIBLE at bottom
        total_lines = len(lines)
        showing_lines = f"{viewport_start + 1}-{viewport_end}/{total_lines}" if total_lines > available_content_lines else f"{total_lines}"
        
        panel_status = f"📝 Lines: {showing_lines} | Cursor: {cursor_row + 1}:{cursor_col + 1} | Ctrl+D to clear"
        editor_content.append(f"[dim]{panel_status}[/dim]")
        
        return Panel(
            editor_content,
            title=f"[bold]Input Panel ({panel_height} lines)[/bold]",
            border_style="green",
            height=panel_height
        )
    
    def _render_layout(self) -> Layout:
        """Render the complete layout."""
        layout = Layout()
        
        # Update panel size
        self._update_input_panel_size()
        
        # Create layout
        layout.split_column(
            Layout(self._render_instructions_panel(), name="top", ratio=2),
            Layout(self._render_input_panel(), name="bottom")
        )
        
        return layout
    
    def _handle_key(self, key: str) -> bool:
        """Handle keyboard input."""
        if key == '\x04':  # Ctrl+D
            # In this demo, Ctrl+D just clears for testing
            self.editor.editor.set_text("")
            return True
        else:
            # Delegate to library editor
            return self.editor._handle_key(key)
    
    def run(self):
        """Run the input panel demo."""
        self.running = True
        self.editor.running = True
        
        self.console.print("[bold green]Input Panel Behavior Demo[/bold green]")
        self.console.print("[dim]Focus on how input panel expands upward[/dim]\n")
        
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
            self.console.print("[green]Input panel demo ended.[/green]")
            final_lines = len(self.editor.editor.lines)
            self.console.print(f"\n📊 You typed {final_lines} lines total")
            self.console.print(f"📏 Final panel height: {int(self.input_panel_height)} lines")


def main():
    """Main function."""
    
    console = Console()
    
    console.print("[bold blue]🎯 Input Panel Behavior Demo[/bold blue]")
    console.print("=" * 60)
    console.print("\nThis demo specifically shows how the input panel should behave:")
    console.print("")
    console.print("✨ Key Behavior:")
    console.print("  • Panel expands UPWARD as you add lines")
    console.print("  • Cursor line stays at BOTTOM of panel")
    console.print("  • Previous lines scroll up naturally")
    console.print("  • No content gets hidden by expansion")
    console.print("")
    console.print("🎮 Try it:")
    console.print("  1. Type some text")
    console.print("  2. Press Enter for new lines")
    console.print("  3. Watch the panel expand upward")
    console.print("  4. Cursor stays visible at bottom!")
    console.print("")
    console.print("🚀 Starting demo...\n")
    
    # Create and run
    demo = InputPanelDemo()
    demo.run()


if __name__ == "__main__":
    main()