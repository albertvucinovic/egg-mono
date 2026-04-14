#!/usr/bin/env python3
"""
Async Final Chat Demo

Renders inline (scrollable terminal) without using Layout.
Supports variable number of live OutputPanels and an InputPanel.
Allows custom layout composition (side-by-side, stacked) via HStack/VStack or a user-provided builder.
"""

import sys
import os
import time
import asyncio
from typing import List, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eggdisplay import OutputPanel, InputPanel, HStack, VStack, DiffRenderer  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich import box


class FinalChatDemoAsync:
    """Async final chat demo using inline Live and composable layouts."""

    def __init__(self):
        self.console = Console()
        self.running = False
        self._renderer: Optional[DiffRenderer] = None

        # Panels
        chat_output_style = OutputPanel.PanelStyle(border_style="red", box=box.MINIMAL, show_header=False)
        self.chat_output = OutputPanel(title="Chat Messages", initial_height=8, max_height=20, style=chat_output_style)
        self.system_output = OutputPanel(title="System Messages", initial_height=6, max_height=15)
        # Provide autocomplete from app code (e.g., filesystem)
        def file_autocomplete(line: str, row: int, col: int):
            import os, re
            prefix = line[:col]
            m = re.search(r"([\w\-./~]+)$", prefix)
            token = m.group(1) if m else ""
            if not token:
                return []
            expanded = os.path.expanduser(token)
            base_dir = expanded
            needle = ""
            if not os.path.isdir(expanded):
                base_dir = os.path.dirname(expanded) or "."
                needle = os.path.basename(expanded)
            try:
                entries = os.listdir(base_dir)
            except Exception:
                return []
            results = []
            for name in entries:
                if needle and not name.startswith(needle):
                    continue
                path = os.path.join(base_dir, name)
                suffix = "/" if os.path.isdir(path) else ""
                results.append(name[len(needle):] + suffix)
            results.sort(key=lambda s: (0 if s.endswith('/') else 1, s))
            return results[:20]

        self.input_panel = InputPanel(title="Message Input", initial_height=8, max_height=12,
                                      autocomplete_callback=file_autocomplete)

        # Variable output panels (top-to-bottom order by default)
        self.output_panels: List[OutputPanel] = [self.chat_output, self.system_output]

        # Optional custom layout builder
        self.layout_builder: Optional[Callable[[Console, List[OutputPanel], InputPanel], object]] = None

        # App state
        self.chat_messages: List[object] = []
        self.system_messages: List[str] = []

    def set_layout_builder(self, builder: Callable[[Console, List[OutputPanel], InputPanel], object]):
        self.layout_builder = builder

    def add_output_panel(self, title: str, initial_height: int = 6, max_height: int = 15) -> OutputPanel:
        panel = OutputPanel(title=title, initial_height=initial_height, max_height=max_height)
        self.output_panels.append(panel)
        return panel

    def _format_chat_message(self, timestamp: str, message: str) -> str:
        formatted = f"💬 [{timestamp}] {message}"
        if "\n" in message:
            lines = formatted.split("\n")
            formatted = lines[0]
            for line in lines[1:]:
                if line.strip():
                    formatted += f"\n   {line}"
        return formatted

    def _format_system_message(self, message: str) -> str:
        return f"• {message}"

    def _update_panel_content(self) -> None:
        # Build chat content
        chat_content = ""
        for msg in self.chat_messages:
            if isinstance(msg, tuple):
                ts, text = msg
                chat_content += self._format_chat_message(ts, text) + "\n"
            else:
                chat_content += str(msg) + "\n"
        # Build system content
        sys_content = "".join(self._format_system_message(m) + "\n" for m in self.system_messages)

        self.chat_output.set_content(chat_content.strip())
        self.system_output.set_content(sys_content.strip())

    def _render_inline(self) -> Group:
        # Update content
        self._update_panel_content()

        # Custom builder path
        if self.layout_builder is not None:
            custom = self.layout_builder(self.console, self.output_panels, self.input_panel)
            if hasattr(custom, "render") and callable(getattr(custom, "render")) and not hasattr(custom, "__rich_console__"):
                return Group(custom.render())
            if isinstance(custom, Group):
                return custom
            return Group(custom)

        # Default layout: first two side-by-side, rest stacked, then input
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

    def _handle_key(self, key: str) -> bool:
        """Handle keystrokes. Return False to exit."""
        # Ctrl+D to send message; Ctrl+C to exit
        try:
            # Attempt to import readchar for constants if available
            import readchar  # type: ignore
            ctrl_d = getattr(readchar.key, "CTRL_D", "\x04")
            ctrl_c = getattr(readchar.key, "CTRL_C", "\x03")
        except Exception:
            ctrl_d = "\x04"
            ctrl_c = "\x03"

        # Exit on Ctrl+C
        if key == ctrl_c or key == "\x03":
            self.running = False
            return False

        if key == ctrl_d or key == "\x04":
            message = self.input_panel.get_text()
            if message:
                timestamp = time.strftime("%H:%M:%S")
                line_count = message.count("\n") + 1
                self.chat_messages.append((timestamp, message))
                self.system_messages.append(f"Sent {line_count}-line message")
                self.input_panel.clear_text()
                self.input_panel.increment_message_count()
                if self._renderer:
                    self._renderer.print_above(f"[dim]📤 Sent {line_count}-line message[/dim]")
                else:
                    self.console.print(f"[dim]📤 Sent {line_count}-line message[/dim]")
            return True

        # Forward other keys to the editor engine
        return self.input_panel.editor._handle_key(key)

    async def _async_input_reader(self):
        """Read keys asynchronously and push to editor queue."""
        try:
            import readchar  # type: ignore
        except Exception:
            # Ensure dependency is available via eggdisplay module behavior
            import importlib
            importlib.import_module("readchar")

        while self.running:
            try:
                key = await asyncio.to_thread(readchar.readkey)  # type: ignore[name-defined]
                # Normal key path
                self.input_panel.editor.input_queue.put(key)
                if key == getattr(readchar.key, "CTRL_C", "\x03"):
                    break
            except KeyboardInterrupt:
                # Emulate CTRL_C delivery via queue so main loop exits cleanly
                try:
                    self.input_panel.editor.input_queue.put(getattr(readchar.key, "CTRL_C", "\x03"))
                except Exception:
                    pass
                break
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def run_async(self):
        self.running = True
        self.input_panel.editor.running = True

        # Non-live header above the live region
        self.console.print("[bold blue]🎯 Final Chat Demo (Async) with Panel Classes[/bold blue]")
        self.console.print("=" * 60)
        self.console.print("\n✨ Features:")
        self.console.print("  • Uses generic OutputPanel and InputPanel classes")
        self.console.print("  • Variable live output panels; inline layout with HStack/VStack")
        self.console.print("  • Panels only handle multi-line strings; app handles logic")
        self.console.print("\n🎮 How to use:")
        self.console.print("  1. Type messages in the input panel below")
        self.console.print("  2. Use Enter for multi-line messages")
        self.console.print("  3. Press Ctrl+D to send")
        self.console.print("  4. Watch messages appear in output panels")
        self.console.print("  5. Non-live prints appear above live region")
        self.console.print("\n[dim]Starting async chat session...[/dim]\n")

        # Seed messages
        self.chat_messages.extend([
            "Welcome to the Async Final Chat Demo!",
            "────────────────────────────────────",
            "This demo uses inline live rendering (no fullscreen)",
            "Two panels can be side-by-side using HStack",
        ])
        self.system_messages.extend([
            "System initialized",
            "Chat session started",
            "Ready for user input",
        ])

        # Start async input reader task
        input_task = asyncio.create_task(self._async_input_reader())

        try:
            self._renderer = DiffRenderer(self._render_inline(), console=self.console)
            with self._renderer as renderer:
                while self.running:
                    # Drain input queue
                    had_input = False
                    try:
                        while True:
                            key = self.input_panel.editor.input_queue.get_nowait()
                            had_input = True
                            if not self._handle_key(key):
                                self.running = False
                                break
                    except Exception:
                        pass

                    # Only rebuild when something changed
                    if had_input or any(p.is_dirty() for p in self.output_panels) or self.input_panel.is_dirty():
                        renderer.update(self._render_inline())
                    try:
                        await asyncio.sleep(0.033)
                    except asyncio.CancelledError:
                        break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self.running = False
            self.input_panel.editor.running = False
            input_task.cancel()
            try:
                await input_task
            except (asyncio.CancelledError, KeyboardInterrupt, Exception):
                # Swallow cancellation/interrupt from input task
                pass

            # Final stats
            self.console.print("\n[green]✅ Chat session ended.[/green]")
            total_messages = len([m for m in self.chat_messages if isinstance(m, tuple)])
            total_lines = sum(m[1].count("\n") + 1 for m in self.chat_messages if isinstance(m, tuple))
            self.console.print(f"📊 Sent {total_messages} messages with {total_lines} total lines")
            self.console.print(f"📏 Final chat panel height: {self.chat_output.current_height} lines")
            self.console.print(f"📏 Final system panel height: {self.system_output.current_height} lines")


def main():
    console = Console()
    console.print("[bold magenta]🚀 ASYNC FINAL CHAT DEMO STARTING[/bold magenta]")
    console.print("[dim]This demo showcases inline live layout with async input[/dim]")
    console.print("")

    demo = FinalChatDemoAsync()
    try:
        asyncio.run(demo.run_async())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    console.print("\n[bold magenta]🎉 Thanks for trying the Async Demo![/bold magenta]")


if __name__ == "__main__":
    main()
