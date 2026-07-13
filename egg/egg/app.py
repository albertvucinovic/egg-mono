#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import replace
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
from eggthreads.command_catalog import CommandContext, create_default_command_registry, create_default_input_prefix_registry  # type: ignore
from eggthreads.runner import scheduler_task_is_live, scheduler_task_status  # type: ignore

# eggdisplay UI components
from eggdisplay import OutputPanel, InputPanel, HStack, VStack, DiffRenderer  # type: ignore
from .completion import AsyncCompletionWorker, CompletionRequest  # type: ignore

# Local mixins and utilities
from .utils import (
    MODELS_PATH,
    ALL_MODELS_PATH,
    IMAGE_GENERATION_MODELS_PATH,
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
from .theme import apply_theme as apply_terminal_theme, register_theme_command
from .attachments import (
    clear_staged_attachments_for_thread,
    register_attachment_commands,
    staged_attachment_count,
    staged_attachments_for_thread,
)
from .image_generation import register_image_generation_command
from .edit_answer import register_edit_answer_command
from .commands import (
    ModelCommandsMixin,
    ThreadCommandsMixin,
    ToolCommandsMixin,
    SandboxCommandsMixin,
    DisplayCommandsMixin,
    UtilityCommandsMixin,
    AuthCommandsMixin,
    SessionCommandsMixin,
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
    SessionCommandsMixin,
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
        self._theme = "default"
        self._rich_theme = None
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
        self._reload_requested: bool = False
        self.command_registry = create_default_command_registry()
        register_theme_command(self.command_registry, self)
        self._staged_attachments_by_thread: Dict[str, List[Dict[str, Any]]] = {}
        register_attachment_commands(self.command_registry, self)
        register_image_generation_command(self.command_registry, self)
        register_edit_answer_command(self.command_registry, self)
        self.input_prefix_registry = create_default_input_prefix_registry()
        reload_thread = (os.environ.get('EGG_RELOAD_THREAD_ID') or '').strip()
        reloaded_existing_thread = False
        if reload_thread and self.db.get_thread(reload_thread):
            self.current_thread = reload_thread
            reloaded_existing_thread = True
        else:
            self.current_thread = create_root_thread(self.db, name='Root', models_path=str(MODELS_PATH))
            append_message(self.db, self.current_thread, 'system', self.system_prompt)
            create_snapshot(self.db, self.current_thread)

        self.active_schedulers: Dict[str, Dict[str, Any]] = {}
        self.start_scheduler(self.thread_root_id(self.current_thread))
        if reloaded_existing_thread:
            self.log_system(f"Reloaded Egg on thread {self.current_thread[-8:]}.")

        # Display mode: "inline" (HEAD-style, native terminal scroll, tiny
        # diffs, stream goes into Chat Messages panel) or "full" (alt-screen
        # TUI, stream-as-static, in-app scroll with mouse wheel). Selected
        # initially via EGG_DISPLAY_MODE env var (default "full"); can be
        # changed at runtime with the /displayMode command, which sets
        # _pending_mode_change and the main loop rebuilds accordingly.
        _mode = os.environ.get('EGG_DISPLAY_MODE', '').strip().lower()
        self._display_is_inline = _mode in ('inline', 'classic', 'head', 'legacy')
        self._pending_mode_change: bool = False
        # UI-only transcript display verbosity. Rendering support is added
        # in later phases; Phase 1 only tracks and updates this state.
        self._display_verbosity: str = 'max'

        # Panels
        # Single-column layout (system, children, chat, input)
        self._build_chat_output_for_mode()
        # System panel: normally just the title bar. While a stream is active,
        # the streaming notification lives in a single second row so long
        # timeout/TPS text does not crowd the title/status line.
        system_style = OutputPanel.PanelStyle(line_wrap_mode="crop")
        self.system_output = OutputPanel(
            title="System",
            initial_height=2,
            max_height=3,
            columns_hint=1,
            style=system_style,
        )
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
            # Keep the current thread's full ID, name, and description visible.
            # Non-maximal Children content is bounded to a few logical lines,
            # so wrapping makes better use of narrow terminals than cropping.
            line_wrap_mode="wrap",
        )
        self.children_output = OutputPanel(
            title="Children",
            initial_height=3,
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
        # Input panel completion is submitted to a latest-request-wins worker
        # once run() owns an asyncio loop. Before then the adapter is a no-op;
        # editor mutation and result application always remain on the UI thread.
        io_mode = os.environ.get("EGG_IO_MODE", "threaded").strip().lower()

        def _async_adapter(line: str, row: int, col: int, generation: int) -> bool:
            return self._request_async_completion(line, row, col, generation)

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
            async_autocomplete_callback=_async_adapter,
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
        self._user_command_tasks: set[asyncio.Task] = set()
        self._input_thread: Optional[Any] = None
        self._input_ready_event: Optional[asyncio.Event] = None
        self._completion_worker: Optional[AsyncCompletionWorker] = None
        self._ui_loop: Optional[asyncio.AbstractEventLoop] = None
        self._external_terminal_active: bool = False
        self._external_terminal_release_event: Optional[asyncio.Event] = None
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

    def _notify_input_ready(self) -> None:
        """Wake the UI loop from the terminal reader thread."""
        loop = self._ui_loop
        event = self._input_ready_event
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    def _request_async_completion(self, line: str, row: int, col: int, generation: int) -> bool:
        """Submit immutable completion identity without blocking key handling."""
        worker = self._completion_worker
        if worker is None:
            return False
        try:
            thread_id = str(self.current_thread)
            snapshot_seq = int(self._snapshot_last_event_seq(thread_id))
        except Exception:
            thread_id = str(getattr(self, 'current_thread', '') or '')
            snapshot_seq = -1
        worker.request(CompletionRequest(
            generation=int(generation),
            line=str(line),
            row=int(row),
            col=int(col),
            thread_id=thread_id,
            snapshot_seq=snapshot_seq,
        ))
        return True

    def _apply_async_completion(self, request: CompletionRequest, items: List[Dict[str, str]]) -> None:
        """Apply a worker result only if editor and thread identity are unchanged."""
        editor = self.input_panel.editor.editor
        try:
            current_snapshot_seq = int(self._snapshot_last_event_seq(self.current_thread))
        except Exception:
            current_snapshot_seq = -1
        if (
            str(self.current_thread) != request.thread_id
            or current_snapshot_seq != request.snapshot_seq
        ):
            editor.discard_completion_result(request.generation)
        else:
            editor.apply_completion_result(
                request.generation,
                request.line,
                request.row,
                request.col,
                items,
            )
        # Completion arrived on the UI loop but it may currently be waiting on
        # the input event. Wake the render tick so the popup appears promptly.
        if self._input_ready_event is not None:
            self._input_ready_event.set()

    def _drain_input_queue(self, limit: int = 32) -> tuple[bool, bool]:
        """Dispatch at most *limit* queued keys, preserving event-loop fairness."""
        had_input = False
        keep_running = True
        queue_obj = self.input_panel.editor.input_queue
        for _ in range(max(1, int(limit))):
            try:
                key = queue_obj.get_nowait()
            except Exception:
                break
            had_input = True
            key_result = self.handle_key(key)
            if asyncio.iscoroutine(key_result):
                self._schedule_user_command_task(key_result)
                key_result = True
            if not key_result:
                self.running = False
                keep_running = False
                break
        if not queue_obj.empty() and self._input_ready_event is not None:
            self._input_ready_event.set()
        return had_input, keep_running

    async def _wait_for_input_or_tick(self, timeout: float = 0.1) -> None:
        """Wait for reader-thread input while retaining periodic UI updates."""
        event = self._input_ready_event
        if event is None:
            await asyncio.sleep(timeout)
            return
        if not self.input_panel.editor.input_queue.empty():
            await asyncio.sleep(0)
            return
        event.clear()
        # Close the clear/queue race: a callback may have set the event just
        # before clear(), while its key is already visible in the thread queue.
        if not self.input_panel.editor.input_queue.empty():
            event.set()
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def run_external_terminal_command(self, argv: List[str]) -> int:
        """Run an interactive subprocess while temporarily releasing Egg's TUI.

        The asyncio event loop remains alive while the foreground program is
        running, so in-process schedulers and child/runtime threads keep being
        polled.  Only terminal ownership and keyboard input are paused.
        """

        if not argv:
            raise ValueError("argv must not be empty")

        editor = getattr(getattr(self, "input_panel", None), "editor", None)
        previous_editor_running = bool(getattr(editor, "running", False)) if editor is not None else False
        if editor is not None:
            try:
                editor.running = False
            except Exception:
                pass
        self._external_terminal_active = True
        release_event = asyncio.Event()
        self._external_terminal_release_event = release_event
        input_thread = getattr(self, "_input_thread", None)
        if input_thread is not None:
            try:
                if input_thread.is_alive():
                    await asyncio.to_thread(input_thread.join, 0.5)
            except Exception:
                pass

        renderer = getattr(self, "_renderer", None)
        if renderer is not None:
            try:
                await asyncio.wait_for(release_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                # Fallback for tests or unusual call sites that invoke this
                # helper while no main render loop can exit the context for us.
                try:
                    renderer.__exit__(None, None, None)
                except Exception:
                    pass
                self._renderer = None
                try:
                    release_event.set()
                except Exception:
                    pass
        else:
            try:
                release_event.set()
            except Exception:
                pass
        try:
            restore_tty()
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(*argv)
            return int(await proc.wait())
        finally:
            try:
                restore_tty()
            except Exception:
                pass
            if previous_editor_running and editor is not None:
                try:
                    # Drop keys typed while the external editor owned the TTY.
                    q = getattr(editor, "input_queue", None)
                    if q is not None:
                        while True:
                            try:
                                q.get_nowait()
                            except Exception:
                                break
                except Exception:
                    pass
                try:
                    editor.running = True
                    import threading

                    input_thread = threading.Thread(target=editor._input_worker, daemon=True)
                    self._input_thread = input_thread
                    input_thread.start()
                except Exception:
                    pass
            self._pending_mode_change = True
            for panel in (
                getattr(self, "chat_output", None),
                getattr(self, "system_output", None),
                getattr(self, "children_output", None),
                getattr(self, "approval_panel", None),
                getattr(self, "input_panel", None),
            ):
                try:
                    panel.mark_dirty()
                except Exception:
                    pass
            self._external_terminal_active = False
            self._external_terminal_release_event = None

    def apply_theme(self, theme_name: str) -> str:
        """Apply a terminal theme and mark live panels for repaint."""
        applied = apply_terminal_theme(self, theme_name)
        renderer = getattr(self, '_renderer', None)
        if renderer is not None:
            try:
                renderer.console = self.console
                renderer.theme = self._rich_theme
            except Exception:
                pass
        for panel in (
            getattr(self, 'chat_output', None),
            getattr(self, 'system_output', None),
            getattr(self, 'children_output', None),
            getattr(self, 'approval_panel', None),
            getattr(self, 'input_panel', None),
        ):
            try:
                panel.mark_dirty()
            except Exception:
                pass
        return applied

    def _build_chat_output_for_mode(self) -> None:
        """(Re)construct the Chat Messages panel for the current display mode.

        Called once in __init__ and again whenever /displayMode flips
        the mode at runtime so the panel dimensions and body behavior
        match the new rendering model.
        """
        chat_style = OutputPanel.PanelStyle(markup=False)
        if self._display_is_inline:
            # Inline mode: streaming preview and recent history render
            # inside the Chat Messages panel body (HEAD behavior).
            self.chat_output = OutputPanel(
                title="Chat Messages",
                initial_height=5,
                max_height=5,
                columns_hint=1,
                style=chat_style,
            )
        else:
            # Full-screen mode: panel body stays empty (streaming and
            # past messages render into DiffRenderer's static window
            # above the live region). Panel collapses to its title bar.
            self.chat_output = OutputPanel(
                title="Chat Messages",
                initial_height=2,
                max_height=2,
                columns_hint=1,
                style=chat_style,
            )
            try:
                self.chat_output.style.show_header = False
            except Exception:
                pass
        # Apply whatever border style is currently in effect.
        try:
            from rich import box as rich_box
            if getattr(self, '_borders_visible', False):
                original = getattr(self, '_original_box_styles', {})
                self.chat_output.style.box = original.get('chat', rich_box.SQUARE)
            else:
                self.chat_output.style.box = rich_box.MINIMAL
        except Exception:
            pass

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
        # Every Egg process should run its own scheduler for each visited root;
        # coordination across processes happens via SQLite per-thread leases.
        # Only suppress a duplicate if this *process* already has a live task.
        existing = self.active_schedulers.get(root_tid)
        if existing is not None and scheduler_task_is_live(existing.get("task")):
            return
        if existing is not None:
            status = scheduler_task_status(existing.get("task"))
            self.active_schedulers.pop(root_tid, None)
            self.log_system(f"Restarting scheduler for root {root_tid[-8:]} (previous task: {status})")
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
            image_generation_models_path=str(IMAGE_GENERATION_MODELS_PATH),
        )
        task = asyncio.create_task(sched.run_forever(poll_sec=poll_sec))
        self.active_schedulers[root_tid] = {"scheduler": sched, "task": task}
        add_done_callback = getattr(task, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(lambda done_task, rid=root_tid: self._scheduler_task_done(rid, done_task))
        self.log_system(f"Started scheduler for root {root_tid[-8:]}")

    def _scheduler_task_done(self, root_tid: str, task: asyncio.Task) -> None:
        """Forget dead scheduler tasks so visiting the root restarts them."""

        entry = self.active_schedulers.get(root_tid)
        if entry is not None and entry.get("task") is task:
            self.active_schedulers.pop(root_tid, None)
        status = scheduler_task_status(task)
        if status != "cancelled":
            self.log_system(f"Scheduler for root {root_tid[-8:]} stopped ({status})")

    def ensure_scheduler_for(self, tid: str) -> None:
        rid = self.thread_root_id(tid)
        entry = self.active_schedulers.get(rid)
        task = entry.get("task") if isinstance(entry, dict) else None
        if not scheduler_task_is_live(task):
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
        prefix_result = self.handle_input_prefix(text)
        if prefix_result is not None:
            return prefix_result.clear_input
        if text.startswith('/paste'):
            self.handle_command(text)
            return False
        if text.startswith('/reload'):
            self.handle_command(text)
            return False
        if text.startswith('/'):
            registry = getattr(self, 'command_registry', None)
            parts = text[1:].split(None, 1)
            cmd = parts[0] if parts else ''
            if registry is not None and cmd and getattr(registry, 'is_async', lambda _name: False)(cmd):
                self._schedule_user_command(text)
            else:
                self.handle_command(text)
            return True
        staged = list(staged_attachments_for_thread(self, self.current_thread))
        if staged:
            from eggthreads.attachment_staging import build_message_content_with_attachments

            content = build_message_content_with_attachments(text, staged)
        else:
            content = text
        append_message(self.db, self.current_thread, 'user', content)
        if staged:
            clear_staged_attachments_for_thread(self, self.current_thread)
        create_snapshot(self.db, self.current_thread)
        self.ensure_scheduler_for(self.current_thread)
        self.log_system("User message queued; scheduler will stream the response.")
        return True

    def _command_context(self) -> CommandContext:
        return CommandContext(
            db=self.db,
            current_thread=self.current_thread,
            set_current_thread=lambda tid: setattr(self, 'current_thread', tid),
            log_system=self.log_system,
            console_print_block=self.console_print_block,
            start_scheduler=self.ensure_scheduler_for,
            llm_client=self.llm_client,
            system_prompt=self.system_prompt,
            get_current_model=self.current_model_for_thread,
            watch_current_thread=self.start_watching_current,
            print_current_thread=self.print_current_thread,
            format_threads=self.format_tree,
            select_threads=self.select_threads_by_selector,
            append_message=append_message,
            create_snapshot=create_snapshot,
            approve_tool_calls=approve_tool_calls_for_thread,
            models_path=str(MODELS_PATH),
            all_models_path=str(ALL_MODELS_PATH),
            image_generation_models_path=str(IMAGE_GENERATION_MODELS_PATH),
            app=self,
        )

    def _staged_attachment_count_for_current_thread(self) -> int:
        return staged_attachment_count(self, self.current_thread)

    def handle_input_prefix(self, text: str):
        """Dispatch non-slash input prefixes through the prefix registry."""
        registry = getattr(self, 'input_prefix_registry', None)
        if registry is None:
            return None
        return registry.execute(text, self._command_context())

    def handle_command(self, text: str) -> None:
        """Dispatch /command through the command registry."""
        parts = text[1:].split(None, 1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ''

        def _visible_feedback(message: str, *, border_style: str = 'blue') -> None:
            try:
                self.console_print_block(f"/{cmd}", message, border_style=border_style)
            except Exception:
                self.log_system(message)

        registry = getattr(self, 'command_registry', None)
        if registry is None:
            message = f'Unknown command: /{cmd}'
            self.log_system(message)
            _visible_feedback(message, border_style='red')
            return

        try:
            registry.get(cmd)
        except KeyError:
            message = f'Unknown command: /{cmd}'
            self.log_system(message)
            _visible_feedback(message, border_style='red')
            return

        log_count = len(getattr(self, '_system_log', []))
        block_count = 0
        context = self._command_context()
        original_printer = context.console_print_block

        def capture_console_print_block(*args, **kwargs):
            nonlocal block_count
            block_count += 1
            if original_printer is not None:
                return original_printer(*args, **kwargs)
            return None

        result = registry.execute(cmd, replace(context, console_print_block=capture_console_print_block), arg)
        try:
            message = getattr(result, 'message', None)
        except Exception:
            message = None
        if isinstance(message, str) and message.strip():
            text = message.strip()
            new_logs = getattr(self, '_system_log', [])[log_count:]
            already_logged = any(str(log).strip() == text for log in new_logs)
            if not already_logged:
                joined_logs = "\n".join(str(log).strip() for log in new_logs if str(log).strip())
                already_logged = joined_logs == text
            if not already_logged:
                self.log_system(text)
            if block_count == 0:
                _visible_feedback(text)

    async def handle_command_async(self, text: str) -> None:
        """Dispatch /command through the command registry without blocking the UI loop."""

        parts = text[1:].split(None, 1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ''

        def _visible_feedback(message: str, *, border_style: str = 'blue') -> None:
            try:
                self.console_print_block(f"/{cmd}", message, border_style=border_style)
            except Exception:
                self.log_system(message)

        registry = getattr(self, 'command_registry', None)
        if registry is None:
            message = f'Unknown command: /{cmd}'
            self.log_system(message)
            _visible_feedback(message, border_style='red')
            return

        try:
            registry.get(cmd)
        except KeyError:
            message = f'Unknown command: /{cmd}'
            self.log_system(message)
            _visible_feedback(message, border_style='red')
            return

        log_count = len(getattr(self, '_system_log', []))
        block_count = 0
        context = self._command_context()
        original_printer = context.console_print_block

        def capture_console_print_block(*args, **kwargs):
            nonlocal block_count
            block_count += 1
            if original_printer is not None:
                return original_printer(*args, **kwargs)
            return None

        self._begin_user_command_stream(cmd)
        try:
            result = await registry.execute_async(cmd, replace(context, console_print_block=capture_console_print_block), arg)
        finally:
            self._end_user_command_stream()
        try:
            message = getattr(result, 'message', None)
        except Exception:
            message = None
        if isinstance(message, str) and message.strip():
            text_msg = message.strip()
            new_logs = getattr(self, '_system_log', [])[log_count:]
            already_logged = any(str(log).strip() == text_msg for log in new_logs)
            if not already_logged:
                joined_logs = "\n".join(str(log).strip() for log in new_logs if str(log).strip())
                already_logged = joined_logs == text_msg
            if not already_logged:
                self.log_system(text_msg)
            if block_count == 0:
                _visible_feedback(text_msg)

    def _begin_user_command_stream(self, command_name: str) -> None:
        try:
            self._live_state = self._make_live_state(
                active_invoke=f"user-command-{uuid.uuid4().hex[:12]}",
                stream_kind='user command',
                started_at=time.time(),
            )
        except Exception:
            self._live_state = {
                "active_invoke": f"user-command-{uuid.uuid4().hex[:12]}",
                "stream_kind": "user command",
                "started_at": time.time(),
            }
        self._live_state['command_name'] = command_name
        self.log_system(f"Running user command /{command_name}...")
        try:
            self.system_output.mark_dirty()
        except Exception:
            pass

    def _schedule_user_command(self, text: str) -> bool:
        """Schedule an async user command while keeping the UI repaint loop alive."""

        return self._schedule_user_command_task(self.handle_command_async(text))

    def _schedule_user_command_task(self, awaitable: Any) -> bool:
        """Schedule an already-created async command awaitable."""

        try:
            task = asyncio.create_task(awaitable)
        except RuntimeError:
            asyncio.run(awaitable)
            return True
        self._user_command_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._user_command_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                try:
                    self._end_user_command_stream()
                except Exception:
                    pass
                self.log_system(f"User command failed: {e}")

        task.add_done_callback(_done)
        return True

    def _end_user_command_stream(self) -> None:
        try:
            self._live_state = self._make_live_state()
        except Exception:
            self._live_state = {"active_invoke": None, "stream_kind": None, "started_at": None}
        try:
            self.system_output.mark_dirty()
        except Exception:
            pass

    # ---------------- Main loop ----------------
    async def run(self):
        self.running = True
        self._ui_loop = asyncio.get_running_loop()
        self._input_ready_event = asyncio.Event()
        self._completion_worker = AsyncCompletionWorker(
            getattr(self.db, 'path', '.egg/threads.sqlite'),
            self.llm_client,
            self.command_registry,
            self._ui_loop,
            self._apply_async_completion,
        )
        self.input_panel.editor.input_ready_callback = self._notify_input_ready
        await self.start_watching_current()

        self.print_banner()
        # Print initial static view to console so history is visible above live panels
        if self._display_is_inline:
            self.print_static_view_current(heading=f"Switched to thread: {self.current_thread}")

        # Start input worker thread (readchar -> queue)
        import threading
        # Ensure the editor input loop is enabled
        try:
            self.input_panel.editor.running = True
        except Exception:
            pass
        input_thread = threading.Thread(target=self.input_panel.editor._input_worker, daemon=True)
        self._input_thread = input_thread
        input_thread.start()

        try:
            # DiffRenderer: flicker-free rendering via line-level diffing +
            # synchronized output (CSI 2026h/l). Critical for SSH / tmux.
            # Wrapped in an outer loop so /displayMode can teardown the
            # renderer, rebuild for the new mode, and re-enter.
            while self.running:
                self._build_chat_output_for_mode()
                mode_name = 'inline' if self._display_is_inline else 'full'
                self._renderer = DiffRenderer(
                    self.render_group(), console=self.console, mode=mode_name, theme=self._rich_theme,
                )
                if not self._display_is_inline:
                    self._install_transcript_scrollback_source(self._renderer)
                with self._renderer as renderer:
                    # Inline mode uses the terminal's native scrollback, so on
                    # mode switches into inline we print the full static
                    # transcript. Full-screen history is provided by the lazy
                    # TranscriptScrollbackSource installed before __enter__ so
                    # the initial paint only renders the visible tail.
                    if self._display_is_inline and self._pending_mode_change:
                        try:
                            self.print_static_view_current(heading=None)
                        except Exception:
                            pass
                    # If a stream is in flight (mode switch mid-stream),
                    # replay the accumulated _live_state into the new
                    # renderer so the in-flight preview doesn't disappear.
                    # Inline mode displays it automatically via
                    # compose_chat_panel_text; full-screen needs us to
                    # seed its stream buffer here.
                    try:
                        self._replay_stream_to_renderer()
                    except Exception:
                        pass
                    self._pending_mode_change = False
                    while self.running and not self._pending_mode_change and not self._external_terminal_active:
                        # Dispatch a bounded batch. If more keys remain the
                        # input event stays set, giving watcher/render tasks a turn
                        # before the next batch instead of monopolizing the loop.
                        had_input, _keep_running = self._drain_input_queue()
                        # A bare ESC is held briefly so split escape
                        # sequences (e.g. SGR mouse reports delivered
                        # across multiple readkey calls) can re-attach;
                        # flush any that have aged past the debounce.
                        try:
                            self.flush_pending_esc_if_stale()
                        except Exception:
                            pass
                        # Drain stale orphan-mouse fragments so a
                        # truncated SGR mouse report (terminator lost
                        # to readchar split delivery) does not stay
                        # buffered forever.
                        try:
                            self.flush_pending_orphan_mouse_if_stale()
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
                            await self._wait_for_input_or_tick(0.1)
                        except asyncio.CancelledError:
                            break
                # Exited the with block. If the inner loop set
                # _pending_mode_change, loop and rebuild the renderer.
                # Otherwise the user asked to quit; break the outer loop.
                if not self.running:
                    break
                if self._external_terminal_active:
                    self._renderer = None
                    release_event = getattr(self, "_external_terminal_release_event", None)
                    if release_event is not None:
                        try:
                            release_event.set()
                        except Exception:
                            pass
                    while self.running and self._external_terminal_active:
                        try:
                            await asyncio.sleep(0.1)
                        except asyncio.CancelledError:
                            break
                    if self.running:
                        continue
                if not self._pending_mode_change:
                    break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self.running = False
            # Cancel background tasks and await their shutdown so asyncio
            # does not destroy still-pending scheduler/runner tasks on exit.
            tasks = []
            if self._watch_task:
                tasks.append(self._watch_task)
            try:
                for entry in self.active_schedulers.values():
                    task = entry.get("task") if isinstance(entry, dict) else None
                    if task is not None:
                        tasks.append(task)
            except Exception:
                pass
            if tasks:
                for task in tasks:
                    try:
                        task.cancel()
                    except Exception:
                        pass
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass
            try:
                schedulers = [
                    entry.get("scheduler")
                    for entry in self.active_schedulers.values()
                    if isinstance(entry, dict) and entry.get("scheduler") is not None
                ]
                await asyncio.gather(
                    *(sched.shutdown() for sched in schedulers if hasattr(sched, "shutdown")),
                    return_exceptions=True,
                )
            except Exception:
                pass
            try:
                self.active_schedulers.clear()
            except Exception:
                pass
            worker = self._completion_worker
            self._completion_worker = None
            if worker is not None:
                worker.stop()
                try:
                    worker.join(0.5)
                except Exception:
                    pass
            try:
                self.input_panel.editor.input_ready_callback = None
            except Exception:
                pass
            self._input_ready_event = None
            self._ui_loop = None
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


async def run_cli() -> int:
    app = EggDisplayApp()
    await app.run()
    if getattr(app, '_reload_requested', False):
        if not getattr(app, '_reload_via_shell', False):
            egg_sh = Path(__file__).resolve().parents[1] / 'egg.sh'
            if egg_sh.is_file():
                os.execv(str(egg_sh), [str(egg_sh), *_sys.argv[1:]])
        try:
            return int(os.environ.get('EGG_RELOAD_EXIT_CODE', '75'))
        except Exception:
            return 75
    return 0


def main():
    raise SystemExit(asyncio.run(run_cli()))


if __name__ == '__main__':
    main()
