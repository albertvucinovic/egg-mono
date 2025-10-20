#!/usr/bin/env python3
"""
Final Chat Demo using the new Panel classes
- Uses OutputPanel and InputPanel from the library
- Two output panels: one for chat messages, one for system messages
- Generic panels that only handle multi-line strings
"""

import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import OutputPanel, InputPanel, HStack, VStack
from rich.console import Console, Group
from rich.live import Live
from typing import List, Callable, Optional


class FinalChatDemo:
    """Final chat demo using the new panel classes."""
    
    def __init__(self):
        self.console = Console()
        self.running = False
        
        # Create panels using the library classes
        self.chat_output = OutputPanel(
            title="Chat Messages",
            initial_height=8,
            max_height=20
        )
        
        self.system_output = OutputPanel(
            title="System Messages", 
            initial_height=6,
            max_height=15
        )
        
        self.input_panel = InputPanel(
            title="Message Input",
            initial_height=8,
            max_height=12
        )
        
        # Support a variable number of live output panels (top-to-bottom order)
        # Users can append more panels via add_output_panel().
        self.output_panels: List[OutputPanel] = [
            self.chat_output,
            self.system_output,
        ]
        
        # Optional layout builder hook: users can supply a function that
        # takes (console, output_panels, input_panel) and returns a Rich
        # renderable (e.g., Group, Columns, Panel, HStack().render(), etc.).
        self.layout_builder: Optional[Callable[[Console, List[OutputPanel], InputPanel], object]] = None
        
        # Chat history - application logic, not panel logic
        self.chat_messages = []
        self.system_messages = []

    def add_output_panel(self, title: str, initial_height: int = 6, max_height: int = 15) -> OutputPanel:
        """Create and append a new live OutputPanel to the stack."""
        panel = OutputPanel(title=title, initial_height=initial_height, max_height=max_height)
        self.output_panels.append(panel)
        return panel
    
    def _format_chat_message(self, timestamp: str, message: str) -> str:
        """Format a chat message for display."""
        formatted = f"💬 [{timestamp}] {message}"
        # Indent multi-line messages
        if '\n' in message:
            lines = formatted.split('\n')
            formatted = lines[0]
            for line in lines[1:]:
                if line.strip():
                    formatted += f"\n   {line}"
        return formatted
    
    def _format_system_message(self, message: str) -> str:
        """Format a system message for display."""
        return f"• {message}"
    
    def _update_panel_content(self):
        """Update panel content from application data."""
        # Format chat messages for display
        chat_content = ""
        for msg in self.chat_messages:
            if isinstance(msg, tuple):  # Chat message with timestamp
                timestamp, message = msg
                chat_content += self._format_chat_message(timestamp, message) + "\n"
            else:  # Simple string
                chat_content += msg + "\n"
        
        # Format system messages for display
        system_content = ""
        for msg in self.system_messages:
            system_content += self._format_system_message(msg) + "\n"
        
        # Update panel content
        self.chat_output.set_content(chat_content.strip())
        self.system_output.set_content(system_content.strip())
    
    def _render_stack(self) -> Group:
        """Render a vertical stack (Group) of panels without using Layout.

        Demonstrates that library users could mix HStack and VStack to build
        custom inline layouts, including side-by-side OutputPanels.
        """
        # Update panel content from current app state
        self._update_panel_content()

        # If a custom builder is provided, use it
        if self.layout_builder is not None:
            custom = self.layout_builder(self.console, self.output_panels, self.input_panel)
            # The builder can return either a renderable or a builder like HStack/VStack
            if hasattr(custom, "render") and callable(getattr(custom, "render")) and not hasattr(custom, "__rich_console__"):
                return Group(custom.render())  # builder-like object
            # If it's already a renderable, wrap as needed
            if isinstance(custom, Group):
                return custom
            return Group(custom)

        # Default: first two output panels side-by-side if available; the rest stacked
        rows = []
        if len(self.output_panels) >= 2:
            rows.append(HStack(self.output_panels[:2]).render())
            for p in self.output_panels[2:]:
                rows.append(p.render())
        else:
            for p in self.output_panels:
                rows.append(p.render())

        rows.append(self.input_panel.render())
        return Group(*rows)

    def set_layout_builder(self, builder: Callable[[Console, List[OutputPanel], InputPanel], object]):
        """Allow users of the library to define custom inline layouts.

        The builder receives (console, output_panels, input_panel) and should
        return a Rich renderable (e.g., Group, Columns, Panel), or one of the
        helper builders (HStack/VStack) which will be rendered.
        """
        self.layout_builder = builder
    
    def _handle_key(self, key: str) -> bool:
        """Handle keyboard input."""
        if key == '\x04':  # Ctrl+D
            # Send message to chat
            message = self.input_panel.get_text()
            if message:
                timestamp = time.strftime('%H:%M:%S')
                
                # Count lines for debugging
                line_count = message.count('\n') + 1
                
                # Add to chat history (newest at end)
                self.chat_messages.append((timestamp, message))
                
                # Add system message
                self.system_messages.append(f"Sent {line_count}-line message")
                
                # Clear editor and increment counter
                self.input_panel.clear_text()
                self.input_panel.increment_message_count()
                
                # Debug output to console (above panels)
                self.console.print(f"[dim]📤 Sent {line_count}-line message[/dim]")
                
            return True
        else:
            # Pass the key to the input panel's editor
            return self.input_panel.editor._handle_key(key)
    
    def run(self):
        """Run the final chat demo."""
        self.running = True
        self.input_panel.editor.running = True
        
        # Console messages above the panels
        self.console.print("[bold blue]🎯 Final Chat Demo with Panel Classes[/bold blue]")
        self.console.print("=" * 60)
        self.console.print("\n✨ Features:")
        self.console.print("  • Uses generic OutputPanel and InputPanel classes")
        self.console.print("  • Two output panels: chat and system messages")
        self.console.print("  • Panels only handle multi-line strings (no message logic)")
        self.console.print("  • Application handles message formatting and logic")
        self.console.print("\n🎮 How to use:")
        self.console.print("  1. Type messages in the input panel below")
        self.console.print("  2. Use Enter for multi-line messages")
        self.console.print("  3. Press Ctrl+D to send")
        self.console.print("  4. Watch messages appear in both output panels")
        self.console.print("  5. See console updates above panels")
        self.console.print("\n[dim]Starting chat session...[/dim]\n")
        
        # Add some initial messages to demonstrate
        self.chat_messages.extend([
            "Welcome to the Final Chat Demo!",
            "──────────────────────────────",
            "This demo uses the new generic panel classes",
            "Chat messages appear in the top panel",
            "System messages appear in the middle panel"
        ])
        
        self.system_messages.extend([
            "System initialized",
            "Chat session started",
            "Ready for user input"
        ])
        
        try:
            # Use screen=False to render inline with normal terminal scrolling.
            # Share the same Console so non-live prints appear above the live region.
            with Live(self._render_stack(), refresh_per_second=30, screen=False, console=self.console) as live:
                # Start input handling
                import threading
                input_thread = threading.Thread(target=self.input_panel.editor._input_worker, daemon=True)
                input_thread.start()
                
                while self.running:
                    # Process input
                    try:
                        while True:
                            key = self.input_panel.editor.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                self.input_panel.editor.running = False
                                break
                    except:
                        pass
                    
                    # Update display
                    live.update(self._render_stack())
                    
                    time.sleep(0.033)
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.input_panel.editor.running = False
            
            # Final console message
            self.console.print("\n[green]✅ Chat session ended.[/green]")
            total_messages = len([m for m in self.chat_messages if isinstance(m, tuple)])
            total_lines = sum(m[1].count('\n') + 1 for m in self.chat_messages if isinstance(m, tuple))
            self.console.print(f"📊 Sent {total_messages} messages with {total_lines} total lines")
            self.console.print(f"📏 Final chat panel height: {self.chat_output.current_height} lines")
            self.console.print(f"📏 Final system panel height: {self.system_output.current_height} lines")


def main():
    """Main function."""
    
    console = Console()
    
    # Welcome message
    console.print("[bold magenta]🚀 FINAL CHAT DEMO WITH PANEL CLASSES STARTING[/bold magenta]")
    console.print("[dim]This demo showcases the new generic panel classes[/dim]")
    console.print("")
    
    # Create and run
    demo = FinalChatDemo()
    demo.run()
    
    # Goodbye message
    console.print("\n[bold magenta]🎉 Thank you for testing the Panel Classes Demo![/bold magenta]")


if __name__ == "__main__":
    main()
