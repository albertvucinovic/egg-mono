#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
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
from eggdisplay import OutputPanel, InputPanel, HStack  # type: ignore

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

    Use Ctrl+D to send, Ctrl+C to quit. Commands start with '/'.
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
        self.chat_output = OutputPanel(title="Chat Messages", initial_height=12, max_height=28)
        self.system_output = OutputPanel(title="System", initial_height=8, max_height=16)
        # Input panel with simple filesystem autocomplete
        # Advanced autocomplete (commands, models, threads, files, conversation words)
        io_mode = os.environ.get("EGG_IO_MODE", "threaded").strip().lower()
        self.input_panel = InputPanel(title="Message Input", initial_height=8, max_height=12,
                                      autocomplete_callback=self._autocomplete_cb, io_mode=io_mode)

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
        # Input behavior: default to Enter sends (toggle with /enterMode)
        self.enter_sends: bool = True
        # Track last printed event sequence per thread for console output
        self._last_printed_seq_by_thread: Dict[str, int] = {}

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
        sched = SubtreeScheduler(self.db, root_thread_id=root_tid,
                                 models_path=str(MODELS_PATH), all_models_path=str(ALL_MODELS_PATH))
        task = asyncio.create_task(sched.run_forever(poll_sec=0.05))
        self.active_schedulers[root_tid] = {"scheduler": sched, "task": task}
        self._log_system(f"Started scheduler for root {root_tid[-8:]}")

    def _ensure_scheduler_for(self, tid: str) -> None:
        rid = self._thread_root_id(tid)
        if rid not in self.active_schedulers:
            self._start_scheduler(rid)

    def _current_model_for_thread(self, tid: str) -> Optional[str]:
        try:
            rows = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 200",
                (tid,)
            ).fetchall()
            for r in rows:
                pj = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
                mk = pj.get('model_key')
                if isinstance(mk, str) and mk.strip():
                    return mk.strip()
        except Exception:
            pass
        th = self.db.get_thread(tid)
        return th.initial_model_key if th else None

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
        sflag = 'STREAMING ' if streaming else ''
        cur_tag = '[CUR] ' if tid == self.current_thread else ''
        sched_tag = '[SCHED] ' if self._thread_root_id(tid) in self.active_schedulers else ''
        return f"{cur_tag}{sched_tag}{sflag}{id_short} {status} - {recap} [model:{mk}]" + (f"  {label}" if label else '')

    def _format_tree(self, root_tid: Optional[str] = None) -> str:
        def _render_tree(tid: str, prefix: str = '', is_last: bool = True, out: Optional[List[str]] = None):
            if out is None:
                out = []
            connector = '└─ ' if is_last else '├─ '
            indent_next = '   ' if is_last else '│  '
            out.append(prefix + connector + self._format_thread_line(tid))
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
        for m in msgs[-50:]:
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
                name = m.get('name') or 'tool'
                content = (m.get('content') or '').strip()
                if content:
                    lines.append(f"[Tool: {name}]\n{content}")
            elif role == 'system':
                content = (m.get('content') or '').strip()
                if content:
                    lines.append(f"[System]\n{content}")
        return "\n\n".join(lines)

    def _compose_chat_panel_text(self) -> str:
        # Create/refresh snapshot when idle for readability
        try:
            row = self.db.current_open(self.current_thread)
        except Exception:
            row = None
        if row is None:
            try:
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass
        base = self._format_messages_text(self.current_thread)
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
        return head + "\n" + ("\n".join(parts).strip() or "No messages yet.")

    def _update_panels(self) -> None:
        self.chat_output.set_content(self._compose_chat_panel_text())
        status_lines = [
            f"Current: {self.current_thread[-8:]} | Roots with schedulers: {len(self.active_schedulers)}",
            "Send: Enter | New line: Ctrl+J | Quit: Ctrl+C",
            "Commands: /help /threads /thread <sel> /new [name] /spawn <text> /children /child <patt> /parent /delete <sel> /pause /resume /model [key] /updateAllModels <prov> /schedulers /quit",
        ]
        tail = "\n".join(self._system_log[-20:]) if self._system_log else ""
        self.system_output.set_content("\n".join(status_lines + (["", tail] if tail else [])))

    def _render_group(self) -> Group:
        row1 = HStack([self.chat_output, self.system_output]).render()
        return Group(row1, self.input_panel.render())

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
        role = m.get('role')
        content = (m.get('content') or '').strip()
        model_key = (m.get('model_key') or '').strip()

        def _panel(renderable, title: str, border: str):
            try:
                self.console.print(Panel(renderable, title=title, border_style=border))
            except Exception:
                # Fallback to plain text if Panel fails for any reason
                self.console.print(f"[{border}]{title}[/] {getattr(renderable, 'plain', str(renderable))}")

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
                self.console.print(Panel(Text(reas, no_wrap=False, overflow='fold'), title='Reasoning', border_style='magenta'))
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
        # Print a static view of recent messages for the selected thread
        tid = self.current_thread
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
            for m in msgs[-50:]:
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
            self.console.print(Panel(Text(text, no_wrap=False, overflow='fold'), title=title, border_style=border_style))
        except Exception:
            # Fallback plain
            self.console.print(f"{title}\n{text}")

    # ---------------- Autocomplete support ----------------
    def _autocomplete_cb(self, line: str, row: int, col: int) -> List[str]:
        import re, os
        # Determine current token (last word before cursor in the whole line)
        prefix = line[:col]
        m = re.search(r"([\w\-.:/~]+)$", prefix)
        token_line = m.group(1) if m else ""

        # Helper: convert full suggestions into eggdisplay items {'display','insert'}.
        def mk_items(candidates: List[str], base_token: str) -> List[Dict[str, str]]:
            items: List[Dict[str, str]] = []
            seen = set()
            for c in candidates:
                if not isinstance(c, str) or c == "":
                    continue
                # Default insertion is suffix beyond the typed token when it is a prefix; otherwise empty to avoid surprising inserts
                if base_token and c.lower().startswith(base_token.lower()):
                    ins = c[len(base_token):]
                    replace_n = 0
                elif not base_token:
                    # Nothing typed for token -> insert full candidate
                    ins = c
                    replace_n = 0
                else:
                    # Containment match: replace the token with the full candidate
                    ins = c
                    replace_n = len(base_token)
                key = (c, ins)
                if ins == "" and base_token:
                    # When we can't compute a simple suffix, still present the display, insert empty so Tab-accept won't add text
                    replace_n = len(base_token)
                if key in seen:
                    continue
                seen.add(key)
                item = {"display": c, "insert": ins}
                if replace_n:
                    item["replace"] = replace_n
                items.append(item)
            return items[:50]

        # Command-level autocomplete for first token
        if prefix.startswith('/'):
            # Command mode: split once to separate command and everything after it
            sp = prefix.find(' ')
            if sp == -1:
                # Completing the command name itself
                cmd_prefix = prefix
                cmds = [
                    '/help', '/model', '/updateAllModels', '/pause', '/resume', '/spawn', '/child', '/parent', '/children', '/threads', '/thread', '/delete', '/new', '/schedulers', '/quit'
                ]
                return mk_items([c for c in cmds if c.startswith(cmd_prefix)], cmd_prefix)

            cmd_token = prefix[:sp]
            sub = prefix[sp+1:]  # raw argument text so far (may be empty)
            # Current argument fragment (last word in sub)
            mm = re.search(r"([\w\-.:/~]+)$", sub)
            arg_token = mm.group(1) if mm else ""
            # All commands
            # Sub-argument suggestions
            # '/model <key>'
            if cmd_token == '/model':
                # Get broad list from model completer/registry
                cands = self._model_suggestions(sub)
                # Filter by case-insensitive containment of the current arg token if present
                atok = (arg_token or '').strip().lower()
                if atok:
                    cands = [c for c in cands if atok in c.lower()]
                return mk_items(cands, arg_token)
            # '/updateAllModels <provider>'
            if cmd_token == '/updateAllModels':
                provs: List[str] = []
                try:
                    if self.llm_client:
                        provs = sorted(self.llm_client.get_providers() or [])
                except Exception:
                    provs = []
                return mk_items([p for p in provs if p], arg_token)
            # '/thread <selector>' rich suggestions: match id, name, or short_recap (case-insensitive)
            if cmd_token == '/thread':
                try:
                    rows = list_threads(self.db)
                except Exception:
                    rows = []
                atok = (arg_token or '').strip().lower()
                # Sort newest-first by created_at if present
                try:
                    rows.sort(key=lambda r: getattr(r, 'created_at', ''), reverse=True)
                except Exception:
                    pass
                items: List[Dict[str, str]] = []
                for r in rows:
                    tid = r.thread_id
                    name = (r.name or '') if hasattr(r, 'name') and r.name else ''
                    recap = (r.short_recap or '') if hasattr(r, 'short_recap') and r.short_recap else ''
                    if atok:
                        hay = f"{tid} {name} {recap}".lower()
                        if atok not in hay:
                            continue
                    disp = self._format_thread_line(tid)
                    # Replace current token with full id on accept
                    rep = len(arg_token)
                    items.append({"display": disp, "insert": tid, "replace": rep})
                    if len(items) >= 50:
                        break
                return items
            # '/delete <selector>' basic id suggestions
            if cmd_token == '/delete':
                try:
                    rows = list_threads(self.db)
                except Exception:
                    rows = []
                ids = [r.thread_id for r in rows]
                return mk_items(ids, arg_token)
            # '/child <pattern>'
            if cmd_token == '/child':
                try:
                    rows = list_children_with_meta(self.db, self.current_thread)
                except Exception:
                    rows = []
                ids = [r[0] if isinstance(r, (list, tuple)) else r.thread_id for r in rows]
                return mk_items(ids, arg_token)
            # '/spawn ' -> prefer filesystem suggestions
            if cmd_token == '/spawn':
                return mk_items(self._fs_suggestions(arg_token), arg_token)
            # '/new ' -> no specific suggestions
            return []

        # Non-command: prefer filesystem suggestions for current token
        fs = self._fs_suggestions(token_line)
        if fs:
            return mk_items(fs, token_line)

        # If no filesystem matches, propose conversation words
        conv = self._conversation_suggestions(token_line)
        if conv:
            return mk_items(conv, token_line)
        return []

    def _fs_suggestions(self, token: str) -> List[str]:
        import os
        if not token:
            return []
        expanded = os.path.expanduser(token)
        base_dir = expanded
        needle = ''
        if not os.path.isdir(expanded):
            base_dir = os.path.dirname(expanded) or '.'
            needle = os.path.basename(expanded)
        try:
            entries = os.listdir(base_dir)
        except Exception:
            return []
        results: List[str] = []
        for name in entries:
            if needle and not name.startswith(needle):
                continue
            path = os.path.join(base_dir, name)
            suffix = '/' if os.path.isdir(path) else ''
            results.append((os.path.join(base_dir, name)).replace('\n', ''))
            # Convert to the same style as the token: if token contains directory, produce absolute-like continuation
            # We return full candidates; the remainder logic will subtract the token.
            results[-1] = (os.path.join(base_dir, name) + suffix)
        # Normalize to user-typed style: if user started with ~ keep it
        # For simplicity, we return absolute-expanded forms; editor inserts remainder only.
        # Sort: directories first
        results.sort(key=lambda s: (0 if s.endswith('/') else 1, s))
        return results[:50]

    def _conversation_suggestions(self, fragment: str) -> List[str]:
        if not fragment:
            return []
        import re
        words: List[str] = []
        try:
            th = self.db.get_thread(self.current_thread)
            if not th or not th.snapshot_json:
                return []
            snap = json.loads(th.snapshot_json)
            msgs = snap.get('messages', []) or []
            for m in msgs[-200:]:
                try:
                    role = (m or {}).get('role')
                    if role not in ('user', 'assistant', 'system', 'tool'):
                        continue
                    txt = (m or {}).get('content') or ''
                    if not isinstance(txt, str) or not txt:
                        continue
                    for w in re.findall(r"[A-Za-z0-9_]{3,}", txt):
                        words.append(w)
                except Exception:
                    continue
        except Exception:
            pass
        fl = fragment.lower()
        seen: set[str] = set()
        out: List[str] = []
        for w in reversed(words):
            wl = w.lower()
            if wl.startswith(fl) and wl not in seen:
                seen.add(wl)
                out.append(w)
        return out[:50]

    def _model_suggestions(self, sub: str) -> List[str]:
        """Model suggestions for '/model ' arguments.

        Prefer reusing existing logic:
          1) Use completion.ModelCompleter to mirror original CLI behavior.
          2) Fall back to AllModelsCatalog.get_all_models_suggestions for 'all:' path.
          3) Finally, a minimal manual listing from the registry as a safety net.
        """
        out: List[str] = []
        llm = self.llm_client
        if not llm:
            return out

        # Try to reuse completion.ModelCompleter for parity with prompt_toolkit flow
        try:
            from completion import ModelCompleter  # type: ignore

            class _Doc:
                def __init__(self, text: str):
                    self.text_before_cursor = text

            mc = ModelCompleter(llm)
            # Feed a fake '/model ' document so completer logic runs identically
            doc = _Doc(f"/model {sub}")
            suggestions: List[str] = []
            for c in mc.get_completions(doc, None):  # type: ignore
                try:
                    txt = getattr(c, 'text', None)
                    if isinstance(txt, str) and txt:
                        suggestions.append(txt)
                except Exception:
                    continue
            # Deduplicate, preserve order
            seen: set[str] = set()
            uniq = []
            for s in suggestions:
                if s not in seen:
                    seen.add(s)
                    uniq.append(s)
            # Do not pre-filter; return raw list for caller to fuzzy filter
            return uniq[:200]
        except Exception:
            pass

        # Fallback 2: use AllModelsCatalog helper for 'all:' path
        try:
            prefix = sub or ''
            if prefix.lower().startswith('all:'):
                return llm.catalog.get_all_models_suggestions(prefix)[:200]
        except Exception:
            pass

        # Fallback 3: minimal manual suggestions from registry
        try:
            pref = (sub or '').strip().lower()
            seen: set[str] = set()
            # display names
            for name in sorted((llm.registry.models_config or {}).keys()):
                if not pref or name.lower().startswith(pref):
                    if name not in seen:
                        seen.add(name)
                        out.append(name)
            # provider:name and alias forms
            for display, cfg in (llm.registry.models_config or {}).items():
                prov = (cfg or {}).get('provider', 'unknown')
                prov_pref = f"{prov}:{display}"
                if not pref or prov_pref.lower().startswith(pref):
                    if prov_pref not in seen:
                        seen.add(prov_pref)
                        out.append(prov_pref)
                for a in (cfg or {}).get('alias', []) or []:
                    if not isinstance(a, str):
                        continue
                    alias_full = f"{prov}:{a}"
                    if not pref or alias_full.lower().startswith(pref) or a.lower().startswith(pref):
                        if alias_full not in seen:
                            seen.add(alias_full)
                            out.append(alias_full)
            # Consider 'all:prov:model' matches if catalogs exist
            if pref:
                try:
                    for prov in (llm.get_providers() or []):
                        for mid in (llm.catalog.get_all_models_for_provider(prov) or []):
                            cand = f"all:{prov}:{mid}"
                            if pref in cand.lower() or pref in mid.lower():
                                if cand not in seen:
                                    seen.add(cand)
                                    out.append(cand)
                except Exception:
                    pass
        except Exception:
            pass
        return out[:200]

    # ---------------- Input and commands ----------------
    def _handle_key(self, key: str) -> bool:
        # Ctrl+D sends, Ctrl+C exits
        try:
            import readchar  # type: ignore
            ctrl_d = getattr(readchar.key, 'CTRL_D', '\x04')
            ctrl_c = getattr(readchar.key, 'CTRL_C', '\x03')
            enter_key = getattr(readchar.key, 'ENTER', '\r')
        except Exception:
            ctrl_d = '\x04'
            ctrl_c = '\x03'
            enter_key = '\r'
        # Quit
        if key == ctrl_c or key == '\x03':
            # Try to interrupt any active stream on the current thread for fast shutdown
            try:
                interrupt_thread(self.db, self.current_thread)
            except Exception:
                pass
            self.running = False
            return False
        # Send on Ctrl+D always
        if key == ctrl_d or key == '\x04':
            text = self.input_panel.get_text().strip()
            if text:
                try:
                    self._on_submit(text)
                except Exception as e:
                    self._log_system(f"Submit error: {e}")
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
            return True
        # Enter behavior depends on mode
        if key in (enter_key, '\r', '\n'):
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

    def _on_submit(self, text: str) -> None:
        if text.startswith('$$') and len(text) > 2:
            self._run_shell(text[2:].strip(), keep_user_turn=True, hidden=True)
            return
        if text.startswith('$') and len(text) > 1:
            self._run_shell(text[1:].strip(), keep_user_turn=True, hidden=False)
            return
        if text.startswith('/'):
            self._handle_command(text)
            return
        append_message(self.db, self.current_thread, 'user', text)
        create_snapshot(self.db, self.current_thread)
        self._ensure_scheduler_for(self.current_thread)
        self._log_system("User message queued; scheduler will stream the response.")

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
            self._log_system('Commands: /model <key>, /updateAllModels <provider>, /pause, /resume, /spawn <text>, /child <pattern>, /parent, /children, /threads, /thread <selector>, /delete <selector>, /new <name>, /schedulers, /enterMode <send|newline>, /quit')
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
            if cur_model_key:
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=new_root, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{cur_model_key}]', 'model_key': cur_model_key})
            create_snapshot(self.db, new_root)
            self._ensure_scheduler_for(new_root)
            self.current_thread = new_root
            asyncio.get_running_loop().create_task(self._start_watching_current())
            self._log_system(f"Created new root thread: {new_root[-8:]}")
            self._print_static_view_current(heading=f"Switched to thread: {self.current_thread}")
        elif cmd == 'spawn':
            def _latest_model_for_thread(tid: str) -> Optional[str]:
                try:
                    rows = self.db.conn.execute(
                        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 200",
                        (tid,)
                    ).fetchall()
                    for r in rows:
                        pj = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
                        mk = pj.get('model_key')
                        if isinstance(mk, str) and mk.strip():
                            return mk.strip()
                except Exception:
                    pass
                th = self.db.get_thread(tid)
                return th.initial_model_key if th else None
            cur_model = _latest_model_for_thread(self.current_thread)
            child = create_child_thread(self.db, self.current_thread, name='spawn', initial_model_key=cur_model)
            append_message(self.db, child, 'system', self.system_prompt)
            append_message(self.db, child, 'user', arg or 'Spawned task')
            if cur_model:
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=child, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{cur_model}]', 'model_key': cur_model})
            create_snapshot(self.db, child)
            self._ensure_scheduler_for(child)
            self._log_system(f"Spawned thread: {child[-8:]}")
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
        elif cmd == 'model':
            arg2 = (arg or '').strip()
            if arg2:
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.current_thread, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{arg2}]', 'model_key': arg2})
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

    # ---------------- Watching & streaming ----------------
    async def _start_watching_current(self):
        if self._watch_task is not None:
            try:
                self._watch_task.cancel()
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

        ew = EventWatcher(self.db, thread_id, after_seq=after_for_watch, poll_sec=0.05)
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
                        "SELECT event_seq, payload_json FROM events WHERE thread_id=? AND event_seq>? AND type='msg.create' ORDER BY event_seq ASC",
                        (self.current_thread, last_printed)
                    )
                    rows = cur.fetchall()
                    for ev_seq, pj in rows:
                        try:
                            m = json.loads(pj) if isinstance(pj, str) else (pj or {})
                            if isinstance(m, dict):
                                self._console_print_message(m)
                            self._last_printed_seq_by_thread[self.current_thread] = ev_seq
                        except Exception:
                            pass
                except Exception:
                    pass
            # Update panels after each batch
            self._update_panels()

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
        self.console.print("Press Enter or Ctrl+D to send (configurable). Ctrl+C to quit. Type /help for commands.\n")
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
            with Live(self._render_group(), refresh_per_second=30, screen=False, console=self.console) as live:
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
                        await asyncio.sleep(0.033)
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


async def run_cli():
    app = EggDisplayApp()
    await app.run()


if __name__ == '__main__':
    asyncio.run(run_cli())
