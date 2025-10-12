#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Rich + Textual
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.console import Group
from rich.segment import Segment

from textual.app import App, ComposeResult
from textual.containers import Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Input, Static
from textual.scroll_view import ScrollView
from textual.widget import Widget
from textual.reactive import reactive
from textual import events

# Local libs (eggthreads/eggllm)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eggthreads'))
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eggllm'))

from eggthreads import (
    ThreadsDB,
    SubtreeScheduler,
    create_root_thread,
    create_child_thread,
    append_message,
    create_snapshot,
    interrupt_thread,
    pause_thread,
    resume_thread,
)
from eggthreads.event_watcher import EventWatcher
from eggllm import LLMClient  # type: ignore

MODELS_PATH = Path(__file__).resolve().parent / 'models.json'
ALL_MODELS_PATH = Path(__file__).resolve().parent / 'all-models.json'
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / 'systemPrompt'

# ----------------------------------------------------------------------------
# Helpers copied/adapted from egg.py
# ----------------------------------------------------------------------------

def _get_system_prompt() -> str:
    try:
        with open(SYSTEM_PROMPT_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return "You are a helpful assistant."


def _looks_markdown(content: str) -> bool:
    if not content:
        return False
    indicators = ['```', '# ', '## ', '### ', '* ', '- ', '> ', '`']
    hits = sum(1 for i in indicators if i in content)
    if hits >= 2:
        return True
    if content.count('\n') >= 2 and hits >= 1:
        return True
    return False


def _render_message_panel(m: Dict[str, Any]) -> Optional[Panel]:
    role = m.get('role')
    content = (m.get('content') or '').strip()
    model_key = m.get('model_key') or ''

    # Error messages
    if role == 'system' and isinstance(content, str) and content.lower().startswith('llm error:'):
        title = '[bold red]Error[/bold red]'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        return Panel(Text(content, no_wrap=False, overflow='fold', style='red'), title=title, border_style='red')

    if role == 'user':
        title = "[bold green]User[/bold green]"
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        return Panel(Text(content, no_wrap=False, overflow='fold', style='green'), title=title, border_style='green')

    elif role == 'assistant':
        title = '[bold cyan]Assistant[/bold cyan]'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        # If no visible content but tool_calls/reasoning exist, render helpful summary
        if not content:
            pieces: List[str] = []
            reas = m.get('reasoning') or m.get('reasoning_content')
            if isinstance(reas, str) and reas.strip():
                pieces.append("Reasoning:\n" + reas.strip())
            tcs = m.get('tool_calls')
            if isinstance(tcs, list) and tcs:
                lines: List[str] = []
                for tc in tcs:
                    f = (tc or {}).get('function') or {}
                    name = f.get('name') or (tc or {}).get('name') or 'function'
                    args = f.get('arguments') or (tc or {}).get('arguments')
                    if isinstance(args, (dict, list)):
                        try:
                            import json as _json
                            args_str = _json.dumps(args, ensure_ascii=False)
                        except Exception:
                            args_str = str(args)
                    else:
                        args_str = str(args or '')
                    if len(args_str) > 160:
                        args_str = args_str[:160] + '…'
                    lines.append(f"- {name}({args_str})")
                if lines:
                    pieces.append("Tool calls:\n" + "\n".join(lines))
            if not pieces:
                # nothing to render
                return None
            content = "\n\n".join(pieces)
        renderable = Markdown(content) if _looks_markdown(content) else Text(content, no_wrap=False, overflow='fold', style='cyan')
        return Panel(renderable, title=title, border_style='cyan')

    elif role == 'tool':
        name = m.get('name') or 'Tool'
        title = f'[bold yellow]{name}[/bold yellow]'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        return Panel(Text(content, no_wrap=False, overflow='fold', style='yellow'), title=title, border_style='yellow')

    else:
        title = role or 'Message'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        return Panel(Text(content, no_wrap=False, overflow='fold', style='blue'), title=title, border_style='blue')


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
        for row in db.conn.execute("SELECT child_id FROM children WHERE parent_id=?", (t,)):
            q.append(row[0])
    return out[1:]

# ----------------------------------------------------------------------------
# Textual Widgets
# ----------------------------------------------------------------------------

class MessageView(ScrollView):
    """Line-API message log with reliable scrolling, wrapping, and boxed headers."""
    follow = reactive(True)

    def __init__(self):
        super().__init__()
        self._blocks: List[Dict[str, Any]] = []  # each: {title:str|None, body:str, key:str|None}
        self._vis: list[str] = []                # reflowed visual lines
        self._reflow()

    # Internal helpers -------------------------------------------------
    def _box_lines(self, title: Optional[str], body: str, width: int) -> List[str]:
        import textwrap
        w = max(width, 10)
        inner = max(w - 2, 1)
        # Top
        if title and title.strip():
            bar = " " + title.strip() + " "
            trimmed = bar[:inner]
            top = "┌" + trimmed + ("─" * max(inner - len(trimmed), 0)) + "┐"
        else:
            top = "┌" + ("─" * inner) + "┐"
        # Body wrapped
        wrapped = textwrap.wrap(
            body or "",
            width=inner,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        mid = ["│" + line.ljust(inner) + "│" for line in wrapped]
        # Bottom
        bot = "└" + ("─" * inner) + "┘"
        return [top, *mid, bot]

    def _reflow(self) -> None:
        from textual.geometry import Size
        width = max(self.size.width or 0, 1)
        vis: list[str] = []
        for blk in self._blocks:
            vis.extend(self._box_lines(blk.get('title'), blk.get('body') or '', width))
        self._vis = vis
        self.virtual_size = Size(width, max(len(self._vis), 1))
        self.refresh()

    def on_resize(self, event) -> None:
        self._reflow()

    # Public API -------------------------------------------------------
    async def clear_messages(self):
        self._blocks.clear()
        self._vis.clear()
        self._reflow()

    async def add_panel(self, panel: Panel):
        title = getattr(panel, 'title', None)
        body = getattr(panel, 'renderable', None)
        txt = body.plain if hasattr(body, 'plain') else (str(body) if body is not None else '')
        self._blocks.append({"title": title if isinstance(title, str) else None, "body": txt or '', "key": None})
        self._reflow()
        if self.follow:
            try:
                super().scroll_end(animate=False)
            except Exception:
                pass

    async def add_text(self, txt: str):
        self._blocks.append({"title": None, "body": txt or '', "key": None})
        self._reflow()
        if self.follow:
            try:
                super().scroll_end(animate=False)
            except Exception:
                pass

    # Streaming helpers: append to single block per category
    def _stream_update(self, key: str, title: str, text: str) -> None:
        # Find or create block with matching key
        for blk in self._blocks:
            if blk.get('key') == key:
                blk['body'] = (blk.get('body') or '') + (text or '')
                self._reflow()
                return
        # Not found: create new
        self._blocks.append({"title": title, "body": text or '', "key": key})
        self._reflow()

    async def update_stream_reason(self, text: str):
        self._stream_update('__reason__', 'Reasoning (streaming)', text or '')
        if self.follow:
            try:
                super().scroll_end(animate=False)
            except Exception:
                pass

    async def update_stream_tool_args(self, name: str, text: str):
        nm = name or 'tool'
        self._stream_update(f'__toolargs__:{nm}', f'Tool Call Args: {nm}', text or '')
        if self.follow:
            try:
                super().scroll_end(animate=False)
            except Exception:
                pass

    async def update_stream_tool_output(self, name: str, text: str):
        nm = name or 'tool'
        self._stream_update(f'__toolout__:{nm}', f'Tool: {nm}', text or '')
        if self.follow:
            try:
                super().scroll_end(animate=False)
            except Exception:
                pass

    async def update_stream_content(self, text: str):
        self._stream_update('__assistant__', 'Assistant (streaming)', text or '')
        if self.follow:
            try:
                super().scroll_end(animate=False)
            except Exception:
                pass

    # Line API: render visible line
    def render_line(self, y: int):
        from textual.strip import Strip
        from rich.segment import Segment
        _, scroll_y = self.scroll_offset
        y += scroll_y
        if y < 0 or y >= len(self._vis):
            return Strip.blank(self.size.width)
        src = self._vis[y]
        # Clip to width (no horizontal scrolling)
        width = max(self.size.width, 1)
        visible = (src or '')[:width].ljust(width)
        return Strip([Segment(visible)])

# ----------------------------------------------------------------------------
# Main App
# ----------------------------------------------------------------------------

class EggTextual(App):
    CSS = """
    #messages {
        height: 1fr;
        overflow-y: auto;
    }
    #input {
        height: 3;
    }
    """
    BINDINGS = [
        ("f", "toggle_follow", "Follow"),
        ("pageup", "scroll_up_page", "PgUp"),
        ("pagedown", "scroll_down_page", "PgDn"),
        ("home", "scroll_home", "Home"),
        ("end", "scroll_end", "End"),
        ("up", "scroll_up", "Up"),
        ("down", "scroll_down", "Down"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.db = ThreadsDB()
        self.db.init_schema()
        self.system_content = _get_system_prompt()
        # Create a root thread (local UI root) and scheduler
        self.root_thread = create_root_thread(self.db, name='Root')
        append_message(self.db, self.root_thread, 'system', self.system_content)
        create_snapshot(self.db, self.root_thread)
        self.current_thread: str = self.root_thread

        # schedulers tracked per-root in this UI
        self.active_schedulers: Dict[str, Dict[str, Any]] = {}
        self.prompted_roots: set[str] = set()
        self.is_prompting_scheduler: bool = False

        # defer default scheduler start until on_mount (event loop ready)
        self._start_default_scheduler = True

        # misc
        try:
            self.llm_for_completion = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH)
        except Exception:
            self.llm_for_completion = None

        # streaming task handle
        self._stream_task: Optional[asyncio.Task] = None
        self._stream_worker = None
        self._attached_invoke: Optional[str] = None
        # virtual scroll offset (lines/pages proxy)
        self.view_offset: int = 0

    # Utilities ----------------------------------------------------------------
    def _current_model_for_thread(self, tid: str) -> Optional[str]:
        try:
            rows = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 200",
                (tid,)
            ).fetchall()
            for r in rows:
                try:
                    pj = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
                except Exception:
                    pj = {}
                mk = pj.get('model_key')
                if isinstance(mk, str) and mk.strip():
                    return mk.strip()
        except Exception:
            pass
        th = self.db.get_thread(tid)
        return th.initial_model_key if th else None

    def _thread_root_id(self, tid: str) -> str:
        cur_id = tid
        while True:
            row = self.db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (cur_id,)).fetchone()
            if not row or not row[0]:
                return cur_id
            cur_id = row[0]

    def _is_streaming(self, tid: str) -> bool:
        try:
            return self.db.current_open(tid) is not None
        except Exception:
            return False

    def _root_has_scheduler(self, tid: str) -> bool:
        try:
            rid = self._thread_root_id(tid)
            return rid in self.active_schedulers
        except Exception:
            return False

    def _start_scheduler(self, root_tid: str) -> None:
        if root_tid in self.active_schedulers:
            return
        sched = SubtreeScheduler(self.db, root_thread_id=root_tid, models_path=str(MODELS_PATH), all_models_path=str(ALL_MODELS_PATH))
        task = asyncio.create_task(sched.run_forever())
        self.active_schedulers[root_tid] = {"scheduler": sched, "task": task}

    # UI composition ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Single-column layout: message view + input (no left tree)
        with Vertical():
            # MessageView is a ScrollView-based widget; no need for ScrollableContainer
            self.msg_view = MessageView()
            yield self.msg_view
            self.input = Input(placeholder="Type a message or /command ...", id='input')
            yield self.input
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh_tree()
        await self._render_thread(self.current_thread)
        # Focus input so user can start typing immediately
        try:
            await self.set_focus(self.input)
        except Exception:
            pass
        # Start default scheduler here when loop is running
        if getattr(self, '_start_default_scheduler', False):
            self._start_scheduler(self.root_thread)
            self._start_default_scheduler = False
        # Also, if current thread is already streaming, attach immediately
        await self._attach_stream(self.current_thread)
        # And keep the tree up to date initially
        try:
            await self._refresh_tree()
        except Exception:
            pass

    # Tree ---------------------------------------------------------------------
    async def _refresh_tree(self):
        # No-op in single-pane mode; kept for command compatibility
        return

    async def _populate_children(self, node, tid: str):
        # order by created_at for readability
        cur = self.db.conn.execute(
            "SELECT c.child_id, t.created_at FROM children c JOIN threads t ON t.thread_id=c.child_id WHERE c.parent_id=? ORDER BY t.created_at ASC",
            (tid,)
        )
        kids = [r[0] for r in cur.fetchall()]
        for cid in kids:
            label = self._format_thread_line(cid)
            child = node.add(label, data=cid)
            await self._populate_children(child, cid)

    def _format_thread_line(self, tid: str) -> str:
        th = self.db.get_thread(tid)
        status = th.status if th else 'unknown'
        recap = (th.short_recap if th and th.short_recap else 'No recap').strip()
        mk = self._current_model_for_thread(tid) or 'default'
        streaming = self._is_streaming(tid)
        try:
            subtree_size = len(_get_subtree(self.db, tid))
        except Exception:
            subtree_size = 0
        label = th.name if th and th.name else ''
        id_short = tid[-8:]
        sflag = '[yellow]STREAMING[/] ' if streaming else ''
        cur_tag = '[cyan][CUR][/] ' if tid == self.current_thread else ''
        sched_tag = '[cyan][SCHED][/] ' if self._root_has_scheduler(tid) and self._thread_root_id(tid) == tid else ''
        extra = f"  [dim]{label}[/dim]" if label else ''
        return f"{cur_tag}{sched_tag}{sflag}[dim]{id_short}[/dim] {status} - {recap} (subtree={subtree_size}) [dim][model: {mk}][/dim]{extra}"

    # Rendering ----------------------------------------------------------------
    async def _render_thread(self, thread_id: str) -> None:
        # Snapshot current content
        try:
            row_open = self.db.current_open(thread_id)
        except Exception:
            row_open = None
        if row_open is None:
            try:
                create_snapshot(self.db, thread_id)
            except Exception:
                pass
        await self.msg_view.clear_messages()
        msgs = _snapshot_messages(self.db, thread_id)
        # Render snapshot honoring stream_sequence if present
        for m in msgs[-80:]:
            if isinstance(m, dict) and m.get('role') == 'assistant' and isinstance(m.get('stream_sequence'), list) and m.get('stream_sequence'):
                seq = m.get('stream_sequence') or []
                grouped: List[Dict[str, Any]] = []
                for item in seq:
                    t = (item or {}).get('type')
                    txt = (item or {}).get('text') or ''
                    name = (item or {}).get('name')
                    if not isinstance(txt, str) or not txt:
                        continue
                    if grouped and grouped[-1]['type'] == t and ((t in ('tool_output', 'tool_call_args') and grouped[-1].get('name') == name) or (t in ('content', 'reason'))):
                        grouped[-1]['text'] += txt
                    else:
                        grouped.append({'type': t, 'text': txt, 'name': name})
                for g in grouped:
                    gtype = g.get('type')
                    if gtype == 'reason':
                        await self.msg_view.add_panel(Panel(Text(g.get('text',''), no_wrap=False, overflow='fold'), title='Reasoning', border_style='magenta'))
                    elif gtype == 'tool_call_args':
                        nm = g.get('name') or 'tool'
                        await self.msg_view.add_panel(Panel(Text(g.get('text',''), no_wrap=False, overflow='fold'), title=f'Tool Call Args: {nm}', border_style='yellow'))
                    elif gtype == 'tool_output':
                        nm = g.get('name') or 'tool'
                        await self.msg_view.add_panel(Panel(Text(g.get('text',''), no_wrap=False, overflow='fold'), title=f'Tool: {nm}', border_style='yellow'))
                    elif gtype == 'content':
                        m2 = dict(m)
                        m2['content'] = g.get('text','')
                        panel = _render_message_panel(m2)
                        if panel:
                            await self.msg_view.add_panel(panel)
                # Tool call summary, if present
                tcs = m.get('tool_calls')
                if isinstance(tcs, list) and tcs:
                    out_lines = []
                    for tc in tcs:
                        f = (tc or {}).get('function') or {}
                        name = f.get('name') or (tc or {}).get('name') or 'function'
                        args = f.get('arguments') or (tc or {}).get('arguments')
                        if isinstance(args, (dict, list)):
                            try:
                                import json as _json
                                args_str = _json.dumps(args, ensure_ascii=False)
                            except Exception:
                                args_str = str(args)
                        else:
                            args_str = str(args or '')
                        out_lines.append(f"{name}({args_str})")
                    await self.msg_view.add_panel(Panel(Text("\n".join(out_lines), no_wrap=False, overflow='fold'), title='Tool Calls', border_style='yellow'))
                continue
            # No stream_sequence: Reasoning before Assistant content
            if isinstance(m, dict) and m.get('role') == 'assistant':
                reas = m.get('reasoning') or m.get('reasoning_content')
                has_content = bool((m.get('content') or '').strip())
                if has_content and isinstance(reas, str) and reas.strip():
                    await self.msg_view.add_panel(Panel(Text(reas, no_wrap=False, overflow='fold'), title='Reasoning', border_style='magenta'))
            panel = _render_message_panel(m)
            if panel:
                await self.msg_view.add_panel(panel)

        # Attach to live stream (if any)
        await self._attach_stream(thread_id)
        # Important: don't block UI thread; schedule periodic pump to recheck events for this thread
        self.set_interval(0.2, lambda: asyncio.create_task(self._poll_once(thread_id)), name=f"poll-{thread_id[-8:]}")
        # Focus input to keep typing
        try:
            await self.set_focus(self.input)
        except Exception:
            pass
        # Ensure messages container is scrollable and can receive scroll events
        try:
            self.scroll.can_focus = True
        except Exception:
            pass
        # Refresh the left tree to reflect STREAMING flag changes
        try:
            await self._refresh_tree()
        except Exception:
            pass

    async def _attach_stream(self, thread_id: str) -> None:
        # Cancel previous stream task if any
        if self._stream_task and not self._stream_task.done():
            task = self._stream_task
            cur = asyncio.current_task()
            task.cancel()
            try:
                if task is not cur:
                    await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        # Start a new UI-worker task via Textual (ensures UI-thread marshalling)
        # Cancel any previous worker/task
        try:
            if self._stream_worker and not self._stream_worker.done:
                self._stream_worker.cancel()
        except Exception:
            pass
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except Exception:
                pass
        # Start a new task on the main event loop (same thread as DB)
        self._stream_task = asyncio.create_task(self._stream_thread_task(thread_id))
        self._stream_worker = None
        # Announce streaming attach (no debug)
        await self.msg_view.add_panel(Panel(Text(f"[dim]Attaching to live stream for {thread_id[-8:]}[/dim]"), border_style='cyan'))

    async def _poll_once(self, thread_id: str) -> None:
        """Lightweight poller to fetch and render any events after the last snapshot.
        This supplements the EventWatcher in case async scheduling misses callbacks.
        """
        try:
            th = self.db.get_thread(thread_id)
            after = int(th.snapshot_last_event_seq) if th and isinstance(th.snapshot_last_event_seq, int) else -1
        except Exception:
            after = -1
        try:
            cur = self.db.conn.execute(
                "SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                (thread_id, after)
            )
            rows = cur.fetchall()
        except Exception:
            rows = []
        any_delta = False
        for e in rows:
            t = e['type']
            if t == 'stream.delta':
                payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
                txt = payload.get('text') or payload.get('delta') or payload.get('content') or ''
                rs = payload.get('reason') or ''
                tl = payload.get('tool') or {}
                tc = payload.get('tool_call') or {}
                if rs:
                    await self.msg_view.update_stream_reason(str(rs))
                if isinstance(tl, dict) and tl.get('text'):
                    await self.msg_view.update_stream_tool_output(tl.get('name') or 'tool', str(tl.get('text')))
                if isinstance(tc, dict) and tc.get('text'):
                    await self.msg_view.update_stream_tool_args(tc.get('name') or 'tool', str(tc.get('text')))
                if txt:
                    await self.msg_view.update_stream_content(str(txt))
                any_delta = True
            elif t == 'stream.close':
                try:
                    create_snapshot(self.db, thread_id)
                except Exception:
                    pass
                await self._render_thread(thread_id)
                return
        # If we saw deltas, bump snapshot seq to avoid reprocessing
        if any_delta:
            try:
                create_snapshot(self.db, thread_id)
            except Exception:
                pass

    async def _stream_thread_task(self, thread_id: str) -> None:
        # Determine start boundary (rewind to stream.open)
        try:
            th = self.db.get_thread(thread_id)
            start_after = int(th.snapshot_last_event_seq) if th and isinstance(th.snapshot_last_event_seq, int) else -1
        except Exception:
            start_after = -1
        active_target: Optional[str] = None
        try:
            row_open = self.db.current_open(thread_id)
        except Exception:
            row_open = None
        if row_open is not None:
            try:
                active_target = row_open["invoke_id"] if isinstance(row_open, dict) else row_open["invoke_id"]
            except Exception:
                try:
                    active_target = row_open["invoke_id"]
                except Exception:
                    active_target = None
        attach_after_seq = start_after
        if active_target:
            try:
                row_open_seq = self.db.conn.execute(
                    "SELECT MIN(event_seq) FROM events WHERE invoke_id=? AND type='stream.open'",
                    (active_target,)
                ).fetchone()
                open_seq = int(row_open_seq[0]) if row_open_seq and row_open_seq[0] is not None else None
                if open_seq is not None:
                    attach_after_seq = min(start_after, open_seq - 1) if start_after >= 0 else (open_seq - 1)
            except Exception:
                pass

        # Preload existing deltas into streaming panels
        def _preload(after_seq: int, target_invoke: Optional[str]):
            last_seq = after_seq
            live_content = ''
            live_reason = ''
            tool_stream: Dict[str, str] = {}
            tool_call_text: Dict[str, str] = {}
            seen_open = False
            current_inv: Optional[str] = None
            try:
                cur = self.db.conn.execute(
                    "SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                    (thread_id, after_seq)
                )
                rows = cur.fetchall()
            except Exception:
                rows = []
            for e in rows:
                t = e['type']
                inv = e['invoke_id']
                if e['event_seq'] is not None:
                    last_seq = e['event_seq']
                # If a target_invoke is specified, only collect for that
                if target_invoke is not None and inv != target_invoke:
                    continue
                if t == 'stream.open':
                    seen_open = True
                    current_inv = inv
                    live_content = ''
                    live_reason = ''
                    tool_stream.clear()
                    tool_call_text.clear()
                elif t == 'stream.delta' and (seen_open or target_invoke):
                    payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
                    txt = payload.get('text') or payload.get('delta') or payload.get('content')
                    if isinstance(txt, str) and txt:
                        live_content += txt
                    rs = payload.get('reason')
                    if isinstance(rs, str) and rs:
                        live_reason += rs
                    tl = payload.get('tool')
                    if isinstance(tl, dict):
                        nm = tl.get('name') or 'tool'
                        tool_stream[nm] = tool_stream.get(nm, '') + (tl.get('text') or '')
                    tcd = payload.get('tool_call')
                    if isinstance(tcd, dict):
                        nm = str(tcd.get('name') or tcd.get('id') or 'tool')
                        frag = tcd.get('text') or tcd.get('arguments_delta') or ''
                        if frag:
                            tool_call_text[nm] = tool_call_text.get(nm, '') + str(frag)
                elif t == 'stream.close':
                    # If a target was specified, stop at its close; else stop at first close
                    if (target_invoke is None) or (inv == target_invoke):
                        break
            return last_seq, live_content, live_reason, tool_stream, tool_call_text, current_inv

        last_seq, live_content, live_reason, tool_stream, tool_call_text, pre_inv = _preload(attach_after_seq, active_target)

        # Determine active invoke id (from open row or preloaded stream.open)
        active_invoke = active_target or pre_inv

        # Initialize streaming panels with preloaded content (if any)
        if live_reason:
            await self.msg_view.update_stream_reason(live_reason)
        for nm, txt in tool_call_text.items():
            await self.msg_view.update_stream_tool_args(nm, txt)
        for nm, txt in tool_stream.items():
            await self.msg_view.update_stream_tool_output(nm, txt)
        if live_content:
            await self.msg_view.update_stream_content(live_content)

        # Watch for new events
        ew = EventWatcher(self.db, thread_id, after_seq=last_seq, poll_sec=0.05)
        active_invoke = active_target or pre_inv
        async for batch in ew.aiter():
            for e in batch:
                t = e['type']
                if t == 'stream.open':
                    active_invoke = e['invoke_id']
                    await self.msg_view.clear_streaming()
                    # Reset accumulators when a new invoke starts
                    live_content = ''
                    live_reason = ''
                    tool_stream.clear()
                    tool_call_text.clear()
                elif t == 'stream.delta':
                    if not (active_invoke and e['invoke_id'] == active_invoke):
                        continue
                    payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
                    txt = payload.get('text') or payload.get('delta') or payload.get('content')
                    if isinstance(txt, str) and txt:
                        await self.msg_view.update_stream_content(txt if not live_content else live_content + txt)
                        live_content = (live_content or '') + txt
                    rs = payload.get('reason')
                    if isinstance(rs, str) and rs:
                        await self.msg_view.update_stream_reason(rs if not live_reason else live_reason + rs)
                        live_reason = (live_reason or '') + rs
                    tl = payload.get('tool')
                    if isinstance(tl, dict):
                        nm = tl.get('name') or 'tool'
                        prev = tool_stream.get(nm, '')
                        now = prev + (tl.get('text') or '')
                        tool_stream[nm] = now
                        await self.msg_view.update_stream_tool_output(nm, now)
                    tcd = payload.get('tool_call')
                    if isinstance(tcd, dict):
                        nm = str(tcd.get('name') or tcd.get('id') or 'tool')
                        prev = tool_call_text.get(nm, '')
                        frag = tcd.get('text') or tcd.get('arguments_delta') or ''
                        if frag:
                            now = prev + str(frag)
                            tool_call_text[nm] = now
                            await self.msg_view.update_stream_tool_args(nm, now)
                elif t == 'stream.close' and active_invoke and e['invoke_id'] == active_invoke:
                    # Clear streaming panels and re-render snapshot for final messages
                    await self.msg_view.clear_streaming()
                    try:
                        create_snapshot(self.db, thread_id)
                    except Exception:
                        pass
                    # Trigger a re-render of the thread to display the completed messages
                    await self._render_thread(thread_id)
                    # End the stream task
                    return

    # Commands -----------------------------------------------------------------
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or '').strip()
        self.input.value = ''
        if not text:
            return
        if text.startswith('/'):
            await self._handle_command(text)
        else:
            # normal user message
            append_message(self.db, self.current_thread, 'user', text)
            create_snapshot(self.db, self.current_thread)
            # Immediately attach to stream to see live output
            await self._attach_stream(self.current_thread)
            # Render baseline snapshot now (pre-stream) so history shows while streaming starts
            await self._render_thread(self.current_thread)

    async def _handle_command(self, cmdline: str) -> None:
        parts = cmdline[1:].split(None, 1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ''
        if cmd == 'help':
            await self.msg_view.add_panel(Panel(Text('/model <key>, /updateAllModels <provider>, /pause, /resume, /spawn <text>, /child <pattern>, /parent, /children, /threads, /thread <selector>, /schedulers, /quit'), border_style='blue'))
        elif cmd == 'model':
            if arg:
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.current_thread, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{arg}]', 'model_key': arg})
                create_snapshot(self.db, self.current_thread)
                await self._render_thread(self.current_thread)
            else:
                if not self.llm_for_completion:
                    await self.msg_view.add_panel(Panel('Models not available (llm client not initialized).', border_style='red'))
                else:
                    by_provider: Dict[str, List[str]] = {}
                    for name, cfg in (self.llm_for_completion.registry.models_config or {}).items():
                        prov = cfg.get('provider', 'unknown')
                        by_provider.setdefault(prov, []).append(name)
                    lines = []
                    for prov in sorted(by_provider.keys()):
                        lines.append(f"{prov}:")
                        for m in sorted(by_provider[prov]):
                            lines.append(f"  - {m}")
                    lines.append("\nTip: type 'all:' to see full provider catalogs (if downloaded). Use 'all:provider:model'.")
                    await self.msg_view.add_panel(Panel(Text("\n".join(lines)), border_style='blue', title='Available models'))
        elif cmd == 'pause':
            pause_thread(self.db, self.current_thread)
            await self._refresh_tree()
        elif cmd == 'resume':
            resume_thread(self.db, self.current_thread)
            await self._refresh_tree()
        elif cmd == 'spawn':
            # propagate current model
            cur_model = self._current_model_for_thread(self.current_thread)
            child = create_child_thread(self.db, self.current_thread, name='spawn', initial_model_key=cur_model)
            append_message(self.db, child, 'system', self.system_content)
            append_message(self.db, child, 'user', arg or 'Spawned task')
            if cur_model:
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=child, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{cur_model}]', 'model_key': cur_model})
            create_snapshot(self.db, child)
            await self.msg_view.add_panel(Panel(f"Spawned thread: {child}", border_style='green'))
            await self._ensure_scheduler_for_thread(child)
            await self._refresh_tree()
        elif cmd == 'child':
            patt = (arg or '').lower()
            cur = self.db.conn.execute(
                "SELECT c.child_id, t.name, t.short_recap FROM children c JOIN threads t ON t.thread_id=c.child_id WHERE c.parent_id=?",
                (self.current_thread,)
            )
            candidates: List[str] = []
            for r in cur.fetchall():
                child_id, name, recap = (r[0] or ''), (r[1] or ''), (r[2] or '')
                if not patt or patt in (name + ' ' + recap + ' ' + child_id).lower():
                    candidates.append(r[0])
            if candidates:
                await self._switch_thread(candidates[0])
            else:
                await self.msg_view.add_panel(Panel('No matching child.', border_style='yellow'))
        elif cmd == 'parent':
            row = self.db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (self.current_thread,)).fetchone()
            if row and row[0]:
                await self._switch_thread(row[0])
            else:
                await self.msg_view.add_panel(Panel('Already at root or no parent found.', border_style='yellow'))
        elif cmd == 'children':
            # Render subtree of current thread as tree-like text
            await self.msg_view.add_panel(Panel(Text(self._render_tree_text(self.current_thread)), border_style='blue', title='Children'))
        elif cmd == 'threads':
            roots = [r[0] for r in self.db.conn.execute("SELECT thread_id FROM threads WHERE thread_id NOT IN (SELECT child_id FROM children)").fetchall()]
            txt_lines = ["Legend: [CUR]=current thread  [SCHED]=local scheduler running  STREAMING=has open stream"]
            for rid in roots:
                txt_lines.append(self._render_tree_text(rid))
            await self.msg_view.add_panel(Panel(Text("\n".join(txt_lines)), border_style='blue', title='Threads'))
        elif cmd == 'thread':
            sel = (arg or '').strip()
            if not sel:
                await self.msg_view.add_panel(Panel(f"Current thread: {self.current_thread}", border_style='blue'))
            else:
                try:
                    cur = self.db.conn.execute("SELECT thread_id, name, short_recap, created_at FROM threads")
                    matches: List[str] = []
                    sel_l = sel.lower()
                    rows = cur.fetchall()
                    for r in rows:
                        if r[0] == sel:
                            matches = [r[0]]
                            break
                    if not matches:
                        suf = [r[0] for r in rows if r[0].lower().endswith(sel_l)]
                        if suf:
                            matches = suf
                    if not matches:
                        cont = [r[0] for r in rows if sel_l in r[0].lower()]
                        if cont:
                            matches = cont
                    if not matches:
                        name_matches = [r[0] for r in rows if isinstance(r[1], str) and sel_l in r[1].lower()]
                        if name_matches:
                            matches = name_matches
                    if not matches:
                        recap_matches = [r[0] for r in rows if isinstance(r[2], str) and sel_l in r[2].lower()]
                        if recap_matches:
                            matches = recap_matches
                    if not matches:
                        await self.msg_view.add_panel(Panel(f"No thread matches selector: {sel}", border_style='yellow'))
                    else:
                        if len(matches) > 1:
                            ca = {r[0]: r[3] for r in rows}
                            matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
                        await self._switch_thread(matches[0])
                except Exception as e:
                    await self.msg_view.add_panel(Panel(f"Error switching thread: {e}", border_style='red'))
            # Refocus input after commands
            try:
                await self.set_focus(self.input)
            except Exception:
                pass
        elif cmd == 'schedulers':
            if not self.active_schedulers:
                await self.msg_view.add_panel(Panel('No active schedulers in this session.', border_style='yellow'))
            else:
                txt = []
                for rid in self.active_schedulers.keys():
                    txt.append(self._render_tree_text(rid))
                await self.msg_view.add_panel(Panel(Text("\n".join(txt)), border_style='blue', title='Active SubtreeSchedulers'))
        elif cmd == 'updateAllModels':
            provider = (arg or '').strip()
            if not provider:
                await self.msg_view.add_panel(Panel('Usage: /updateAllModels <provider>', border_style='yellow'))
            else:
                try:
                    llm_tmp = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH)
                    res = llm_tmp.update_all_models(provider)
                    await self.msg_view.add_panel(Panel(res, border_style='cyan', title='Update All Models'))
                except Exception as e:
                    await self.msg_view.add_panel(Panel(f'Error: {e}', border_style='red', title='Update All Models'))
        elif cmd == 'quit':
            await self.action_quit()
        else:
            await self.msg_view.add_panel(Panel('Unknown command', border_style='yellow'))

    def _render_tree_text(self, root_tid: str) -> str:
        lines: List[str] = []
        def rec(tid: str, prefix: str = '', is_last: bool = True):
            connector = '└─ ' if is_last else '├─ '
            indent_next = '   ' if is_last else '│  '
            lines.append(prefix + connector + self._format_thread_line(tid))
            cur = self.db.conn.execute(
                "SELECT c.child_id, t.created_at FROM children c JOIN threads t ON t.thread_id=c.child_id WHERE c.parent_id=? ORDER BY t.created_at ASC",
                (tid,)
            )
            kids = [r[0] for r in cur.fetchall()]
            for i, cid in enumerate(kids):
                rec(cid, prefix + indent_next, i == len(kids)-1)
        rec(root_tid, '', True)
        return "\n".join(lines)

    async def _switch_thread(self, tid: str):
        # Scheduler prompt if switching to a root not in active schedulers
        root_tid = self._thread_root_id(tid)
        if root_tid not in self.active_schedulers and root_tid not in self.prompted_roots:
            await self.msg_view.add_panel(Panel(self._render_tree_text(root_tid), border_style='blue', title='Proposed subtree'))
            await self.msg_view.add_panel(Panel('Start scheduler for this subtree? Type: yes / no', border_style='cyan'))
            self.prompted_roots.add(root_tid)
            self.awaiting_scheduler_confirm = root_tid
        else:
            self.awaiting_scheduler_confirm = None
        self.current_thread = tid
        await self._refresh_tree()
        await self._render_thread(self.current_thread)

    async def _ensure_scheduler_for_thread(self, tid: str):
        root_tid = self._thread_root_id(tid)
        if root_tid in self.active_schedulers:
            return
        if root_tid in self.prompted_roots:
            return
        await self.msg_view.add_panel(Panel(self._render_tree_text(root_tid), border_style='blue', title='Proposed subtree'))
        await self.msg_view.add_panel(Panel('Start scheduler for this subtree? Type: yes / no', border_style='cyan'))
        self.prompted_roots.add(root_tid)
        self.awaiting_scheduler_confirm = root_tid

    async def on_input_changed(self, event: Input.Changed) -> None:
        # no-op
        pass

    async def on_input_submitted_post(self, event: Input.Submitted) -> None:
        # not used
        pass

    async def action_toggle_follow(self) -> None:
        self.msg_view.follow = not self.msg_view.follow
        await self.msg_view.add_panel(Panel(f"Follow: {'ON' if self.msg_view.follow else 'OFF'}", border_style='blue'))

    # Scrolling actions for messages container (manual)
    async def action_scroll_up(self) -> None:
        # For now, no-op because we trim old messages; could implement virtual offset if needed
        pass

    async def action_scroll_down(self) -> None:
        pass

    async def action_scroll_up_page(self) -> None:
        pass

    async def action_scroll_down_page(self) -> None:
        pass

    async def action_scroll_home(self) -> None:
        # No-op: we keep only the recent window
        pass

    async def action_scroll_end(self) -> None:
        # No-op: end is always visible
        pass

    # Intercept simple yes/no input when awaiting scheduler confirmation
    async def on_key(self, event: events.Key) -> None:
        if getattr(self, 'awaiting_scheduler_confirm', None):
            if event.key.lower() in ('y', 'enter'):
                # accept only if input buffer says yes; otherwise ignore
                # read current input value
                val = (self.input.value or '').strip().lower()
                if val in ('y', 'yes'):
                    rid = self.awaiting_scheduler_confirm
                    self._start_scheduler(rid)
                    await self.msg_view.add_panel(Panel(f"Started scheduler for root {rid}", border_style='green'))
                    self.awaiting_scheduler_confirm = None
                    self.input.value = ''
                elif val in ('n', 'no'):
                    await self.msg_view.add_panel(Panel("Skipped starting scheduler.", border_style='yellow'))
                    self.awaiting_scheduler_confirm = None
                    self.input.value = ''
            elif event.key.lower() == 'escape':
                self.awaiting_scheduler_confirm = None


if __name__ == "__main__":
    EggTextual().run()
