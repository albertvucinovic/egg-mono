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
    pause_thread,
    resume_thread,
)
from eggthreads.event_watcher import EventWatcher  # type: ignore

# eggdisplay UI components
from eggdisplay import OutputPanel, InputPanel, HStack, VStack  # type: ignore
from completion import get_autocomplete_items  # type: ignore

# eggllm (optional, for /model and catalogs)
try:
    from eggllm import LLMClient  # type: ignore
except Exception:  # pragma: no cover - optional
    LLMClient = None  # type: ignore

MODELS_PATH = _ROOT / 'models.json'
ALL_MODELS_PATH = _ROOT / 'all-models.json'
SYSTEM_PROMPT_PATH = _ROOT / 'systemPrompt'


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


class EggDisplayApp:
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
        self.system_prompt = _get_system_prompt()
        # Initialize system log early so _start_scheduler can log
        self._system_log: List[str] = []
        self.current_thread: str = create_root_thread(self.db, name='Root')
        append_message(self.db, self.current_thread, 'system', self.system_prompt)
        create_snapshot(self.db, self.current_thread)

        self.active_schedulers: Dict[str, Dict[str, Any]] = {}
        self._start_scheduler(self.current_thread)

        # Panels
        # columns_hint=2 because chat/system panels are side-by-side
        self.chat_output = OutputPanel(title="Chat Messages", initial_height=12, max_height=28, columns_hint=2)
        # System panel
        self.system_output = OutputPanel(title="System", initial_height=8, max_height=16, columns_hint=2)
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
            columns_hint=2,
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

    # ---------------- TTY helpers ----------------
    def _restore_tty(self) -> None:
        """Best-effort restoration of terminal settings (echo / canonical).

        In some environments, libraries like readchar or low-level input
        handling may leave the TTY with echo or canonical mode disabled
        if the process exits unexpectedly. This method tries to ensure
        that, when Egg exits, the terminal is in a sane state so that
        subsequent shell input is visible again.
        """
        try:
            import sys as _sys
            import termios as _termios
        except Exception:
            return
        try:
            if not _sys.stdin.isatty():
                return
            fd = _sys.stdin.fileno()
            try:
                attrs = _termios.tcgetattr(fd)
            except Exception:
                return
            # Ensure echo and canonical mode are enabled
            lflag = attrs[3]
            lflag |= _termios.ECHO | _termios.ICANON
            attrs[3] = lflag
            try:
                _termios.tcsetattr(fd, _termios.TCSADRAIN, attrs)
            except Exception:
                pass
        except Exception:
            pass

    # ---------------- Scheduler & thread helpers ----------------
    def _thread_root_id(self, tid: str) -> str:
        cur = tid
        while True:
            row = self.db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (cur,)).fetchone()
            if not row or not row[0]:
                return cur
            cur = row[0]

    def _start_scheduler(self, root_tid: str) -> None:
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
        self._log_system(f"Started scheduler for root {root_tid[-8:]}")

    def _ensure_scheduler_for(self, tid: str) -> None:
        rid = self._thread_root_id(tid)
        if rid not in self.active_schedulers:
            self._start_scheduler(rid)

    def _current_model_for_thread(self, tid: str) -> Optional[str]:
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

    # ---------------- Formatting helpers ----------------
    def _format_thread_line(self, tid: str) -> str:
        th = self.db.get_thread(tid)
        status = th.status if th else 'unknown'
        recap = (th.short_recap if th and th.short_recap else 'No recap').strip()
        mk = self._current_model_for_thread(tid) or 'default'
        try:
            streaming = self.db.current_open(tid) is not None
        except Exception:
            streaming = False
        label = th.name if th and th.name else ''
        id_short = tid[-8:]
        sflag = '[bold yellow]STREAMING[/bold yellow] ' if streaming else ''
        cur_tag = '[bold cyan][CUR][/bold cyan] ' if tid == self.current_thread else ''
        sched_tag = '[bold cyan][SCHED][/bold cyan] ' if self._thread_root_id(tid) in self.active_schedulers else ''
        # Color status
        if status == 'active':
            status_tag = f"[bold green]{status}[/]"
        elif status == 'paused':
            status_tag = f"[bold red]{status}[/]"
        else:
            status_tag = f"[bold]{status}[/]"
        return (
            f"{cur_tag}{sched_tag}{sflag}[dim]{id_short}[/dim] {status_tag} - {recap} "
            f"[dim][model: {mk}][/dim]" + (f"  [dim]{label}[/dim]" if label else '')
        )

    def _format_tree(self, root_tid: Optional[str] = None) -> str:
        def _render_tree(tid: str, prefix: str = '', is_last: bool = True, out: Optional[List[str]] = None):
            if out is None:
                out = []
            is_root = (tid == (root_tid or tid))
            connector = '└─ ' if is_last else '├─ '
            indent_next = '   ' if is_last else '│  '
            base_line = self._format_thread_line(tid)
            # For now, we rely on the short id printed by
            # _format_thread_line; we no longer append the full tid here
            # to keep the Children panel concise.
            out.append(prefix + connector + base_line)
            try:
                kids = [cid for cid, _n, _r, _c in list_children_with_meta(self.db, tid)]
            except Exception:
                kids = []
            for i, cid in enumerate(kids):
                last = (i == len(kids) - 1)
                _render_tree(cid, prefix + indent_next, last, out)
            return out
        roots = [root_tid] if root_tid else list_root_threads(self.db)
        lines: List[str] = []
        if not roots:
            return 'No threads.'
        for rid in roots:
            lines.extend(_render_tree(rid))
        return "\n".join(lines)

    def _format_messages_text(self, thread_id: str) -> str:
        msgs = _snapshot_messages(self.db, thread_id)
        lines: List[str] = []
        if not msgs:
            return "No messages yet."
        for m in msgs:
            role = m.get('role')
            if role == 'assistant':
                reas = (m.get('reasoning') or m.get('reasoning_content') or '').strip()
                if reas:
                    lines.append(f"[Reasoning]\n{reas}")
                content = (m.get('content') or '').strip()
                if content:
                    lines.append(f"[Assistant]\n{content}")
                # Final tool calls summary (if any)
                tcs = m.get('tool_calls') or []
                if isinstance(tcs, list) and tcs:
                    for tc in tcs:
                        f = (tc or {}).get('function') or {}
                        name = f.get('name') or ''
                        args = f.get('arguments')
                        try:
                            args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args or '')
                        except Exception:
                            args_str = str(args or '')
                        lines.append(f"[ToolCall] {name} {args_str}")
                # Streamed-only metadata (if snapshot captured)
                tstream = m.get('tool_stream') or {}
                if isinstance(tstream, dict):
                    for nm, txt in tstream.items():
                        if txt:
                            lines.append(f"[Tool Output: {nm}]\n{txt}")
                tc_stream = m.get('tool_calls_stream') or {}
                if isinstance(tc_stream, dict):
                    for nm, txt in tc_stream.items():
                        if txt:
                            lines.append(f"[Tool Call Args: {nm}]\n{txt}")
            elif role == 'user':
                content = (m.get('content') or '').strip()
                if content:
                    lines.append(f"[User]\n{content}")
            elif role == 'tool':
                # Distinguish between genuine assistant tool outputs and
                # user-initiated command outputs that are stored as
                # role="tool" with user_tool_call flag.
                if m.get('user_tool_call'):
                    name = m.get('name') or 'user_command'
                else:
                    name = m.get('name') or 'tool'
                content = (m.get('content') or '').strip()
                if content:
                    lines.append(f"[Tool: {name}]\n{content}")
            elif role == 'system':
                content = (m.get('content') or '').strip()
                if content:
                    lines.append(f"[System]\n{content}")
        return "\n\n".join(lines)

    # ---------------- Chat panel caching ----------------
    def _truncate_for_chat_panel(self, text: str, max_lines: int = 100) -> str:
        """Return a shortened view of ``text`` suitable for the chat panel.

        The static console view printed by ``_print_static_view_current``
        continues to render the *full* history; this helper only affects the
        scrolling "Chat Messages" panel inside the live UI.
        """
        if not text:
            return "No messages yet."
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        omitted = len(lines) - max_lines
        tail = lines[-max_lines:]
        notice = (
            f"... ({omitted} earlier lines omitted from Chat Messages; use /threads or "
            f"static view for full history) ..."
        )
        return "\n".join([notice] + tail)

    def _rebuild_chat_cache_for_current(self) -> None:
        """Ensure the cached base chat text for the current thread is fresh.

        We key the cache by (thread_id, snapshot_last_event_seq) so that we
        only walk the full snapshot when it actually changes. This eliminates
        a large amount of idle CPU work on long threads, where repeatedly
        calling ``_format_messages_text`` every 100ms used to be expensive.
        """
        # Lazily create cache dict the first time we are called.
        if not hasattr(self, "_chat_cache"):
            self._chat_cache = {
                "thread_id": None,
                "snapshot_seq": -1,
                "base_full": "",
                "base_tail": "",
            }

        try:
            th = self.db.get_thread(self.current_thread)
        except Exception:
            th = None
        snap_seq = getattr(th, "snapshot_last_event_seq", -1) if th else -1

        if (
            self._chat_cache.get("thread_id") == self.current_thread
            and self._chat_cache.get("snapshot_seq") == snap_seq
        ):
            return

        base_full = self._format_messages_text(self.current_thread)
        base_tail = self._truncate_for_chat_panel(base_full)
        self._chat_cache = {
            "thread_id": self.current_thread,
            "snapshot_seq": snap_seq,
            "base_full": base_full,
            "base_tail": base_tail,
        }

    def _compose_chat_panel_text(self) -> str:
        """Compose the text for the Chat Messages panel.

        Earlier versions of Egg eagerly rebuilt the snapshot on every UI
        refresh whenever the thread was "idle" (no open stream) by calling
        ``create_snapshot`` here. Snapshot building walks *all* events for the
        thread and JSON-decodes each payload; doing that 5–10 times per second
        on a long-running thread is surprisingly expensive and shows up as
        high CPU usage even when nothing new is happening.

        Snapshots are already maintained in cheaper places:
          - right after we append a user message or user command,
          - in the watcher when new msg.create/edit/delete events arrive,
          - when switching threads (/thread, /child, /new) via
            ``_print_static_view_current``.

        So we avoid rebuilding here and just read whatever snapshot is
        present. This keeps the chat panel up to date while eliminating a
        large amount of idle CPU work.

        Additionally, we cache the formatted snapshot text per
        (thread_id, snapshot_last_event_seq) so that we don't repeatedly walk
        and format a large history on every UI refresh when the snapshot
        hasn't changed.
        """
        # Ensure cache is up to date for the current thread / snapshot.
        self._rebuild_chat_cache_for_current()
        base = self._chat_cache.get("base_tail", "No messages yet.")
        ls = self._live_state
        parts: List[str] = [base]
        if ls.get('active_invoke'):
            if ls.get('reason'):
                parts.append(f"\n[Reasoning (streaming)]\n{ls['reason']}")
            for pk in ls.get('tc_order') or []:
                delta = (ls.get('tc_text') or {}).get(pk, '')
                if delta:
                    parts.append(f"\n[Tool Call Args: {pk}]\n{delta}")
            for name, txt in (ls.get('tools') or {}).items():
                if txt:
                    parts.append(f"\n[Tool: {name} (streaming)]\n{txt}")
            if ls.get('content'):
                parts.append(f"\n[Assistant (streaming)]\n{ls['content']}")

        head = f"Thread {self.current_thread[-8:]} | Model: {self._current_model_for_thread(self.current_thread) or 'default'}"

        # Combine historical + streaming text. The historical part is already
        # truncated to a tail window by ``_truncate_for_chat_panel`` so we do
        # not need to re-apply truncation here; streaming contributions are
        # typically small.
        body = "\n".join(parts).strip() or "No messages yet."

        return head + "\n" + body

    def _update_panels(self) -> None:
        self.chat_output.set_content(self._compose_chat_panel_text())
        status_lines = [
            f"Current: {self.current_thread[-8:]} | Roots with schedulers: {len(self.active_schedulers)}",
            "Send: Enter or Ctrl+D | New line: Ctrl+J | Clear: Ctrl+E | Quit: Ctrl+C",
            "Commands: /help /threads /thread <sel> /new [name] /spawn <text> /spawn_auto <text> /children /child <patt> /parent /delete <sel> /pause /resume /model [key] /updateAllModels <prov> /schedulers /enterMode /toggle_auto_approval /toolson /toolsoff /disabletool <name> /enabletool <name> /toolstatus /quit",
        ]
        tail = "\n".join(self._system_log[-20:]) if self._system_log else ""
        self.system_output.set_content("\n".join(status_lines + (["", tail] if tail else [])))

        # Children panel: refresh at most once every 2 seconds
        try:
            now = time.time()
            if now - self._last_children_refresh >= 2.0:
                try:
                    subtree_text = self._format_tree(self.current_thread)
                except Exception:
                    subtree_text = "(error rendering children tree)"
                self.children_output.set_content(subtree_text)
                self._last_children_refresh = now
        except Exception:
            pass

        # Update approval panel content based on pending prompt. This
        # panel is rendered between the output panels and the input
        # panel so that approval requests are visually close to where
        # the user types a y/n/o answer.
        pending = getattr(self, '_pending_prompt', {}) or {}
        if pending:
            kind = pending.get('kind')
            msg_lines: List[str] = []
            if kind == 'exec':
                msg_lines.append("[yellow]Execution approval needed.[/yellow]")
                msg_lines.append(
                    "[yellow]Type 'y' to approve, 'n' to deny, or 'a' to approve all "
                    "tool calls for this user turn, then press Enter.[/yellow]"
                )
            elif kind == 'output':
                msg_lines.append("[yellow]Output approval needed.[/yellow]")
                msg_lines.append("[yellow]Type 'y' to include full output, 'n' for a shortened preview, or 'o' to omit, then press Enter.[/yellow]")
            self.approval_panel.set_content("\n".join(msg_lines))
        else:
            # Empty content makes the panel effectively invisible
            self.approval_panel.set_content("")



    def _compute_pending_prompt(self) -> None:
        """Compute whether there is an execution or output approval pending
        for the current thread and update self._pending_prompt accordingly.

        - Execution approval (TC1) is only for assistant tool calls.
        - Output approval (TC4) is for any tool call with finished_output
          present and no output_approval yet.

        To avoid spamming the System panel, we only log when the
        pending prompt *changes* (kind or ids).
        """
        try:
            from eggthreads import list_tool_calls_for_thread, thread_state
        except Exception:
            self._pending_prompt = {}
            return

        old = getattr(self, '_pending_prompt', {}) or {}
        new = {}
        try:
            st = thread_state(self.db, self.current_thread)
        except Exception:
            st = 'unknown'

        if st in ('waiting_tool_approval', 'waiting_output_approval'):
            try:
                tcs = list_tool_calls_for_thread(self.db, self.current_thread)
            except Exception:
                tcs = []
            # Prefer execution approval first
            exec_needed = [tc for tc in tcs if tc.state == 'TC1']
            if exec_needed:
                ids = [tc.tool_call_id for tc in exec_needed]
                new = {'kind': 'exec', 'tool_call_ids': ids}
            else:
                # Otherwise, check output approval for finished tool calls
                out_needed = [tc for tc in tcs if tc.state == 'TC4' and tc.finished_output]
                if out_needed:
                    ids = [tc.tool_call_id for tc in out_needed]
                    new = {'kind': 'output', 'tool_call_ids': ids}

        # Update and log only if changed
        if new != old:
            self._pending_prompt = new
            if not new:
                return
            if new.get('kind') == 'exec':
                self._log_system(
                    'Execution approval needed for some tool calls. '
                    'Type "y" to approve, "n" to deny, or "a" to approve all tool calls for this assistant turn.'
                )
            elif new.get('kind') == 'output':
                # Compose a size-aware prompt for the first pending long output.
                try:
                    from eggthreads import list_tool_calls_for_thread
                    tcs_all = list_tool_calls_for_thread(self.db, self.current_thread)
                except Exception:
                    tcs_all = []
                tc_for_msg = None
                ids = new.get('tool_call_ids') or []
                for tc in tcs_all:
                    if tc.tool_call_id in ids and tc.finished_output:
                        tc_for_msg = tc
                        break
                if tc_for_msg and isinstance(tc_for_msg.finished_output, str):
                    out = tc_for_msg.finished_output
                    line_count = len(out.splitlines())
                    char_count = len(out)
                    self._log_system(
                        f"This output is very long ({line_count} lines, {char_count} chars), "
                        "do you want to include all of it?([y]es/[n]o/[o]mit)"
                    )
                    preview = self._shorten_output_preview(out)
                    if preview:
                        self._log_system("Preview (shortened):\n" + preview)
                else:
                    # Fallback generic message if we cannot inspect the output.
                    self._log_system('Output approval needed for some tool calls. Type "y" to include, "n" to send a shortened preview, or "o" to omit.')

    def _shorten_output_preview(self, text: str, max_lines: int = 200, max_chars: int = 8000) -> str:
        """Return a shortened preview for very long tool outputs.

        This keeps at most max_lines and max_chars of content and appends
        an ellipsis notice when truncation occurs.
        """
        if not isinstance(text, str) or not text:
            return ""
        lines = text.splitlines()
        truncated = text
        if len(lines) > max_lines:
            truncated = "\n".join(lines[:max_lines])
        if len(truncated) > max_chars:
            truncated = truncated[:max_chars]
        if truncated != text:
            truncated = truncated.rstrip()
            truncated += "\n\n...[output truncated for preview]..."
        return truncated

    def _cancel_pending_tools_on_interrupt(self) -> None:
        """Best-effort cancellation of pending or running tool calls.

        Used by Ctrl+C handling to stop any user command or tool execution
        from continuing without quitting the app.

        Semantics:
          - TC1 (needs approval): auto-deny execution.
          - TC2.1 / TC3 / TC4: mark output decision as "omit" so that any
            eventual results are not surfaced to the model and do not
            require further approval.
        """
        try:
            from eggthreads import build_tool_call_states, create_snapshot  # type: ignore
        except Exception:
            return
        try:
            states = build_tool_call_states(self.db, self.current_thread)
        except Exception:
            return
        if not states:
            return
        import os as _os
        any_tool_msg = False
        for tcid, tc in states.items():
            try:
                # Skip tool calls that already have a published tool
                # message; they are protocol-complete.
                if getattr(tc, 'published', False):
                    continue

                tool_call_id = str(tcid)
                # TC1: needs approval -> deny execution entirely.
                if tc.state == 'TC1':
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.current_thread,
                        type_='tool_call.approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tool_call_id,
                            'decision': 'denied',
                            'reason': 'Cancelled by user via Ctrl+C',
                        },
                    )
                    # For assistant-originated tool calls, also emit a
                    # synthetic tool response so that every assistant
                    # message with tool_calls has a corresponding tool
                    # message, even though execution never ran.
                    if getattr(tc, 'parent_role', None) == 'assistant':
                        name = getattr(tc, 'name', '') or tool_call_id
                        content = (
                            f"Tool call '{name}' was cancelled before it ran. "
                            "Reason: cancelled by user via Ctrl+C."
                        )
                        payload = {
                            'role': 'tool',
                            'content': content,
                            'tool_call_id': tool_call_id,
                            'name': name,
                        }
                        self.db.append_event(
                            event_id=_os.urandom(10).hex(),
                            thread_id=self.current_thread,
                            type_='msg.create',
                            msg_id=_os.urandom(10).hex(),
                            invoke_id=None,
                            payload=payload,
                        )
                        any_tool_msg = True
                # TC2.1 (approved), TC3 (executing), TC4 (finished, waiting
                # for output approval): mark output as omitted so the
                # runner will not auto-approve or surface it.
                elif tc.state in ('TC2.1', 'TC3', 'TC4'):
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.current_thread,
                        type_='tool_call.output_approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tool_call_id,
                            'decision': 'omit',
                            'reason': 'Cancelled by user via Ctrl+C',
                            'preview': 'Output omitted (cancelled by user).',
                        },
                    )
                    # Assistant-originated tool calls should still get a
                    # tool message so that the tools protocol invariant
                    # holds and future LLM calls do not fail with
                    # missing responses for tool_call_ids.
                    if getattr(tc, 'parent_role', None) == 'assistant':
                        name = getattr(tc, 'name', '') or tool_call_id
                        content = (
                            f"Tool call '{name}' was interrupted and its output was omitted. "
                            "Reason: cancelled by user via Ctrl+C."
                        )
                        payload = {
                            'role': 'tool',
                            'content': content,
                            'tool_call_id': tool_call_id,
                            'name': name,
                        }
                        self.db.append_event(
                            event_id=_os.urandom(10).hex(),
                            thread_id=self.current_thread,
                            type_='msg.create',
                            msg_id=_os.urandom(10).hex(),
                            invoke_id=None,
                            payload=payload,
                        )
                        any_tool_msg = True
            except Exception:
                continue

        # If we synthesized any tool messages, rebuild the snapshot so
        # that subsequent LLM turns see a history that already contains
        # those tool responses. This avoids provider errors complaining
        # about missing tool messages for previously declared
        # tool_call_ids.
        if any_tool_msg:
            try:
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass

    def _render_group(self) -> Group:
        # Left: chat; right: vertical stack of system + children panels
        right = VStack([self.system_output, self.children_output]).render()
        row1 = HStack([self.chat_output, right]).render()
        # Compose rows: top output row, optional approval panel, then input.
        children: List[Any] = [row1]
        pending = getattr(self, '_pending_prompt', {}) or {}
        # Only render the approval panel when there is a pending prompt
        # and it has non-empty content. Otherwise, omit it entirely so it
        # visually disappears from the layout.
        if pending and getattr(self.approval_panel, 'content', ''):
            children.append(self.approval_panel.render())
        children.append(self.input_panel.render())
        return Group(*children)

    def _log_system(self, msg: str) -> None:
        if not hasattr(self, '_system_log'):
            self._system_log = []
        self._system_log.append(msg)

    # ---------------- Console rendering of finished messages ----------------
    def _looks_markdown(self, content: str) -> bool:
        if not content:
            return False
        indicators = ['```', '# ', '## ', '### ', '* ', '- ', '> ', '`']
        hits = sum(1 for i in indicators if i in content)
        if hits >= 2:
            return True
        if content.count('\n') >= 2 and hits >= 1:
            return True
        return False

    def _console_print_message(self, m: Dict[str, Any]) -> None:
        # Enrich titles with msg_id and timestamp when available so that
        # static views are easier to correlate with events in the DB.
        from datetime import datetime

        def _fmt_ts(val: Any) -> str:
            if not val:
                return ""
            s = str(val)
            # SQLite ts is typically ISO-like (e.g. '2024-01-01T12:34:56.789Z')
            # Try a few common formats; fall back to raw string on failure.
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s, fmt)
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
            return s

        role = m.get('role')
        content = (m.get('content') or '').strip()
        model_key = (m.get('model_key') or '').strip()
        msg_id = m.get('msg_id') or ''
        ts_str = _fmt_ts(m.get('ts'))

        def _panel(renderable, title: str, border: str):
            # Build a unified title with optional timestamp and msg_id
            parts = [title]
            if ts_str:
                parts.append(f"[dim]{ts_str}[/dim]")
            if msg_id:
                parts.append(f"[dim]{msg_id[-8:]}[/dim]")
            full_title = " | ".join(parts)
            try:
                self.console.print(Panel(renderable, title=full_title, border_style=border))
            except Exception:
                # Fallback to plain text if Panel fails for any reason
                self.console.print(f"[{border}]{full_title}[/] {getattr(renderable, 'plain', str(renderable))}")

        if role == 'system':
            title = '[bold blue]System[/bold blue]'
            if isinstance(content, str) and content.lower().startswith('llm error:'):
                title = '[bold red]Error[/bold red]'
                _panel(Text(content, no_wrap=False, overflow='fold', style='red'), title, 'red')
                return
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            _panel(Text(content, no_wrap=False, overflow='fold', style='blue'), title, 'blue')
            return

        if role == 'user':
            title = '[bold green]User[/bold green]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            _panel(Text(content, no_wrap=False, overflow='fold', style='green'), title, 'green')
            return

        if role == 'assistant':
            title = '[bold cyan]Assistant[/bold cyan]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            # Prefer to show reasoning first if present
            reas = m.get('reasoning') or m.get('reasoning_content')
            if isinstance(reas, str) and reas.strip():
                reason_title = '[bold magenta]Reasoning[/bold magenta]'
                if model_key:
                    reason_title += f" [dim](model: {model_key})[/dim]"
                _panel(Text(reas, no_wrap=False, overflow='fold'), reason_title, 'magenta')
            if content:
                if self._looks_markdown(content):
                    _panel(Markdown(content), title, 'cyan')
                else:
                    _panel(Text(content, no_wrap=False, overflow='fold', style='cyan'), title, 'cyan')
            # Tool-calls summary if present
            tcs = m.get('tool_calls')
            if isinstance(tcs, list) and tcs:
                lines = []
                for tc in tcs:
                    f = (tc or {}).get('function') or {}
                    name = f.get('name') or (tc or {}).get('name') or 'function'
                    args = f.get('arguments') or (tc or {}).get('arguments')
                    if isinstance(args, (dict, list)):
                        try:
                            args_str = json.dumps(args, ensure_ascii=False)
                        except Exception:
                            args_str = str(args)
                    else:
                        args_str = str(args or '')
                    lines.append(f"{name}({args_str})")
                self.console.print(Panel(Text("\n".join(lines), no_wrap=False, overflow='fold'), title='Tool Calls', border_style='yellow'))
            # Streamed-only metadata if present in snapshot (optional)
            tstream = m.get('tool_stream') or {}
            if isinstance(tstream, dict) and tstream:
                for nm, txt in tstream.items():
                    if txt:
                        self.console.print(Panel(Text(txt, no_wrap=False, overflow='fold'), title=f'Tool Output: {nm}', border_style='yellow'))
            tc_stream = m.get('tool_calls_stream') or {}
            if isinstance(tc_stream, dict) and tc_stream:
                for nm, txt in tc_stream.items():
                    if txt:
                        self.console.print(Panel(Text(txt, no_wrap=False, overflow='fold'), title=f'Tool Call Args (streamed): {nm}', border_style='yellow'))
            return

        if role == 'tool':
            name = m.get('name') or 'Tool'
            title = f'[bold yellow]{name}[/bold yellow]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            _panel(Text(content, no_wrap=False, overflow='fold', style='yellow'), title, 'yellow')
            return

        # Fallback generic
        title = (role or 'Message')
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        _panel(Text(content, no_wrap=False, overflow='fold', style='blue'), title, 'blue')

    def _print_static_view_current(self, heading: Optional[str] = None) -> None:
        # Print a static view of recent messages for the selected thread.
        # Only take a fresh snapshot if the thread is not currently
        # streaming. For streaming threads we want to keep
        # snapshot_last_event_seq at the last completed message so that
        # any in-flight stream can be fully reconstructed from its
        # stream.delta events when attaching a watcher mid-stream.
        tid = self.current_thread
        try:
            row = self.db.current_open(tid)
        except Exception:
            row = None
        if row is None:
            try:
                create_snapshot(self.db, tid)
            except Exception:
                pass
        if heading:
            try:
                self.console.print(Panel(heading, border_style='blue'))
            except Exception:
                self.console.print(heading)
        msgs = _snapshot_messages(self.db, tid)
        if not msgs:
            self.console.print(Panel('[dim]No messages yet[/dim]', border_style='blue'))
        else:
            for m in msgs:
                if isinstance(m, dict):
                    self._console_print_message(m)
        # Update last-printed seq to the latest message event so we don't re-print
        try:
            row = self.db.conn.execute(
                "SELECT MAX(event_seq) FROM events WHERE thread_id=? AND type='msg.create'",
                (tid,)
            ).fetchone()
            last = int(row[0]) if row and row[0] is not None else -1
            self._last_printed_seq_by_thread[tid] = last
        except Exception:
            self._last_printed_seq_by_thread[tid] = self._last_printed_seq_by_thread.get(tid, -1)

    def _console_print_block(self, title: str, text: str, border_style: str = 'blue') -> None:
        try:
            # Parse rich markup within the text for colored segments
            self.console.print(Panel(Text.from_markup(text), title=title, border_style=border_style))
        except Exception:
            # Fallback plain
            self.console.print(f"{title}\n{text}")

    # ---------------- Input and commands ----------------
    def _handle_key(self, key: str) -> bool:
        # Ctrl+D sends, Ctrl+E clears input, Ctrl+C exits
        try:
            import readchar  # type: ignore
            ctrl_d = getattr(readchar.key, 'CTRL_D', '\x04')
            ctrl_c = getattr(readchar.key, 'CTRL_C', '\x03')
            ctrl_e = getattr(readchar.key, 'CTRL_E', '\x05')
            enter_key = getattr(readchar.key, 'ENTER', '\r')
        except Exception:
            ctrl_d = '\x04'
            ctrl_c = '\x03'
            ctrl_e = '\x05'
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
                self._log_system(f"Esc-like key received: {repr(key)}")
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
                    self._cancel_pending_tools_on_interrupt()
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
                self._log_system('Interrupted current stream/tool execution with Ctrl+C (thread remains open).')
                # Recompute any approval prompts after cancellation
                self._compute_pending_prompt()
                return True

            # No active work. If there's text in the input panel, clear it.
            if not text_empty:
                self.input_panel.clear_text()
                try:
                    self._log_system('Input cleared with Ctrl+C (press Ctrl+C again on empty input to quit).')
                except Exception:
                    pass
                return True

            # Idle thread and empty input -> quit.
            self._log_system('Exiting on Ctrl+C (no active work and empty input).')
            self.running = False
            return False
        # Send on Ctrl+D always (but if we have a pending approval
        # prompt, interpret it as an approval answer first, regardless
        # of /enterMode).
        if key == ctrl_d or key == '\x04':
            # First, try to handle any pending approval using the current
            # input text as the answer. This works in both /enterMode
            # send and newline.
            if self._handle_pending_approval_answer(self.input_panel.get_text(), source='Ctrl+D'):
                return True
            # No pending approval (or unrecognized answer), treat Ctrl+D
            # as normal send.
            text = self.input_panel.get_text().strip()
            if text:
                try:
                    self._on_submit(text)
                except Exception as e:
                    self._log_system(f"Submit error: {e}")
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
            return True
        # Clear input on Ctrl+E
        if key == ctrl_e or key == '\x05':
            self.input_panel.clear_text()
            try:
                self._log_system('Input cleared.')
            except Exception:
                pass
            return True
        # Enter behavior depends on mode
        if key in (enter_key, '\r', '\n'):
            # If we have a pending approval prompt and Enter-sends mode
            # is active, interpret y/n/o/a answers via the same helper as
            # Ctrl+D before treating Enter as a normal send.
            if self.enter_sends and self._handle_pending_approval_answer(self.input_panel.get_text(), source='Enter'):
                return True
            if self.enter_sends:
                text = self.input_panel.get_text().strip()
                if text:
                    try:
                        self._on_submit(text)
                    except Exception as e:
                        self._log_system(f"Submit error: {e}")
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

    def _handle_pending_approval_answer(self, raw_text: str, source: str = 'Enter') -> bool:
        """Handle a pending tool approval/output-approval answer.

        raw_text: current input text (will be stripped/lowered)
        source: human-readable origin (e.g. 'Enter', 'Ctrl+D') for logging.

        Returns True if an approval was handled and the prompt/input were
        cleared, False otherwise.
        """
        try:
            pending = getattr(self, '_pending_prompt', {}) or {}
        except Exception:
            pending = {}
        if not pending:
            return False
        txt = (raw_text or '').strip().lower()
        if not txt:
            return False
        kind = pending.get('kind')
        ids = pending.get('tool_call_ids') or []
        try:
            import os as _os
            from eggthreads import build_tool_call_states
        except Exception:
            ids = []
        # Exec approval: y = approve this set, n = deny this set,
        # a = approve all tool calls in this user turn (RA2 and RA3)
        if kind == 'exec' and ids and txt in ('y', 'n', 'a'):
            try:
                if txt in ('y', 'n'):
                    approve = (txt == 'y')
                    decision = 'granted' if approve else 'denied'
                    for tcid in ids:
                        self.db.append_event(
                            event_id=_os.urandom(10).hex(),
                            thread_id=self.current_thread,
                            type_='tool_call.approval',
                            msg_id=None,
                            invoke_id=None,
                            payload={
                                'tool_call_id': tcid,
                                'decision': decision,
                                'reason': f'Approved/denied by user from UI ({source})',
                            },
                        )
                    self._log_system(f"Tool calls {ids} approval decision: {decision}.")
                else:  # txt == 'a'
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.current_thread,
                        type_='tool_call.approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'decision': 'all-in-turn',
                            'reason': f'Approved by user from UI ({source})',
                        },
                    )
                    self._log_system(
                        f"Approved all tool calls for this user turn (decision=all-in-turn, via {source})."
                    )
            except Exception as e:
                self._log_system(f'Error recording approval: {e}')
            self._pending_prompt = {}
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
            return True
        # Output approval for very long tool outputs:
        # y -> whole, n -> shortened preview, o -> omit.
        if kind == 'output' and ids and txt in ('y', 'n', 'o'):
            try:
                states = build_tool_call_states(self.db, self.current_thread)
                if txt == 'y':
                    decision = 'whole'
                elif txt == 'n':
                    decision = 'partial'
                else:
                    decision = 'omit'
                for tcid in ids:
                    tc = states.get(str(tcid))
                    if not tc or not tc.finished_output:
                        continue
                    full = tc.finished_output
                    if not isinstance(full, str):
                        full = str(full)
                    line_count = len(full.splitlines())
                    char_count = len(full)
                    if decision == 'whole':
                        preview = full
                    elif decision == 'partial':
                        preview = self._shorten_output_preview(full)
                    else:
                        preview = "Output omitted."
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.current_thread,
                        type_='tool_call.output_approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tcid,
                            'decision': decision,
                            'reason': f'User decided in UI ({source})',
                            'preview': preview,
                            'line_count': line_count,
                            'char_count': char_count,
                        },
                    )
                self._log_system(f"Tool calls {ids} output decision: {decision} (via {source}).")
            except Exception as e:
                self._log_system(f'Error recording approval: {e}')
            self._pending_prompt = {}
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
            return True
        return False

    def _on_submit(self, text: str) -> None:
        # User command execution ($ / $$) is modeled as a user-originated
        # tool call (RA3). We enqueue a user message with tool_calls and
        # an automatic tool_call.approval so that the ThreadRunner executes
        # the command via the bash tool under the normal tool-call state
        # machine, rather than running it locally in the UI.
        if text.startswith('$$') and len(text) > 2:
            self._enqueue_bash_tool(text[2:].strip(), hidden=True)
            return
        if text.startswith('$') and len(text) > 1:
            self._enqueue_bash_tool(text[1:].strip(), hidden=False)
            return
        if text.startswith('/'):
            self._handle_command(text)
            return
        append_message(self.db, self.current_thread, 'user', text)
        create_snapshot(self.db, self.current_thread)
        self._ensure_scheduler_for(self.current_thread)
        self._log_system("User message queued; scheduler will stream the response.")


    def _enqueue_bash_tool(self, script: str, hidden: bool) -> None:
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
            self._log_system('Empty bash command, skipping.')
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
            self.db.append_event(
                event_id=_os.urandom(10).hex(),
                thread_id=self.current_thread,
                type_='tool_call.approval',
                msg_id=None,
                invoke_id=None,
                payload={
                    'tool_call_id': tc_id,
                    'decision': 'granted',
                    'reason': 'Approved as user-initiated command',
                },
            )
        except Exception as e:
            self._log_system(f'Error recording tool_call.approval for bash: {e}')
        # Snapshot and ensure scheduler so that RA3 will pick this up.
        try:
            create_snapshot(self.db, self.current_thread)
        except Exception:
            pass
        self._ensure_scheduler_for(self.current_thread)
        self._log_system(f"Queued bash command as tool_call {tc_id[-6:]} (hidden={hidden}).")

    def _run_shell(self, bash_command: str, keep_user_turn: bool, hidden: bool) -> None:
        import subprocess
        try:
            res = subprocess.run(bash_command, shell=True, capture_output=True, text=True, cwd=os.getcwd())
            output = res.stdout or ''
            if res.stderr:
                output += f"\nSTDERR:\n{res.stderr}"
            if res.returncode != 0:
                output += f"\nReturn code: {res.returncode}"
            message_content = f"Command: {bash_command}\n\nOutput:\n{output}"
            extra = {'keep_user_turn': keep_user_turn}
            if hidden:
                extra['no_api'] = True
            append_message(self.db, self.current_thread, 'user', message_content, extra=extra)
            create_snapshot(self.db, self.current_thread)
            self._log_system(("(hidden) " if hidden else "") + f"Executed: {bash_command}")
        except Exception as e:
            err = f"Error executing command: {e}"
            append_message(self.db, self.current_thread, 'user', f"Command: {bash_command}\n\nError: {err}", extra={'keep_user_turn': keep_user_turn})
            create_snapshot(self.db, self.current_thread)
            self._log_system(err)

    def _handle_command(self, text: str) -> None:
        parts = text[1:].split(None, 1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ''
        if cmd == 'help':
            self._log_system(
                'Commands: '
                '/model <key>, /updateAllModels <provider>, /pause, /resume, '
                '/spawn <text>, /spawn_auto <text>, /wait <threads>, '
                '/child <pattern>, /parent, /children, /threads, /thread <selector>, /delete <selector>, /new <name>, /dup [name], '
                '/schedulers, /enterMode <send|newline>, /toggle_auto_approval, '
                '/toolson, /toolsoff, /disabletool <name>, /enabletool <name>, /toolstatus, /quit'
            )
        elif cmd == 'quit':
            self.running = False
        elif cmd == 'pause':
            pause_thread(self.db, self.current_thread)
            self._log_system('Paused current thread')
        elif cmd == 'resume':
            resume_thread(self.db, self.current_thread)
            self._log_system('Resumed current thread')
        elif cmd == 'new':
            new_name = (arg or '').strip() or 'Root'
            cur_model_key = self._current_model_for_thread(self.current_thread) or None
            new_root = create_root_thread(self.db, name=new_name, initial_model_key=cur_model_key)
            append_message(self.db, new_root, 'system', self.system_prompt)
            create_snapshot(self.db, new_root)
            self._ensure_scheduler_for(new_root)
            self.current_thread = new_root
            asyncio.get_running_loop().create_task(self._start_watching_current())
            self._log_system(f"Created new root thread: {new_root[-8:]}")
            self._print_static_view_current(heading=f"Switched to thread: {self.current_thread}")
        elif cmd == 'spawn':
            # Use the spawn_agent tool implementation from eggthreads so we
            # share the same semantics between UI (/spawn) and model tools.
            def _latest_model_for_thread(tid: str) -> Optional[str]:
                # Mirror _current_model_for_thread so that spawned
                # children inherit the same effective model as their
                # parent thread.
                try:
                    from eggthreads import current_thread_model  # type: ignore
                    return current_thread_model(self.db, tid)
                except Exception:
                    th = self.db.get_thread(tid)
                    imk = th.initial_model_key if th and isinstance(th.initial_model_key, str) else None
                    return imk.strip() if imk and imk.strip() else None

            cur_model = _latest_model_for_thread(self.current_thread)

            # Import ToolRegistry/create_default_tools in a local scope to
            # avoid circular imports at module import time.
            try:
                from eggthreads.tools import create_default_tools  # type: ignore
                tools = create_default_tools()
                # Ensure spawn_agent exists; if not, this will raise.
                args = {
                    # Parent is this UI thread; we pass it explicitly so
                    # spawn_agent behaves the same as model-initiated calls
                    # that receive thread_id via the runner context.
                    'parent_thread_id': self.current_thread,
                    'context_text': arg or 'Spawned task',
                    'label': 'spawn',
                    'system_prompt': self.system_prompt,
                }
                if cur_model:
                    args['initial_model_key'] = cur_model
                # When called directly from the UI, we do not rely on the
                # implicit _thread_id injection and pass parent id
                # explicitly.
                res = tools.execute('spawn_agent', args)
            except Exception as e:
                self._log_system(f"/spawn error: {e}")
                return

            if not isinstance(res, str):
                self._log_system(f"/spawn returned non-string thread id: {res!r}")
                return

            child = res

            # Child thread now exists and has the system prompt + user
            # message and optional model marker already seeded by the
            # tool. We only need to ensure a scheduler is running and
            # log the result.
            self._ensure_scheduler_for(child)
            self._log_system(f"Spawned thread: {child[-8:]}")

            # Also append a user-visible command output message so the
            # spawned thread id becomes part of the conversation
            # context, similar to other user commands.
            try:
                cmd_text = text.strip()  # e.g. "/spawn tell me a story"
                msg_content = f"Command: {cmd_text}\n\nOutput:\n{child}"
                append_message(self.db, self.current_thread, 'user', msg_content, extra={'keep_user_turn': True})
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass
        elif cmd == 'spawn_auto':
            # Same as /spawn, but use spawn_agent_auto so the spawned
            # child has global tool auto-approval.
            def _latest_model_for_thread(tid: str) -> Optional[str]:
                # Mirror _current_model_for_thread so that spawned
                # children inherit the same effective model as their
                # parent thread.
                try:
                    from eggthreads import current_thread_model  # type: ignore
                    return current_thread_model(self.db, tid)
                except Exception:
                    th = self.db.get_thread(tid)
                    imk = th.initial_model_key if th and isinstance(th.initial_model_key, str) else None
                    return imk.strip() if imk and imk.strip() else None

            cur_model = _latest_model_for_thread(self.current_thread)

            try:
                from eggthreads.tools import create_default_tools  # type: ignore
                tools = create_default_tools()
                args = {
                    'parent_thread_id': self.current_thread,
                    'context_text': arg or 'Spawned task',
                    'label': 'spawn_auto',
                    'system_prompt': self.system_prompt,
                }
                if cur_model:
                    args['initial_model_key'] = cur_model
                res = tools.execute('spawn_agent_auto', args)
            except Exception as e:
                self._log_system(f"/spawn_auto error: {e}")
                return

            if not isinstance(res, str):
                self._log_system(f"/spawn_auto returned non-string thread id: {res!r}")
                return

            child = res
            self._ensure_scheduler_for(child)
            self._log_system(f"Spawned auto-approval thread: {child[-8:]}")
        elif cmd == 'wait':
            # Treat /wait as a user command that enqueues a wait tool
            # call (RA3). The argument is a space-separated list of
            # thread selectors; use the same resolution logic as /thread
            # (via _resolve_single_thread_selector) for maximum DRYness.

            arg_txt = (arg or '').strip()
            if not arg_txt:
                self._log_system('Usage: /wait <thread-id|suffix|name|recap-fragment>[,more...]')
                return

            # Support comma- or whitespace-separated selectors, e.g.
            #   /wait abc,def ghi
            # becomes selectors ['abc', 'def', 'ghi'].
            selectors = [s for s in re.split(r'[\s,]+', arg_txt) if s]
            resolved: list[str] = []
            for sel in selectors:
                tid = self._resolve_single_thread_selector(sel)
                if not tid:
                    self._log_system(f"/wait: no thread matches selector '{sel}'")
                    return
                resolved.append(tid)

            # Enqueue a wait tool call via the RA3 mechanism. We do not
            # hide it from the model by default; the model should see the
            # summary of the waited threads.
            import os as _os, json as _json
            tc_id = _os.urandom(8).hex()
            tool_call = {
                'id': tc_id,
                'type': 'function',
                'function': {
                    'name': 'wait',
                    'arguments': _json.dumps({'thread_ids': resolved}, ensure_ascii=False),
                },
            }
            extra = {
                'tool_calls': [tool_call],
                'keep_user_turn': True,
                'user_command_type': '/wait',
            }
            # Store the triggering user message and associated tool_calls
            msg_id = append_message(self.db, self.current_thread, 'user', f"/wait {arg_txt}", extra=extra)
            # Auto-approve this user-initiated tool call
            try:
                self.db.append_event(
                    event_id=_os.urandom(10).hex(),
                    thread_id=self.current_thread,
                    type_='tool_call.approval',
                    msg_id=None,
                    invoke_id=None,
                    payload={
                        'tool_call_id': tc_id,
                        'decision': 'granted',
                        'reason': 'Approved as user-initiated /wait command',
                    },
                )
            except Exception as e:
                self._log_system(f'Error recording tool_call.approval for wait: {e}')
            try:
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass
            self._ensure_scheduler_for(self.current_thread)
            self._log_system(f"Queued /wait for threads: {' '.join([tid[-8:] for tid in resolved])}.")
        elif cmd == 'children':
            sub = _get_subtree(self.db, self.current_thread)
            if not sub:
                self._log_system('No subthreads.')
            else:
                block = self._format_tree(self.current_thread)
                self._log_system('Subtree (see console for full):')
                self._console_print_block('Subtree', block, border_style='blue')
        elif cmd == 'child':
            patt = (arg or '').lower()
            rows = list_children_with_meta(self.db, self.current_thread)
            candidates: List[str] = []
            for child_id, name, recap, _created in rows:
                if not patt or patt in (name + ' ' + recap + ' ' + child_id).lower():
                    candidates.append(child_id)
            if candidates:
                self._ensure_scheduler_for(candidates[0])
                self.current_thread = candidates[0]
                asyncio.get_running_loop().create_task(self._start_watching_current())
                self._log_system(f"Switched to child: {self.current_thread[-8:]}")
                self._print_static_view_current(heading=f"Switched to thread: {self.current_thread}")
            else:
                self._log_system('No matching child.')
        elif cmd == 'parent':
            pid = get_parent(self.db, self.current_thread)
            if pid:
                self.current_thread = pid
                asyncio.get_running_loop().create_task(self._start_watching_current())
                self._log_system('Moved to parent thread')
                self._print_static_view_current(heading=f"Switched to thread: {self.current_thread}")
            else:
                self._log_system('Already at root or no parent found.')
        elif cmd == 'threads':
            try:
                text = self._format_tree()
                self._log_system('Threads by subtree (see console for full).')
                self._console_print_block('Threads', text, border_style='blue')
            except Exception as e:
                self._log_system(f"Error listing threads: {e}")
        elif cmd == 'thread':
            sel = (arg or '').strip()
            if not sel:
                self._log_system(f"Current thread: {self.current_thread}")
            else:
                matches = self._select_threads_by_selector(sel)
                if not matches and ' ' in sel:
                    sel_first = sel.split()[0]
                    matches = self._select_threads_by_selector(sel_first)
                if not matches:
                    try:
                        rows_all = list_threads(self.db)
                        suf = sel.lower()
                        matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
                    except Exception:
                        matches = []
                if not matches:
                    self._log_system(f"No thread matches selector: {sel}")
                else:
                    try:
                        rows = list_threads(self.db)
                        ca = {r.thread_id: r.created_at for r in rows}
                    except Exception:
                        ca = {}
                    matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
                    new_tid = matches[0]
                    self._ensure_scheduler_for(new_tid)
                    self.current_thread = new_tid
                    asyncio.get_running_loop().create_task(self._start_watching_current())
                    self._log_system(f"Switched to thread: {new_tid[-8:]}")
                    self._print_static_view_current(heading=f"Switched to thread: {self.current_thread}")
        elif cmd == 'delete':
            selector = (arg or '').strip()
            if not selector:
                self._log_system('Usage: /delete <thread-id|suffix|name|recap-fragment>')
                return
            matches = self._select_threads_by_selector(selector)
            if not matches and ' ' in selector:
                sel_first = selector.split()[0]
                matches = self._select_threads_by_selector(sel_first)
            if not matches:
                try:
                    rows_all = list_threads(self.db)
                    suf = selector.lower()
                    matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
                except Exception:
                    matches = []
            matches = [m for m in matches if m != self.current_thread]
            if not matches:
                self._log_system('No deletable thread matches selector.')
                return
            try:
                rows = list_threads(self.db)
                ca = {r.thread_id: r.created_at for r in rows}
            except Exception:
                ca = {}
            matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
            target_tid = matches[0]
            try:
                delete_thread(self.db, target_tid)
                self._log_system(f"Thread {target_tid[-8:]} deleted.")
            except Exception as e:
                self._log_system(f'Error deleting thread: {e}')
        elif cmd == 'dup':
            # Duplicate the current thread as a new root thread, acting
            # as a "checkpoint" copy of the entire conversation up to
            # this point. The new thread has the same history (events
            # and snapshot) but no open stream and no parent/children
            # links. This is useful for branching or backups.
            try:
                from eggthreads import duplicate_thread  # type: ignore
            except Exception as e:
                self._log_system(f'/dup not available: eggthreads import failed: {e}')
                return
            label = (arg or '').strip() or None
            try:
                new_tid = duplicate_thread(self.db, self.current_thread, name=label)
            except Exception as e:
                self._log_system(f'/dup error: {e}')
                return
            # Ensure a scheduler is running for the duplicate so it can
            # be continued independently if desired.
            self._ensure_scheduler_for(new_tid)
            self._log_system(f"Duplicated current thread to new root: {new_tid[-8:]}")
            # Switch to the duplicate so the user can inspect/continue it.
            self.current_thread = new_tid
            asyncio.get_running_loop().create_task(self._start_watching_current())
            self._print_static_view_current(heading=f"Switched to duplicated thread: {self.current_thread}")
        elif cmd == 'model':
            arg2 = (arg or '').strip()
            if arg2:
                # Record a model.switch event as the authoritative source
                # of model selection for this thread and append a
                # user-level notification that is excluded from LLM
                # context (no_api=True) but visible in the transcript.
                from eggthreads import set_thread_model  # type: ignore
                set_thread_model(self.db, self.current_thread, arg2, reason='ui /model')
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.current_thread,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload={
                        'role': 'user',
                        'content': f"/model {arg2}",
                        'no_api': True,
                    },
                )
                create_snapshot(self.db, self.current_thread)
                self._log_system(f"Model set to: {arg2}")
            else:
                try:
                    llm = self.llm_client
                    if not llm:
                        self._log_system('Models not available (llm client not initialized).')
                    else:
                        by_provider: Dict[str, List[str]] = {}
                        for name, cfg in (llm.registry.models_config or {}).items():
                            prov = cfg.get('provider', 'unknown')
                            by_provider.setdefault(prov, []).append(name)
                        lines = []
                        for prov in sorted(by_provider.keys()):
                            lines.append(f"{prov}:")
                            for m in sorted(by_provider[prov]):
                                lines.append(f"  - {m}")
                        lines.append("\nTip: use 'all:provider:model' to pick catalog models.")
                        self._log_system("Available models:\n" + "\n".join(lines))
                except Exception as e:
                    self._log_system(f"Error listing models: {e}")
        elif cmd == 'updateAllModels':
            provider = (arg or '').strip()
            if not provider:
                self._log_system('Usage: /updateAllModels <provider>')
            else:
                try:
                    # Prefer to use the long-lived LLM client instance
                    # so that its in-memory AllModelsCatalog is updated
                    # and autocomplete (/model all:...<tab>) immediately
                    # sees the new models. If no client is available in
                    # this UI, fall back to a temporary one.
                    if self.llm_client is not None:
                        res = self.llm_client.update_all_models(provider)
                    else:
                        if not LLMClient:
                            raise RuntimeError('eggllm not available')
                        llm_tmp = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH)
                        res = llm_tmp.update_all_models(provider)
                    self._log_system("Update All Models:\n" + res)
                except Exception as e:
                    self._log_system(f"Update All Models error: {e}")
        elif cmd == 'schedulers':
            if not self.active_schedulers:
                self._log_system('No active schedulers in this session.')
            else:
                out: List[str] = []
                for rid in self.active_schedulers.keys():
                    out.append(f"- root {rid[-8:]}")
                    out.append(self._format_tree(rid))
                block = "\n".join(out)
                self._log_system("Active SubtreeSchedulers (see console for full).")
                self._console_print_block('Schedulers', block, border_style='cyan')
        elif cmd == 'enterMode':
            mode = (arg or '').strip().lower()
            if mode in ('send', 's', 'on'):
                self.enter_sends = True
                self._log_system('Enter mode: send (Enter sends, Ctrl+D also sends).')
            elif mode in ('newline', 'n', 'off'):
                self.enter_sends = False
                self._log_system('Enter mode: newline (Enter inserts newline, Ctrl+D sends).')
            else:
                self._log_system('Usage: /enterMode <send|newline>')
        elif cmd == 'toggle_auto_approval':
            # Toggle per-thread global tool auto-approval by emitting a
            # tool_call.approval event with decision global_approval /
            # revoke_global_approval. This affects future tool calls in
            # this thread (both assistant- and user-originated).
            import os as _os
            try:
                from eggthreads import build_tool_call_states  # type: ignore
                from eggthreads import thread_state as _thread_state  # type: ignore
            except Exception:
                self._log_system('Auto-approval toggle not available (eggthreads import failed).')
                return
            # Heuristic: check whether there exists any approval event with
            # decision == 'global_approval' more recent than any
            # revoke_global_approval; since we don't persist this flag
            # separately, we simply toggle based on the last such event.
            try:
                cur = self.db.conn.execute(
                    "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval' ORDER BY event_seq ASC",
                    (self.current_thread,),
                )
                last_decision = None
                for (pj,) in cur.fetchall():
                    try:
                        payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
                    except Exception:
                        payload = {}
                    d = payload.get('decision')
                    if d in ('global_approval', 'revoke_global_approval'):
                        last_decision = d
                enable = (last_decision != 'global_approval')
            except Exception:
                enable = True

            decision = 'global_approval' if enable else 'revoke_global_approval'
            try:
                self.db.append_event(
                    event_id=_os.urandom(10).hex(),
                    thread_id=self.current_thread,
                    type_='tool_call.approval',
                    msg_id=None,
                    invoke_id=None,
                    payload={
                        'decision': decision,
                        'reason': 'Toggled by user via /toggle_auto_approval',
                    },
                )
                self._log_system(
                    'Global tool auto-approval ENABLED for this thread.' if enable
                    else 'Global tool auto-approval DISABLED for this thread.'
                )
            except Exception as e:
                self._log_system(f'Error toggling auto-approval: {e}')
        elif cmd == 'toolson':
            # Thread-wide toggle: allow RA1 to expose tools again.
            try:
                from eggthreads import set_thread_tools_enabled  # type: ignore
                set_thread_tools_enabled(self.db, self.current_thread, True)
                self._log_system('Tools enabled for this thread (LLM may call tools).')
            except Exception as e:
                self._log_system(f'/toolson error: {e}')
        elif cmd == 'toolsoff':
            # Thread-wide toggle: RA1 will not expose tools to the LLM
            # for this thread. User-initiated commands ($, $$, /wait)
            # still work as they are modelled as explicit tool calls.
            try:
                from eggthreads import set_thread_tools_enabled  # type: ignore
                set_thread_tools_enabled(self.db, self.current_thread, False)
                self._log_system('Tools disabled for this thread (LLM tool calls suppressed).')
            except Exception as e:
                self._log_system(f'/toolsoff error: {e}')
        elif cmd == 'disabletool':
            # Per-thread blacklist of individual tool names.
            name = (arg or '').strip()
            if not name:
                self._log_system('Usage: /disabletool <tool_name>')
                return
            try:
                from eggthreads import disable_tool_for_thread  # type: ignore
                disable_tool_for_thread(self.db, self.current_thread, name)
                self._log_system(f"Tool '{name}' disabled for this thread.")
            except Exception as e:
                self._log_system(f'/disabletool error: {e}')
        elif cmd == 'enabletool':
            name = (arg or '').strip()
            if not name:
                self._log_system('Usage: /enabletool <tool_name>')
                return
            try:
                from eggthreads import enable_tool_for_thread  # type: ignore
                enable_tool_for_thread(self.db, self.current_thread, name)
                self._log_system(f"Tool '{name}' enabled for this thread.")
            except Exception as e:
                self._log_system(f'/enabletool error: {e}')
        elif cmd == 'toolstatus':
            # Report effective tools configuration for the current
            # thread: whether LLM tools are enabled and which tools are
            # currently disabled.
            try:
                from eggthreads import get_thread_tools_config  # type: ignore
                cfg = get_thread_tools_config(self.db, self.current_thread)
            except Exception as e:
                self._log_system(f'/toolstatus error: {e}')
                return
            status = 'enabled' if cfg.llm_tools_enabled else 'disabled'
            disabled = sorted(cfg.disabled_tools) if cfg.disabled_tools else []
            lines = [
                f"Tools for this thread: {status}",
                "Disabled tools: " + (", ".join(disabled) if disabled else "(none)"),
            ]
            self._log_system("\n".join(lines))
        else:
            self._log_system('Unknown command')

    def _select_threads_by_selector(self, selector: str) -> List[str]:
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

    def _resolve_single_thread_selector(self, selector: str) -> Optional[str]:
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

        matches = self._select_threads_by_selector(sel)
        if not matches and ' ' in sel:
            sel_first = sel.split()[0]
            matches = self._select_threads_by_selector(sel_first)
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

    # ---------------- Watching & streaming ----------------
    async def _start_watching_current(self):
        if self._watch_task is not None:
            try:
                self._watch_task.cancel()
            except Exception:
                pass
        # Reset live streaming state when switching threads so that we
        # don't show stale streaming output from a previous thread while
        # we wait for the new thread's events to arrive.
        self._live_state = {
            "active_invoke": None,
            "content": "",
            "reason": "",
            "tools": {},
            "tc_text": {},
            "tc_order": [],
        }
        # When switching to a thread, compute any pending tool or output
        # approvals once so that the Approval panel reflects existing
        # state even if no new events arrive. We deliberately avoid doing
        # this on every UI tick in _update_panels() for performance.
        try:
            self._compute_pending_prompt()
        except Exception:
            pass
        self._watch_task = asyncio.create_task(self._watch_thread(self.current_thread))

    async def _watch_thread(self, thread_id: str):
        # Start from last snapshot event
        try:
            th = self.db.get_thread(thread_id)
            start_after = int(th.snapshot_last_event_seq) if th and isinstance(th.snapshot_last_event_seq, int) else -1
        except Exception:
            start_after = -1

        # Preload: if currently open stream, fold existing deltas into buffers
        try:
            row_open = self.db.current_open(thread_id)
        except Exception:
            row_open = None
        after_for_watch = start_after
        if row_open is not None:
            # There is an active stream for this thread. Initialize the
            # in-memory live_state so that any subsequent stream.delta
            # events (and any preloaded deltas below) are rendered as
            # "(streaming)" output, even if we joined the thread after
            # the stream had already started.
            try:
                self._live_state = {
                    "active_invoke": row_open["invoke_id"],
                    "content": "",
                    "reason": "",
                    "tools": {},
                    "tc_text": {},
                    "tc_order": [],
                }
            except Exception:
                # If anything goes wrong, we still proceed; the stream
                # deltas will be applied, we just might miss the
                # "active" flag for this session.
                pass
            try:
                cur = self.db.conn.execute(
                    "SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                    (thread_id, start_after)
                )
                for e in cur.fetchall():
                    after_for_watch = e["event_seq"] or after_for_watch
                    await self._ingest_event_for_live(e, thread_id)
            except Exception:
                pass

        # Poll a bit less aggressively to reduce idle CPU; EventWatcher
        # itself backs off further when idle.
        ew = EventWatcher(self.db, thread_id, after_seq=after_for_watch, poll_sec=0.1)
        async for batch in ew.aiter():
            saw_non_stream_msg = False
            for e in batch:
                try:
                    if e["type"] in ("msg.create", "msg.edit", "msg.delete"):
                        saw_non_stream_msg = True
                except Exception:
                    pass
                await self._ingest_event_for_live(e, thread_id)

            # If we saw message-level events, refresh snapshot to include them
            if saw_non_stream_msg:
                try:
                    create_snapshot(self.db, self.current_thread)
                except Exception:
                    pass
                # Print any new messages to console (above live panel)
                try:
                    last_printed = self._last_printed_seq_by_thread.get(self.current_thread, -1)
                    cur = self.db.conn.execute(
                        "SELECT event_seq, msg_id, ts, payload_json FROM events WHERE thread_id=? AND event_seq>? AND type='msg.create' ORDER BY event_seq ASC",
                        (self.current_thread, last_printed)
                    )
                    rows = cur.fetchall()
                    for ev_seq, msg_id, ts, pj in rows:
                        try:
                            m = json.loads(pj) if isinstance(pj, str) else (pj or {})
                            if isinstance(m, dict):
                                # Ensure msg_id and ts are propagated so
                                # console titles can display them.
                                m.setdefault('msg_id', msg_id)
                                if ts is not None:
                                    m.setdefault('ts', ts)
                                self._console_print_message(m)
                            self._last_printed_seq_by_thread[self.current_thread] = ev_seq
                        except Exception:
                            pass
                except Exception:
                    pass

            # Recompute approval prompts when new events arrive so the
            # Approval panel content stays in sync, but avoid doing this on
            # every UI tick in _update_panels(), which is costly on long
            # threads.
            try:
                self._compute_pending_prompt()
            except Exception:
                pass

    async def _ingest_event_for_live(self, e, thread_id: str):
        if thread_id != self.current_thread:
            return
        t = e["type"]
        if t == 'stream.open':
            self._live_state = {"active_invoke": e["invoke_id"], "content": "", "reason": "", "tools": {}, "tc_text": {}, "tc_order": []}
            try:
                inv = e.get("invoke_id") if isinstance(e, dict) else e["invoke_id"]
                self._log_system(f"Streaming started (invoke {str(inv)[-6:]}).")
            except Exception:
                pass
        elif t == 'stream.delta':
            try:
                payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
            except Exception:
                payload = {}
            txt = payload.get('text') or payload.get('content') or payload.get('delta')
            if isinstance(txt, str) and txt:
                self._live_state['content'] = (self._live_state.get('content') or '') + txt
            if isinstance(payload.get('reason'), str):
                self._live_state['reason'] = (self._live_state.get('reason') or '') + payload.get('reason', '')
            tl = payload.get('tool')
            if isinstance(tl, dict):
                name = tl.get('name') or 'tool'
                self._live_state.setdefault('tools', {})
                self._live_state['tools'][name] = self._live_state['tools'].get(name, '') + (tl.get('text') or '')
            tcd = payload.get('tool_call')
            if isinstance(tcd, dict):
                raw_key = str(tcd.get('id') or tcd.get('name') or 'tool')
                frag = tcd.get('text') or tcd.get('arguments_delta') or ''
                if isinstance(frag, str) and frag:
                    order = self._live_state.setdefault('tc_order', [])
                    text_map = self._live_state.setdefault('tc_text', {})
                    if raw_key not in order:
                        order.append(raw_key)
                    text_map[raw_key] = text_map.get(raw_key, '') + frag
        elif t == 'stream.close':
            self._live_state['active_invoke'] = None
            try:
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass
            self._log_system('Streaming finished.')

    # ---------------- Main loop ----------------
    async def run(self):
        self.running = True
        await self._start_watching_current()

        self.console.print("[bold blue]Egg Chat (eggdisplay UI)[/bold blue]")
        self.console.print("Press Enter or Ctrl+D to send (configurable). Ctrl+E clears input. Ctrl+C to quit. Type /help for commands.\n")
        # Print initial static view to console so history is visible above live panels
        self._print_static_view_current(heading=f"Switched to thread: {self.current_thread}")

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
            with Live(self._render_group(), refresh_per_second=10, screen=False, console=self.console) as live:
                while self.running:
                    # Drain input queue
                    try:
                        while True:
                            key = self.input_panel.editor.input_queue.get_nowait()
                            if not self._handle_key(key):
                                self.running = False
                                break
                    except Exception:
                        pass
                    # Update panels and live region
                    self._update_panels()
                    live.update(self._render_group())
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
                self._restore_tty()
            except Exception:
                pass


async def run_cli():
    app = EggDisplayApp()
    await app.run()


if __name__ == '__main__':
    asyncio.run(run_cli())
