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

from text_editor import OutputPanel, InputPanel
from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from typing import List


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
    
    def _render_layout(self) -> Layout:
        """Render the complete layout with a variable number of live panels."""
        layout = Layout()
        
        # Update panel content
        self._update_panel_content()
        
        # Build children from all output panels plus the input panel at the end
        children: List[Layout] = []
        for idx, panel in enumerate(self.output_panels):
            children.append(
                Layout(panel.render(), name=f"out-{idx}", size=panel.calculate_height())
            )
        children.append(Layout(self.input_panel.render(), name="input", size=self.input_panel.calculate_height()))
        
        layout.split_column(*children)
        return layout
    
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
            with Live(self._render_layout(), refresh_per_second=30, screen=False, console=self.console) as live:
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
                    live.update(self._render_layout())
                    
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
