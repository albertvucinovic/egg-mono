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
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown

# Local development: add sibling libraries to sys.path
import sys as _sys
import subprocess
_ROOT = Path(__file__).resolve().parent
_sys.path.insert(0, str(_ROOT.parent / 'eggthreads'))
_sys.path.insert(0, str(_ROOT.parent / 'eggllm'))
_sys.path.insert(0, str(_ROOT.parent / 'eggdisplay'))

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
    list_children_ids,
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
from eggdisplay import OutputPanel, InputPanel, HStack, VStack  # type: ignore
from completion import get_autocomplete_items  # type: ignore

# Local mixins and utilities
from utils import (
    MODELS_PATH as MODELS_PATH_UTILS,
    ALL_MODELS_PATH as ALL_MODELS_PATH_UTILS,
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
from formatting import FormattingMixin
from panels import PanelsMixin
from approval import ApprovalMixin
from streaming import StreamingMixin
from commands import (
    ModelCommandsMixin,
    ThreadCommandsMixin,
    ToolCommandsMixin,
    SandboxCommandsMixin,
    DisplayCommandsMixin,
    UtilityCommandsMixin,
)

# eggllm (optional, for /model and catalogs)
try:
    from eggllm import LLMClient  # type: ignore
except Exception:  # pragma: no cover - optional
    LLMClient = None  # type: ignore

MODELS_PATH = _ROOT / 'models.json'
ALL_MODELS_PATH = _ROOT / 'all-models.json'
SYSTEM_PROMPT_PATH = _ROOT / 'systemPrompt'

commandsText="""
Commands: 
  Model handling: 
    /model <key>, /updateAllModels <provider> 
  Thread management basic: 
    /spawnChildThread <text>, /spawnAutoApprovedChildThread <text>, /waitForThreads <threads> 
  Thread management other: 
    /parentThread, /listChildren, /threads, /thread <selector>
    /deleteThread <selector>, /newThread <name>, /duplicateThread <name>
    /schedulers 
  Tool management: 
    /toggleAutoApproval, /toolsOn, /toolsOff, /disableTool <name>, /enableTool <name> 
    /toggleSandboxing, /setSandboxConfiguration <file.json>
    /getSandboxingConfig
    /toolsSecrets <on|off>, /toolsStatus 
  Display:
    /togglePanel (chat|children|system)
    /redraw
  Other: 
    /enterMode <send|newline>, /cost, /paste, /quit 
    /help
"""


def _get_system_prompt() -> str:
    try:
        with open(SYSTEM_PROMPT_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return "You are a helpful assistant."


def _snapshot_messages(db: ThreadsDB, thread_id: str) -> List[Dict[str, Any]]:
    th = db.get_thread(thread_id)
    if not th or not th.snapshot_json:
        return []
    try:
        snap = json.loads(th.snapshot_json)
        msgs = snap.get('messages', [])
        return msgs
    except Exception:
        return []


def _get_subtree(db: ThreadsDB, root_id: str) -> List[str]:
    out: List[str] = []
    q = [root_id]
    seen = set()
    while q:
        t = q.pop(0)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        try:
            for cid in list_children_ids(db, t):
                q.append(cid)
        except Exception:
            pass
    return out[1:]


class EggDisplayApp(
    ModelCommandsMixin,
    ThreadCommandsMixin,
    ToolCommandsMixin,
    SandboxCommandsMixin,
    DisplayCommandsMixin,
    UtilityCommandsMixin,
    FormattingMixin,
    PanelsMixin,
    ApprovalMixin,
    StreamingMixin,
):
    """Chat UI using eggdisplay panels with eggthreads backend.

    Layout:
      - HStack(ChatOutput, SystemOutput)
      - InputPanel

    Use Ctrl+D to send, Ctrl+E to clear input, Ctrl+C to quit.
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
        self.chat_output = OutputPanel(title="Chat Messages", initial_height=12, max_height=12, columns_hint=1)
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
        self.input_panel = InputPanel(title="Message Input", initial_height=8, max_height=12,
                                      autocomplete_callback=_adapter, io_mode=io_mode)

        # Panel visibility (single-column layout).
        # Users can toggle these at runtime via /togglePanel.
        self._panel_visible: Dict[str, bool] = {
            'system': True,
            'children': True,
            'chat': True,
        }

        # Auto redraw static console view when terminal is resized.
        # This is debounced so that we redraw once after resizing settles.
        self._auto_redraw_on_resize: bool = True
        self._last_term_size: Optional[tuple[int, int]] = None
        self._resize_dirty_since: Optional[float] = None

        # Streaming/watch state
        self._live_state: Dict[str, Any] = {
            "active_invoke": None,
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
        # Input behavior: default to Enter inserts newline (toggle with
        # /enterMode). Ctrl+D always sends.
        self.enter_sends: bool = False
        # Track last printed event sequence per thread for console output
        self._last_printed_seq_by_thread: Dict[str, int] = {}

        # Pending approval prompt state (execution/output approvals)
        self._pending_prompt: Dict[str, Any] = {}
        # Last time we refreshed the children tree panel (sec since epoch)
        self._last_children_refresh: float = 0.0

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

    def thread_root_id(self, tid: str) -> str:
        """Return the root thread id for any thread id.

        Egg's SubtreeScheduler is keyed by *root* thread id. The UI
        needs a reliable way to map any thread in a subtree to its root
        so we can accurately mark threads as "scheduled" in the tree.

        We primarily use the backend's get_parent() helper (shared
        semantics with eggthreads). We also keep a tiny SQL fallback in
        case get_parent is unavailable or fails.
        """

        cur = tid
        seen: set[str] = set()
        # Hard cap to avoid infinite loops in case of corrupted parent
        # links.
        for _ in range(2048):
            if not cur:
                break
            if cur in seen:
                # Cycle detected; best-effort: treat the current node as
                # the root to avoid crashing the UI.
                return cur
            seen.add(cur)

            parent: Optional[str] = None
            try:
                parent = get_parent(self.db, cur)
            except Exception:
                parent = None
            if parent is None:
                # Fallback (should be equivalent to get_parent)
                try:
                    row = self.db.conn.execute(
                        'SELECT parent_id FROM children WHERE child_id=?',
                        (cur,),
                    ).fetchone()
                    parent = row[0] if row and row[0] else None
                except Exception:
                    parent = None

            if not parent:
                return cur
            cur = parent

        return cur or tid

    def is_thread_scheduled(self, tid: str) -> bool:
        """True if tid's root has an entry in active_schedulers."""
        rid = self.thread_root_id(tid)
        return rid in (self.active_schedulers or {})

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
    def handle_key(self, key: str) -> bool:
        # Ctrl+D sends, Ctrl+E clears input, Ctrl+C exits
        try:
            import readchar  # type: ignore
            ctrl_d = getattr(readchar.key, 'CTRL_D', '\x04')
            ctrl_c = getattr(readchar.key, 'CTRL_C', '\x03')
            ctrl_e = getattr(readchar.key, 'CTRL_E', '\x05')
            ctrl_p = getattr(readchar.key, 'CTRL_P', '\x10')
            enter_key = getattr(readchar.key, 'ENTER', '\r')
        except Exception:
            ctrl_d = '\x04'
            ctrl_c = '\x03'
            ctrl_e = '\x05'
            ctrl_p = '\x10'
            enter_key = '\r'
        # Esc handling: log and forward a logical 'escape' to the editor.
        # Some terminals send a single ESC ('\x1b'), others double ('\x1b\x1b').
        try:
            esc = getattr(readchar.key, 'ESC', '\x1b')  # type: ignore[name-defined]
        except Exception:
            esc = '\x1b'
        esc2 = esc + esc
        if isinstance(key, str) and (key == esc or key == esc2):
            try:
                self.log_system(f"Esc-like key received: {repr(key)}")
            except Exception:
                pass
            try:
                ed = self.input_panel.editor.editor
                # Ask the editor to handle a logical escape first
                ed.handle_key('escape')
                # Then forcefully clear any active completion popup in case
                # the terminal sent a non-standard ESC sequence.
                if hasattr(ed, '_completion_active'):
                    ed._completion_active = False
                if hasattr(ed, '_completion_items'):
                    ed._completion_items = []
                if hasattr(ed, '_completion_index'):
                    ed._completion_index = 0
            except Exception:
                pass
            return True
        # Ctrl+C: interrupt/cancel first, quit only when idle with empty input
        if key == ctrl_c or key == '\x03':
            # Current editor contents
            try:
                current_text = self.input_panel.get_text()
            except Exception:
                current_text = ""
            text_empty = not (current_text.strip())

            # Coarse thread state
            try:
                from eggthreads import thread_state  # type: ignore
                thread_st = thread_state(self.db, self.current_thread)
            except Exception:
                thread_st = "unknown"

            # Is there an active stream (LLM or tool) for this thread?
            try:
                row_open = self.db.current_open(self.current_thread)
            except Exception:
                row_open = None
            has_active_stream = row_open is not None

            # When there is active work (streaming or pending tool approvals),
            # treat Ctrl+C as "interrupt/cancel" without quitting.
            if has_active_stream or thread_st in ("running", "waiting_tool_approval", "waiting_output_approval"):
                # Interrupt any in-flight stream (LLM or tools)
                try:
                    interrupt_thread(self.db, self.current_thread)
                except Exception:
                    pass
                # Best-effort: cancel pending/running tool calls so they do not
                # continue or require further approval.
                try:
                    self.cancel_pending_tools_on_interrupt()
                except Exception:
                    pass
                # Reset live streaming state so UI stops showing partial output
                self._live_state = {
                    "active_invoke": None,
                    "content": "",
                    "reason": "",
                    "tools": {},
                    "tc_text": {},
                    "tc_order": [],
                }
                self.log_system('Interrupted current stream/tool execution with Ctrl+C (thread remains open).')
                # Recompute any approval prompts after cancellation
                self.compute_pending_prompt()
                return True

            # No active work. If there's text in the input panel, clear it.
            if not text_empty:
                self.input_panel.clear_text()
                try:
                    self.log_system('Input cleared with Ctrl+C (press Ctrl+C again on empty input to quit).')
                except Exception:
                    pass
                return True

            # Idle thread and empty input -> quit.
            self.log_system('Exiting on Ctrl+C (no active work and empty input).')
            self.running = False
            return False
        # Send on Ctrl+D always (but if we have a pending approval
        # prompt, interpret it as an approval answer first, regardless
        # of /enterMode).
        if key == ctrl_d or key == '\x04':
            # First, try to handle any pending approval using the current
            # input text as the answer. This works in both /enterMode
            # send and newline.
            if self.handle_pending_approval_answer(self.input_panel.get_text(), source='Ctrl+D'):
                return True
            # No pending approval (or unrecognized answer), treat Ctrl+D
            # as normal send.
            text = self.input_panel.get_text().strip()
            if text:
                try:
                    should_clear = self.on_submit(text)
                except Exception as e:
                    self.log_system(f"Submit error: {e}")
                    should_clear = True
            else:
                should_clear = True
            if should_clear:
                self.input_panel.clear_text()
                self.input_panel.increment_message_count()
            return True
        # Clear input on Ctrl+E
        if key == ctrl_e or key == '\x05':
            self.input_panel.clear_text()
            try:
                self.log_system('Input cleared.')
            except Exception:
                pass
            return True
        # Paste clipboard on Ctrl+P
        if key == ctrl_p or key == '\x10':
            content = read_clipboard()
            if content is None:
                self.log_system('Failed to read clipboard.')
            elif content == '':
                self.log_system('Clipboard is empty.')
            else:
                self.input_panel.editor.editor.set_text(content)
                # Move cursor to start of pasted text so user sees beginning
                self.input_panel.editor.editor.cursor.row = 0
                self.input_panel.editor.editor.cursor.col = 0
                self.input_panel.editor.editor._clamp_cursor()
                # Reset scroll positions to show from start
                self.input_panel._scroll_top = 0
                self.input_panel._hscroll_left = 0
                self.log_system(f'Pasted {len(content)} characters from clipboard.')
            return True
        # Enter behavior depends on mode
        if key in (enter_key, '\r', '\n'):
            # If we have a pending approval prompt and Enter-sends mode
            # is active, interpret y/n/o/a answers via the same helper as
            # Ctrl+D before treating Enter as a normal send.
            if self.enter_sends and self.handle_pending_approval_answer(self.input_panel.get_text(), source='Enter'):
                return True
            if self.enter_sends:
                text = self.input_panel.get_text().strip()
                if text:
                    try:
                        should_clear = self.on_submit(text)
                    except Exception as e:
                        self.log_system(f"Submit error: {e}")
                        should_clear = True
                else:
                    should_clear = True
                if should_clear:
                    self.input_panel.clear_text()
                    self.input_panel.increment_message_count()
                return True
            else:
                # Insert newline in editor
                try:
                    self.input_panel.editor.editor.insert_newline()
                except Exception:
                    pass
                return True
        # delegate to editor engine
        return self.input_panel.editor._handle_key(key)

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

    def enqueue_bash_tool(self, script: str, hidden: bool) -> None:
        """Enqueue a bash command as a user tool call (RA3).

        - For `$ cmd` (hidden=False): output is intended to be visible to the
          model, subject to output_approval gating.
        - For `$$ cmd` (hidden=True): we still execute the command and store
          the result in the thread, but mark the eventual tool message as
          no_api via the output_approval decision so the model does not see
          it.
        """
        import os as _os, json as _json
        cmd = (script or '').strip()
        if not cmd:
            self.log_system('Empty bash command, skipping.')
            return
        # Build a single tool_call entry for the bash tool
        tc_id = _os.urandom(8).hex()
        tool_call = {
            'id': tc_id,
            'type': 'function',
            'function': {
                'name': 'bash',
                'arguments': _json.dumps({'script': cmd}, ensure_ascii=False),
            },
        }
        extra = {
            'tool_calls': [tool_call],
            # Keep the user turn: the runner will execute the tool but we do
            # not immediately hand control to the LLM.
            'keep_user_turn': True,
            'user_command_type': '$$' if hidden else '$',
        }
        if hidden:
            # The user explicitly requested that this command output not be
            # shown to the model; we still allow the tool result to be stored
            # in the thread but mark this triggering message as no_api so it
            # is excluded from LLM context reconstruction.
            extra['no_api'] = True
        # Store the triggering user message (for transcript) and associated
        # tool_calls metadata. Visible commands use "$ ", hidden commands
        # use "$$ " so they are easy to distinguish in the transcript.
        prefix = '$$ ' if hidden else '$ '
        msg_id = append_message(self.db, self.current_thread, 'user', f"{prefix}{cmd}", extra=extra)
        # Automatically approve this tool call so it starts in TC2.1
        try:
            approve_tool_calls_for_thread(
                self.db,
                self.current_thread,
                decision='granted',
                reason='Approved as user-initiated command',
                tool_call_id=tc_id,
            )
        except Exception as e:
            self.log_system(f'Error approving tool call for bash command: {e}')
        # Snapshot and ensure scheduler so that RA3 will pick this up.
        try:
            create_snapshot(self.db, self.current_thread)
        except Exception:
            pass
        self.ensure_scheduler_for(self.current_thread)
        self.log_system(f"Queued bash command as tool_call {tc_id[-6:]} (hidden={hidden}).")

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

    # Thread selector helpers
    # NOTE: The command handling code has been moved to commands/ package mixins.
    # The handle_command() method above dispatches to cmd_* methods in those mixins.

    # ---- This section intentionally removed (old command handlers are now in commands/ mixins) ----
    def select_threads_by_selector(self, selector: str) -> List[str]:
        try:
            rows = list_threads(self.db)
        except Exception:
            rows = []
        sel_l = (selector or '').lower()
        matches: List[str] = []
        for r in rows:
            if r.thread_id == selector:
                matches = [r.thread_id]
                break
        if not matches and sel_l:
            suf = [r.thread_id for r in rows if r.thread_id.lower().endswith(sel_l)]
            if suf:
                matches = suf
        if not matches and sel_l:
            cont = [r.thread_id for r in rows if sel_l in r.thread_id.lower()]
            if cont:
                matches = cont
        if not matches and sel_l:
            name_matches = [r.thread_id for r in rows if isinstance(r.name, str) and sel_l in r.name.lower()]
            if name_matches:
                matches = name_matches
        if not matches and sel_l:
            recap_matches = [r.thread_id for r in rows if isinstance(r.short_recap, str) and sel_l in r.short_recap.lower()]
            if recap_matches:
                matches = recap_matches
        return matches

    def resolve_single_thread_selector(self, selector: str) -> Optional[str]:
        """Resolve a free-form thread selector to a single thread_id.

        This wraps _select_threads_by_selector with the same additional
        fallbacks and created_at ordering used by /thread and /delete so
        that other commands (e.g. /wait) can reuse the exact selector
        semantics.
        """
        from eggthreads import list_threads  # local import to avoid cycles

        sel = (selector or '').strip()
        if not sel:
            return None

        matches = self.select_threads_by_selector(sel)
        if not matches and ' ' in sel:
            sel_first = sel.split()[0]
            matches = self.select_threads_by_selector(sel_first)
        if not matches:
            try:
                rows_all = list_threads(self.db)
                suf = sel.lower()
                matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
            except Exception:
                matches = []
        if not matches:
            return None

        # Order by created_at newest-first, mirroring /thread behavior
        try:
            rows = list_threads(self.db)
            ca = {r.thread_id: r.created_at for r in rows}
        except Exception:
            ca = {}
        matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
        return matches[0]

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
            # Lower refresh rate to reduce CPU, and rely on EventWatcher
            # / input changes to keep the UI responsive.
            with Live(self.render_group(), refresh_per_second=10, screen=False, console=self.console) as live:
                while self.running:
                    # Drain input queue
                    try:
                        while True:
                            key = self.input_panel.editor.input_queue.get_nowait()
                            if not self.handle_key(key):
                                self.running = False
                                break
                    except Exception:
                        pass
                    # Update panels and live region
                    self.update_panels()
                    live.update(self.render_group())

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


if __name__ == '__main__':
    asyncio.run(run_cli())
