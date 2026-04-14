#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown

import sys as _sys
import subprocess

# Global flag: allow forcing run without aiohttp (no hard HTTP cancellation)
_FORCE_WITHOUT_AIOHTTP = '--force-without-aiohttp' in _sys.argv
if _FORCE_WITHOUT_AIOHTTP:
    os.environ['EGG_FORCE_WITHOUT_AIOHTTP'] = '1'
    _sys.argv = [a for a in _sys.argv if a != '--force-without-aiohttp']

# eggthreads backend
from eggthreads import (  # type: ignore
    ThreadsDB,
    SubtreeScheduler,
    create_root_thread,
    create_child_thread,
    append_message,
    delete_thread,
    interrupt_thread,
    list_threads,
    list_root_threads,
    get_parent,
    list_children_with_meta,
    create_snapshot,
    approve_tool_calls_for_thread,
    pause_thread,
    resume_thread,
    current_thread_model_info,
)
from eggthreads import (  # type: ignore
    get_sandbox_status,
)
from eggthreads.event_watcher import EventWatcher  # type: ignore

# eggdisplay UI components
from eggdisplay import OutputPanel, InputPanel, HStack, VStack, DiffRenderer  # type: ignore
from .completion import get_autocomplete_items  # type: ignore

# Local mixins and utilities
from .utils import (
    MODELS_PATH,
    ALL_MODELS_PATH,
    SYSTEM_PROMPT_PATH,
    COMMANDS_TEXT,
    get_system_prompt,
    snapshot_messages,
    get_subtree,
    looks_markdown,
    shorten_output_preview,
    read_clipboard,
    restore_tty,
)
from .formatting import FormattingMixin
from .panels import PanelsMixin
from .approval import ApprovalMixin
from .streaming import StreamingMixin
from .input import InputMixin
from .commands import (
    ModelCommandsMixin,
    ThreadCommandsMixin,
    ToolCommandsMixin,
    SandboxCommandsMixin,
    DisplayCommandsMixin,
    UtilityCommandsMixin,
    AuthCommandsMixin,
)

# eggllm (optional, for /model and catalogs)
try:
    from eggllm import LLMClient  # type: ignore
except Exception:  # pragma: no cover - optional
    LLMClient = None  # type: ignore

class EggDisplayApp(
    ModelCommandsMixin,
    ThreadCommandsMixin,
    ToolCommandsMixin,
    SandboxCommandsMixin,
    DisplayCommandsMixin,
    UtilityCommandsMixin,
    AuthCommandsMixin,
    FormattingMixin,
    PanelsMixin,
    ApprovalMixin,
    StreamingMixin,
    InputMixin,
):
    """Chat UI using eggdisplay panels with eggthreads backend.

    Layout:
      - HStack(ChatOutput, SystemOutput)
      - InputPanel

    Use Enter/Ctrl+D to send. Use Shift+Enter or Alt+Enter for a newline.
    Ctrl+E clears input, Ctrl+C quits.
    Commands start with '/'.
    """

    def __init__(self):
        self.console = Console()
        self.db = ThreadsDB()
        self.db.init_schema()

        # Optional LLM client (for /model listing & catalogs)
        try:
            self.llm_client = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH) if LLMClient else None
        except Exception:
            self.llm_client = None

        # If LLM is enabled and hard cancellation is expected, ensure
        # aiohttp is available unless the user explicitly forces
        # degraded mode via --force-without-aiohttp / EGG_FORCE_WITHOUT_AIOHTTP.
        if self.llm_client is not None:
            force_no_aiohttp = bool(os.environ.get('EGG_FORCE_WITHOUT_AIOHTTP'))
            if not force_no_aiohttp:
                try:
                    import aiohttp  # type: ignore
                except Exception:
                    # Exit early with a clear message so the user knows
                    # why Ctrl+C will not work as expected.
                    self.console.print(
                        "[bold red]aiohttp is required for streaming cancellation (Ctrl+C) in Egg.[/bold red]\n"
                        "[bold yellow]Install it with:[/bold yellow] [white]pip install aiohttp[/white]\n"
                        "Or run [white]./egg.sh --force-without-aiohttp[/white] to continue without hard HTTP cancellation."
                    )
                    raise SystemExit(1)
            else:
                try:
                    self.console.print(
                        "[yellow]Warning: running without aiohttp; Ctrl+C will not stop the underlying HTTP stream.\n"
                        "Use --force-without-aiohttp only if you understand the implications.[/yellow]"
                    )
                except Exception:
                    pass

        # Threads and scheduler setup
        self.system_prompt = get_system_prompt()
        # Initialize system log early so _start_scheduler can log
        self._system_log: List[str] = []
        # Sandbox status (updated on startup and whenever the user
        # changes configuration).  Used by the System panel to surface
        # a persistent warning when tools are running without a
        # sandbox.
        self._sandbox_status: Dict[str, Any] = {}
        self.current_thread: str = create_root_thread(self.db, name='Root', models_path=str(MODELS_PATH))
        append_message(self.db, self.current_thread, 'system', self.system_prompt)
        create_snapshot(self.db, self.current_thread)

        self.active_schedulers: Dict[str, Dict[str, Any]] = {}
        self.start_scheduler(self.current_thread)

        # Panels
        # Single-column layout (system, children, chat, input)
        chat_style = OutputPanel.PanelStyle(markup=False)
        self.chat_output = OutputPanel(
            title="Chat Messages",
            initial_height=12,
            max_height=12,
            columns_hint=1,
            style=chat_style,
        )
        # System panel: keep compact by default (~5 content lines).
        # OutputPanel height includes borders; with wrap-mode padding this
        # corresponds to ~5 content lines.
        self.system_output = OutputPanel(title="System", initial_height=7, max_height=7, columns_hint=1)
        # For the System panel we want the sandboxing status to appear
        # only in the panel title (border), not as a separate header
        # line inside the content area.
        try:
            self.system_output.style.show_header = False
        except Exception:
            pass
        # Children panel: shows subtree of the current thread
        children_style = OutputPanel.PanelStyle(
            border_style="cyan",
            box="SQUARE",
            title_style="bold cyan",
            title_align="left",
            show_header=True,
            header_style="bold white on cyan",
            header_separator_char="─",
            header_separator_style="cyan",
            line_wrap_mode="crop",
        )
        self.children_output = OutputPanel(
            title="Children",
            initial_height=5,
            max_height=24,
            columns_hint=1,
            style=children_style,
        )
        # Approval panel: appears between the output panels and the input
        # panel when an execution or output approval is pending for the
        # current thread. Style it in yellow to stand out.
        approval_style = OutputPanel.PanelStyle(
            border_style="yellow",
            box="SQUARE",
            title_style="bold yellow",
            title_align="left",
            show_header=True,
            header_style="bold black on yellow",
            header_separator_char="─",
            header_separator_style="yellow",
        )
        self.approval_panel = OutputPanel(
            title="Approval",
            initial_height=3,
            max_height=6,
            columns_hint=1,
            style=approval_style,
        )
        # Input panel with autocomplete via completion module
        io_mode = os.environ.get("EGG_IO_MODE", "threaded").strip().lower()
        def _adapter(line: str, row: int, col: int):
            return get_autocomplete_items(line, col, self.db, lambda: self.current_thread, self.llm_client)
        # Input panel (no footer/status line; keep it visually clean).
        try:
            from eggdisplay import InputPanel as _InputPanel  # type: ignore
            input_style = _InputPanel.PanelStyle(show_footer=False)
        except Exception:
            input_style = None

        self.input_panel = InputPanel(
            title="Message Input",
            initial_height=8,
            max_height=12,
            autocomplete_callback=_adapter,
            io_mode=io_mode,
            style=input_style,
        )

        # Panel visibility (single-column layout).
        # Users can toggle these at runtime via /togglePanel.
        self._panel_visible: Dict[str, bool] = {
            'system': True,
            'children': True,
            'chat': True,
        }

        # Border visibility for all output panels (not input panel).
        # Users can toggle this at runtime via /toggleBorders.
        self._borders_visible: bool = False  # Off by default
        # Store original box styles so we can restore them when toggling borders back on.
        self._original_box_styles: Dict[str, Any] = {
            'chat': self.chat_output.style.box,
            'system': self.system_output.style.box,
            'children': self.children_output.style.box,
            'approval': self.approval_panel.style.box,
        }
        # Apply minimal box style since borders are off by default
        from rich import box as rich_box
        self.chat_output.style.box = rich_box.MINIMAL
        self.system_output.style.box = rich_box.MINIMAL
        self.children_output.style.box = rich_box.MINIMAL
        self.approval_panel.style.box = rich_box.MINIMAL

        # Auto redraw static console view when terminal is resized.
        # This is debounced so that we redraw once after resizing settles.
        self._auto_redraw_on_resize: bool = True
        self._last_term_size: Optional[tuple[int, int]] = None
        self._resize_dirty_since: Optional[float] = None

        # Streaming/watch state
        self._live_state: Dict[str, Any] = {
            "active_invoke": None,
            "stream_kind": None,
            "started_at": None,
            "content": "",
            "reason": "",
            "tools": {},  # name -> text
            "tc_text": {},  # key -> text
            "tc_order": [],
        }
        self._watch_task: Optional[asyncio.Task] = None
        self.running = False
        # ensure system log exists (double safety)
        if not hasattr(self, '_system_log'):
            self._system_log = []
        # Input behavior: Enter sends by default.
        # Use Alt+Enter / Shift+Enter to insert a newline. Ctrl+D always sends.
        # (/enterMode can still override this behaviour.)
        self.enter_sends: bool = True
        # Track last printed event sequence per thread for console output
        self._last_printed_seq_by_thread: Dict[str, int] = {}

        # Pending approval prompt state (execution/output approvals)
        self._pending_prompt: Dict[str, Any] = {}
        # Last time we refreshed the children tree panel (sec since epoch)
        self._last_children_refresh: float = time.time()

        # Log any global sandbox availability warning.
        try:
            self._sandbox_status = get_sandbox_status()
            warn = self._sandbox_status.get('warning')
            if isinstance(warn, str) and warn:
                self.log_system(f"[bold red]Sandbox warning:[/bold red] {warn}")
        except Exception:
            self._sandbox_status = {}

        # We intentionally do not persist any sandbox configuration at
        # startup. Sandboxing is per-thread and inherited from ancestors;
        # a thread will fall back to .egg/srt/default.json when no
        # sandbox.config event is present in its ancestry.

    def start_scheduler(self, root_tid: str) -> None:
        # Scheduler lifecycle is managed elsewhere; here we only avoid
        # starting duplicate schedulers for the same root.
        if root_tid in self.active_schedulers:
            return
        # The SubtreeScheduler scans the entire subtree looking for runnable
        # threads. A very aggressive poll interval (e.g. 50ms) can keep a CPU
        # core busy even when nothing is happening. Use a slightly more
        # relaxed default and allow users to tune it via an environment
        # variable if they need snappier or lazier behaviour.
        try:
            poll_env = os.environ.get("EGG_SCHEDULER_POLL_SEC")
            poll_sec = float(poll_env) if poll_env is not None else 0.15
            if poll_sec <= 0:
                poll_sec = 0.15
        except Exception:
            poll_sec = 0.15

        sched = SubtreeScheduler(
            self.db,
            root_thread_id=root_tid,
            models_path=str(MODELS_PATH),
            all_models_path=str(ALL_MODELS_PATH),
        )
        task = asyncio.create_task(sched.run_forever(poll_sec=poll_sec))
        self.active_schedulers[root_tid] = {"scheduler": sched, "task": task}
        self.log_system(f"Started scheduler for root {root_tid[-8:]}")

    def ensure_scheduler_for(self, tid: str) -> None:
        rid = self.thread_root_id(tid)
        if rid not in self.active_schedulers:
            self.start_scheduler(rid)

    def current_model_for_thread(self, tid: str) -> Optional[str]:
        """Return the effective model for a thread using eggthreads API.

        This calls eggthreads.current_thread_model so the UI and
        ThreadRunner share the exact same semantics for model selection.
        """
        try:
            from eggthreads import current_thread_model  # type: ignore
        except Exception:
            # Fallback: show only the thread's initial_model_key if any.
            th = self.db.get_thread(tid)
            imk = getattr(th, 'initial_model_key', None) if th else None
            return imk.strip() if isinstance(imk, str) and imk.strip() else None
        return current_thread_model(self.db, tid)

    # ---------------- Input and commands ----------------
    def on_submit(self, text: str) -> bool:
        """
        Process user-submitted text.
        Returns True if the input panel should be cleared, False otherwise.
        """
        # User command execution ($ / $$) is modeled as a user-originated
        # tool call (RA3). We enqueue a user message with tool_calls and
        # an automatic tool_call.approval so that the ThreadRunner executes
        # the command via the bash tool under the normal tool-call state
        # machine, rather than running it locally in the UI.
        if text.startswith('$$') and len(text) > 2:
            self.enqueue_bash_tool(text[2:].strip(), hidden=True)
            return True
        if text.startswith('$') and len(text) > 1:
            self.enqueue_bash_tool(text[1:].strip(), hidden=False)
            return True
        if text.startswith('/paste'):
            self.handle_command(text)
            return False
        if text.startswith('/'):
            self.handle_command(text)
            return True
        append_message(self.db, self.current_thread, 'user', text)
        create_snapshot(self.db, self.current_thread)
        self.ensure_scheduler_for(self.current_thread)
        self.log_system("User message queued; scheduler will stream the response.")
        return True

    def handle_command(self, text: str) -> None:
        """Dispatch /command to the appropriate cmd_* method from mixins."""
        parts = text[1:].split(None, 1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ''

        # Look up the command method
        handler = getattr(self, f'cmd_{cmd}', None)
        if handler is not None:
            # Special case: spawnChildThread needs the original text for logging
            if cmd == 'spawnChildThread':
                handler(arg, text=text)
            else:
                handler(arg)
        else:
            self.log_system(f'Unknown command: /{cmd}')

    # ---------------- Main loop ----------------
    async def run(self):
        self.running = True
        await self.start_watching_current()

        self.print_banner()
        # Print initial static view to console so history is visible above live panels
        self.print_static_view_current(heading=f"Switched to thread: {self.current_thread}")

        # Start input worker thread (readchar -> queue)
        import threading
        # Ensure the editor input loop is enabled
        try:
            self.input_panel.editor.running = True
        except Exception:
            pass
        input_thread = threading.Thread(target=self.input_panel.editor._input_worker, daemon=True)
        input_thread.start()

        try:
            # DiffRenderer: flicker-free rendering via line-level diffing +
            # synchronized output (CSI 2026h/l). Critical for SSH / tmux.
            self._renderer = DiffRenderer(self.render_group(), console=self.console)
            with self._renderer as renderer:
                while self.running:
                    # Drain input queue
                    had_input = False
                    try:
                        while True:
                            key = self.input_panel.editor.input_queue.get_nowait()
                            had_input = True
                            if not self.handle_key(key):
                                self.running = False
                                break
                    except Exception:
                        pass
                    # Update panels content (sets dirty flags on changes)
                    self.update_panels()
                    # Only rebuild when something changed
                    panels = [self.system_output, self.children_output,
                              self.chat_output, self.approval_panel]
                    if had_input or any(p.is_dirty() for p in panels) or self.input_panel.is_dirty():
                        renderer.update(self.render_group())

                    # Optional: auto-redraw static view on terminal resize.
                    # We keep this low-overhead by only sampling terminal size
                    # and debouncing redraw to happen after resizing settles.
                    if self._auto_redraw_on_resize:
                        try:
                            import shutil as _shutil

                            sz = _shutil.get_terminal_size(fallback=(100, 24))
                            cur_sz = (int(sz.columns), int(sz.lines))
                            if self._last_term_size is None:
                                self._last_term_size = cur_sz
                            elif cur_sz != self._last_term_size:
                                self._last_term_size = cur_sz
                                self._resize_dirty_since = time.time()
                            else:
                                if self._resize_dirty_since is not None and (time.time() - self._resize_dirty_since) > 0.75:
                                    self._resize_dirty_since = None
                                    # Don't do expensive redraw while actively streaming.
                                    try:
                                        row_open = self.db.current_open(self.current_thread)
                                    except Exception:
                                        row_open = None
                                    if row_open is None:
                                        self.redraw_static_view(reason='resize')
                        except Exception:
                            pass

                    try:
                        await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self.running = False
            # Cancel watcher task
            if self._watch_task:
                try:
                    self._watch_task.cancel()
                    await asyncio.sleep(0)
                except Exception:
                    pass
            # Stop editor input loop
            try:
                self.input_panel.editor.running = False
            except Exception:
                pass
            # Best-effort: restore TTY so subsequent shell input is
            # visible and line-editing works as expected.
            try:
                restore_tty()
            except Exception:
                pass


async def run_cli():
    app = EggDisplayApp()
    await app.run()


def main():
    asyncio.run(run_cli())


if __name__ == '__main__':
    main()
